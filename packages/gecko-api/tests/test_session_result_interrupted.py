"""Tests for the interrupted-session 410 path + lifespan shutdown drain.

2026-05-06 — production logs showed two `Shutting down` events mid
research session. The container's background task gets SIGTERMed, the
session row is left at status='generating', and clients poll 425
forever. We:

  1. Stamp every still-running session for this worker as 'interrupted'
     in the lifespan shutdown branch (`_drain_inflight_sessions`).
  2. Return HTTP 410 Gone from `/sessions/{id}/result` for interrupted
     sessions so the client knows to re-submit instead of polling.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

os.environ["X402_MODE"] = "stub"
os.environ.setdefault("GECKO_WALLET_ADDRESS", "STUB_WALLET_ADDRESS_NOT_FOR_LIVE")
os.environ.setdefault("TAVILY_API_KEY", "test-stub-key")


def _make_record(status: str, sid: UUID) -> Any:
    """Build a SessionRecord-shaped object the route's `record.status` can read."""
    from gecko_core.sessions.store import SessionRecord

    return SessionRecord(
        id=sid,
        idea="test",
        tier="basic",
        status=status,  # type: ignore[arg-type]
        payment_intent_id=None,
        payment_mode="stub",
        x402_tx_signature=None,
        project_id=None,
        paid_from_wallet_address=None,
        phase="pre_product",
        parent_session_id=None,
        created_at=datetime.now(tz=UTC),
        completed_at=None,
        deleted_at=None,
    )


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    for mod in [m for m in sys.modules if m.startswith("gecko_api")]:
        sys.modules.pop(mod, None)

    from gecko_api import main as api_main

    # Stub SessionStore.from_env so the route doesn't try to reach Supabase.
    fake_store = MagicMock()
    fake_store.get = AsyncMock()
    fake_store.get_result = AsyncMock(return_value=None)
    fake_store.get_error = AsyncMock(return_value=None)

    monkeypatch.setattr(api_main.SessionStore, "from_env", classmethod(lambda cls: fake_store))

    with TestClient(api_main.app) as c:
        c.__dict__["fake_store"] = fake_store  # surface for assertions
        yield c


def test_interrupted_session_returns_410(client: TestClient) -> None:
    """A session row at status='interrupted' must answer 410, not 425."""
    sid = uuid4()
    fake_store: Any = client.__dict__["fake_store"]
    fake_store.get.return_value = _make_record("interrupted", sid)

    r = client.get(f"/sessions/{sid}/result")
    assert r.status_code == 410, r.text
    body = r.json()
    detail = body["detail"]
    assert detail["status"] == "interrupted"
    assert "container restart" in detail["detail"]


def test_running_session_still_returns_425(client: TestClient) -> None:
    """Sanity — non-interrupted in-flight sessions keep the 425 contract."""
    sid = uuid4()
    fake_store: Any = client.__dict__["fake_store"]
    fake_store.get.return_value = _make_record("generating", sid)

    r = client.get(f"/sessions/{sid}/result")
    assert r.status_code == 425, r.text


def test_drain_inflight_sessions_marks_each(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lifespan-shutdown path: every in-flight session_id is stamped interrupted.

    We don't drive a real lifespan here — the shutdown branch just calls
    `_drain_inflight_sessions()`, which is the public seam to test.
    """
    for mod in [m for m in sys.modules if m.startswith("gecko_api")]:
        sys.modules.pop(mod, None)

    from gecko_api import main as api_main

    sids = [uuid4(), uuid4(), uuid4()]
    # Build fake tasks (loop is irrelevant — keys are object identity).
    fake_tasks = [MagicMock(spec=asyncio.Task) for _ in sids]
    api_main._INFLIGHT_SESSIONS.clear()
    for task, sid in zip(fake_tasks, sids, strict=True):
        api_main._INFLIGHT_SESSIONS[task] = sid

    fake_store = MagicMock()
    fake_store.mark_interrupted = AsyncMock()
    monkeypatch.setattr(api_main.SessionStore, "from_env", classmethod(lambda cls: fake_store))

    asyncio.run(api_main._drain_inflight_sessions())

    # mark_interrupted called once per in-flight session, with reason.
    assert fake_store.mark_interrupted.await_count == len(sids)
    called_sids = {call.args[0] for call in fake_store.mark_interrupted.await_args_list}
    assert called_sids == set(sids)
    for call in fake_store.mark_interrupted.await_args_list:
        assert call.kwargs.get("reason") == "container_shutdown"

    api_main._INFLIGHT_SESSIONS.clear()


def test_drain_with_no_inflight_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty in-flight set must not even instantiate the store."""
    for mod in [m for m in sys.modules if m.startswith("gecko_api")]:
        sys.modules.pop(mod, None)

    from gecko_api import main as api_main

    api_main._INFLIGHT_SESSIONS.clear()

    sentinel = MagicMock(side_effect=AssertionError("store must not init"))
    monkeypatch.setattr(api_main.SessionStore, "from_env", classmethod(sentinel))

    # Must not raise — the AssertionError above would surface if the
    # function called from_env on an empty set.
    asyncio.run(api_main._drain_inflight_sessions())
