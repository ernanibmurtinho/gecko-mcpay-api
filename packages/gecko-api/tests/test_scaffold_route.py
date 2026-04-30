"""Tests for the POST /scaffold endpoint (S8-API-01).

Stub-mode coverage only — live x402 + Supabase + OpenAI are mocked. The
MCP ``gecko_scaffold`` tool is the single source of truth for the
business logic; this suite asserts the HTTP transport behaves correctly:

    1. Valid session_id triggers ``generate_scaffold`` and returns the
       bundle JSON.
    2. Invalid session_id (non-UUID) returns 400.
    3. KillVerdictError -> 409.
    4. Stub-mode does not require x402 payment headers.
    5. /.well-known/x402 does NOT advertise /scaffold in stub mode (route
       is FREE and unregistered).
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

# Force stub mode BEFORE importing the app — settings are frozen at import time.
os.environ.setdefault("X402_MODE", "stub")
os.environ.setdefault("GECKO_WALLET_ADDRESS", "STUB_TEST_WALLET")


@pytest.fixture
def client() -> Iterator[TestClient]:
    for mod in [m for m in sys.modules if m.startswith("gecko_api")]:
        sys.modules.pop(mod, None)

    from gecko_api.main import app
    from gecko_core.orchestration.scaffold.models import ScaffoldResult

    fake_result = ScaffoldResult(
        session_id="00000000-0000-0000-0000-000000000abc",
        paths=[Path("/tmp/.gecko/scaffolds/abc/PRD.md")],
        tokens_used=1234,
        cost_usd=0.0,
        summary="stub scaffold",
    )

    with (
        patch(
            "gecko_core.orchestration.scaffold.generate_scaffold",
            new=AsyncMock(return_value=fake_result),
        ),
        TestClient(app) as c,
    ):
        yield c


def test_scaffold_returns_bundle_in_stub_mode(client: TestClient) -> None:
    """Stub mode is free — no PAYMENT-SIGNATURE header required."""
    r = client.post(
        "/scaffold",
        json={"session_id": "00000000-0000-0000-0000-000000000abc"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["session_id"] == "00000000-0000-0000-0000-000000000abc"
    assert body["tokens_used"] == 1234
    assert body["summary"] == "stub scaffold"
    assert isinstance(body["paths"], list) and body["paths"]


def test_scaffold_invalid_session_id_returns_400(client: TestClient) -> None:
    r = client.post("/scaffold", json={"session_id": "not-a-uuid"})
    assert r.status_code == 400, r.text
    assert "invalid session_id" in r.text


def test_scaffold_kill_verdict_returns_409() -> None:
    """KillVerdictError -> 409 Conflict (resource state forbids scaffolding)."""
    for mod in [m for m in sys.modules if m.startswith("gecko_api")]:
        sys.modules.pop(mod, None)
    from gecko_api.main import app
    from gecko_core.orchestration.scaffold import KillVerdictError

    with (
        patch(
            "gecko_core.orchestration.scaffold.generate_scaffold",
            new=AsyncMock(side_effect=KillVerdictError("idea was killed")),
        ),
        TestClient(app) as c,
    ):
        r = c.post(
            "/scaffold",
            json={"session_id": "00000000-0000-0000-0000-000000000def"},
        )
    assert r.status_code == 409, r.text
    assert "kill_verdict" in r.text


def test_well_known_does_not_advertise_scaffold_in_stub(client: TestClient) -> None:
    """Stub mode: /scaffold is FREE -> not registered on x402."""
    r = client.get("/.well-known/x402")
    assert r.status_code == 200
    routes = {entry["route"] for entry in r.json()["routes"]}
    assert "POST /scaffold" not in routes
