"""Pro-tier retry-without-recharge — POST /research/pro/{session_id}/retry.

Covers:
  - Happy path: failed session + valid retry_token → 202, status flips off
    `failed`, fresh events_token issued, background task scheduled.
  - Token validation: missing / malformed / wrong-prefix (events) / wrong
    session_id / expired → 401.
  - Status guard: retry against `pending|indexing|generating|complete` → 409.
    Double-redemption (token used twice) → 409 on the second call because
    status has already moved off `failed`.
  - Tier guard: retry against a basic-tier session → 409.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient


def _purge_gecko_api_modules() -> None:
    for mod in [m for m in sys.modules if m.startswith("gecko_api")]:
        sys.modules.pop(mod, None)


def _record(
    sid: UUID,
    *,
    status: str = "failed",
    tier: str = "pro",
    project_id: UUID | None = None,
) -> Any:
    """Build a SessionRecord-shaped object for the mock store.get()."""
    from gecko_core.sessions.store import SessionRecord

    return SessionRecord(
        id=sid,
        idea="test-idea-for-retry",
        tier=tier,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        project_id=project_id,
        created_at=datetime.now(UTC),
    )


@pytest.fixture
def client_and_secret() -> Iterator[tuple[TestClient, AsyncMock, str]]:
    os.environ["X402_MODE"] = "stub"
    os.environ["GECKO_WALLET_ADDRESS"] = "STUB_TEST_WALLET"
    os.environ["EVENTS_SECRET"] = "test-secret-fixed-for-retry-fixture"
    _purge_gecko_api_modules()

    from gecko_api.main import app

    fake_store = AsyncMock()
    fake_store.update_status = AsyncMock(return_value=None)
    fake_store.set_error = AsyncMock(return_value=None)
    fake_store.set_result = AsyncMock(return_value=None)
    fake_store.append_pro_event = AsyncMock(return_value=42)

    with (
        patch("gecko_api.main.SessionStore.from_env", return_value=fake_store),
        patch("gecko_api.main._run_pro_debate", new=AsyncMock(return_value=None)),
        TestClient(app) as c,
    ):
        yield c, fake_store, "test-secret-fixed-for-retry-fixture"


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_retry_happy_path_returns_202_and_flips_status(
    client_and_secret: tuple[TestClient, AsyncMock, str],
) -> None:
    client, store, secret = client_and_secret
    from gecko_api.events_token import issue_retry_token

    sid = uuid4()
    store.get = AsyncMock(return_value=_record(sid, status="failed"))

    token = issue_retry_token(sid, secret)
    r = client.post(f"/research/pro/{sid}/retry", params={"token": token})
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["session_id"] == str(sid)
    assert body["status"] == "processing"
    assert body["events_url"] == f"/research/pro/{sid}/events"
    assert isinstance(body["events_token"], str) and len(body["events_token"]) > 20

    # Status was flipped off `failed` before the task was scheduled.
    store.update_status.assert_awaited()
    args, _ = store.update_status.call_args
    assert args[0] == sid
    assert args[1] != "failed"


# ---------------------------------------------------------------------------
# Token validation
# ---------------------------------------------------------------------------


def test_retry_rejects_missing_token(
    client_and_secret: tuple[TestClient, AsyncMock, str],
) -> None:
    client, store, _ = client_and_secret
    sid = uuid4()
    store.get = AsyncMock(return_value=_record(sid))
    r = client.post(f"/research/pro/{sid}/retry")
    assert r.status_code == 401


def test_retry_rejects_malformed_token(
    client_and_secret: tuple[TestClient, AsyncMock, str],
) -> None:
    client, store, _ = client_and_secret
    sid = uuid4()
    store.get = AsyncMock(return_value=_record(sid))
    r = client.post(f"/research/pro/{sid}/retry", params={"token": "garbage"})
    assert r.status_code == 401


def test_retry_rejects_events_prefix_token(
    client_and_secret: tuple[TestClient, AsyncMock, str],
) -> None:
    """An events: token must NOT redeem against the retry endpoint."""
    client, store, secret = client_and_secret
    from gecko_api.events_token import issue_token

    sid = uuid4()
    store.get = AsyncMock(return_value=_record(sid))
    events_only = issue_token(sid, secret)  # default prefix='events'
    r = client.post(f"/research/pro/{sid}/retry", params={"token": events_only})
    assert r.status_code == 401


def test_retry_rejects_wrong_session_token(
    client_and_secret: tuple[TestClient, AsyncMock, str],
) -> None:
    client, store, secret = client_and_secret
    from gecko_api.events_token import issue_retry_token

    sid = uuid4()
    other = uuid4()
    store.get = AsyncMock(return_value=_record(sid))
    token = issue_retry_token(other, secret)
    r = client.post(f"/research/pro/{sid}/retry", params={"token": token})
    assert r.status_code == 401


def test_retry_rejects_expired_token(
    client_and_secret: tuple[TestClient, AsyncMock, str],
) -> None:
    client, store, secret = client_and_secret
    from gecko_api.events_token import issue_retry_token

    sid = uuid4()
    store.get = AsyncMock(return_value=_record(sid))
    expired = issue_retry_token(sid, secret, ttl_seconds=-10)
    r = client.post(f"/research/pro/{sid}/retry", params={"token": expired})
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Status guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["pending", "indexing", "generating", "complete"])
def test_retry_rejects_non_failed_status(
    client_and_secret: tuple[TestClient, AsyncMock, str],
    status: str,
) -> None:
    client, store, secret = client_and_secret
    from gecko_api.events_token import issue_retry_token

    sid = uuid4()
    store.get = AsyncMock(return_value=_record(sid, status=status))
    token = issue_retry_token(sid, secret)
    r = client.post(f"/research/pro/{sid}/retry", params={"token": token})
    assert r.status_code == 409


def test_retry_double_redemption_409_on_second_use(
    client_and_secret: tuple[TestClient, AsyncMock, str],
) -> None:
    """Same retry_token used twice — second call sees non-failed status."""
    client, store, secret = client_and_secret
    from gecko_api.events_token import issue_retry_token

    sid = uuid4()
    # Simulate the status transition: first call sees `failed`, second sees `pending`.
    store.get = AsyncMock(
        side_effect=[_record(sid, status="failed"), _record(sid, status="pending")]
    )
    token = issue_retry_token(sid, secret)

    r1 = client.post(f"/research/pro/{sid}/retry", params={"token": token})
    assert r1.status_code == 202

    r2 = client.post(f"/research/pro/{sid}/retry", params={"token": token})
    assert r2.status_code == 409


def test_retry_rejects_basic_tier(
    client_and_secret: tuple[TestClient, AsyncMock, str],
) -> None:
    client, store, secret = client_and_secret
    from gecko_api.events_token import issue_retry_token

    sid = uuid4()
    store.get = AsyncMock(return_value=_record(sid, status="failed", tier="basic"))
    token = issue_retry_token(sid, secret)
    r = client.post(f"/research/pro/{sid}/retry", params={"token": token})
    assert r.status_code == 409


def test_retry_returns_404_for_missing_session(
    client_and_secret: tuple[TestClient, AsyncMock, str],
) -> None:
    client, store, secret = client_and_secret
    from gecko_api.events_token import issue_retry_token

    sid = uuid4()
    store.get = AsyncMock(return_value=None)
    token = issue_retry_token(sid, secret)
    r = client.post(f"/research/pro/{sid}/retry", params={"token": token})
    assert r.status_code == 404
