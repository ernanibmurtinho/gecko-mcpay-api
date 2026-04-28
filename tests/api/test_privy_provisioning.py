"""End-to-end-ish tests for S2-05 (project wallet provisioning) and S2-06
(server-side budget cap enforcement).

We mock at two boundaries:
  * Supabase — replaced by an AsyncMock SessionStore so no DB roundtrip.
  * Privy — respx-mocked at the HTTP boundary so the real SDK / endpoint
    is never reached.

The x402 paywall middleware is bypassed using the stub-mode payload pattern
borrowed from tests/api/test_middleware.py — paid calls send a
PAYMENT-SIGNATURE header whose `accepted` block matches the route's
PaymentRequirements. /projects has no x402 middleware, so it just needs the
frames.ag bearer mock.
"""

from __future__ import annotations

import base64
import json
import os
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
import respx
from fastapi.testclient import TestClient
from gecko_core.sessions.store import ProjectWallet
from httpx import Response

# Force stub mode + stable env BEFORE importing gecko_api.
os.environ.setdefault("X402_MODE", "stub")
os.environ.setdefault("GECKO_WALLET_ADDRESS", "STUB_TEST_WALLET")
os.environ.setdefault("X402_NETWORK", "solana-devnet")


def _project_row(name: str = "demo", budget: float | None = 5.0) -> dict[str, object]:
    return {
        "id": str(uuid4()),
        "frames_username": "alice",
        "name": name,
        "budget_usd": budget,
        "wallet_address": None,
        "wallet_provider": "frames-policy",
        "created_at": datetime.now(tz=UTC).isoformat(),
        "deleted_at": None,
    }


def _reload_app() -> None:
    """Drop cached modules so Settings.from_env picks up new env vars."""
    for mod in [m for m in sys.modules if m.startswith("gecko_api")]:
        sys.modules.pop(mod, None)


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {
        "Authorization": "Bearer mf_alicekey",
        "X-Frames-Username": "alice",
    }


def _mock_frames_ok() -> respx.Router:
    from gecko_api.auth import FRAMES_BASE_URL

    router = respx.mock(assert_all_called=False)
    router.get(f"{FRAMES_BASE_URL}/wallets/alice/balances").mock(
        return_value=Response(200, json={"balances": []})
    )
    return router


# ---------------------------------------------------------------------------
# x402 paid-call helpers (mirrors tests/api/test_middleware.py)
# ---------------------------------------------------------------------------


def _decode_payment_required_header(value: str) -> dict:
    return json.loads(base64.b64decode(value).decode("utf-8"))


def _build_payment_payload_header(accepts_entry: dict) -> str:
    payload_obj = {
        "x402Version": 2,
        "payload": {"signature": "stub-sig", "transaction": "stub-tx"},
        "accepted": accepts_entry,
    }
    return base64.b64encode(json.dumps(payload_obj).encode("utf-8")).decode("utf-8")


def _paid_headers(client: TestClient, route: str = "/research") -> dict[str, str]:
    """Trigger the 402 once to grab requirements, then build a stub PAYMENT
    header that the stub facilitator will rubber-stamp."""
    r = client.post(route, json={"idea": "test idea long enough", "tier": "basic"})
    assert r.status_code == 402, r.text
    decoded = _decode_payment_required_header(r.headers["payment-required"])
    accepts_entry = decoded["accepts"][0]
    return {"PAYMENT-SIGNATURE": _build_payment_payload_header(accepts_entry)}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def client_privy_on() -> Iterator[TestClient]:
    """TestClient with Privy creds set + Supabase mocked."""
    os.environ["PRIVY_APP_ID"] = "cmtest123"
    os.environ["PRIVY_APP_SECRET"] = "sec-test-456"
    _reload_app()

    from gecko_api.auth import _reset_cache_for_tests
    from gecko_api.main import app

    _reset_cache_for_tests()

    fake_store = AsyncMock()
    fake_store.create_project = AsyncMock(return_value=uuid4())
    fake_store.get_project = AsyncMock(return_value=None)
    fake_store.list_projects = AsyncMock(return_value=[])
    fake_store.delete_project = AsyncMock(return_value=True)
    fake_store.project_total_spent = AsyncMock(return_value=0.0)
    fake_store.project_budget_remaining = AsyncMock(return_value=None)
    fake_store.list_project_sessions = AsyncMock(return_value=[])
    fake_store._project_spend = AsyncMock(return_value=(0.0, 0))
    fake_store.get_project_wallet = AsyncMock(
        return_value=ProjectWallet(
            privy_wallet_id=None,
            privy_wallet_address=None,
            budget_cap_usd=Decimal("5.00"),
            spent_usd=Decimal("0"),
        )
    )
    fake_store.set_project_wallet = AsyncMock(return_value=None)
    fake_store.increment_project_spent = AsyncMock(return_value=Decimal("0.10"))
    fake_store.create = AsyncMock(return_value=uuid4())
    fake_store.set_session_project = AsyncMock(return_value=None)
    fake_store.set_tx_signature = AsyncMock(return_value=None)
    fake_store.set_price = AsyncMock(return_value=None)

    with (
        patch("gecko_api.main.SessionStore.from_env", return_value=fake_store),
        TestClient(app) as c,
    ):
        c.fake_store = fake_store  # type: ignore[attr-defined]
        yield c

    os.environ.pop("PRIVY_APP_ID", None)
    os.environ.pop("PRIVY_APP_SECRET", None)


@pytest.fixture
def client_privy_off() -> Iterator[TestClient]:
    """TestClient with sentinel Privy creds — provisioning must be skipped."""
    os.environ["PRIVY_APP_ID"] = "__unset__"
    os.environ["PRIVY_APP_SECRET"] = "__unset__"
    _reload_app()

    from gecko_api.auth import _reset_cache_for_tests
    from gecko_api.main import app

    _reset_cache_for_tests()

    fake_store = AsyncMock()
    fake_store.create_project = AsyncMock(return_value=uuid4())
    fake_store.get_project = AsyncMock(return_value=None)
    fake_store._project_spend = AsyncMock(return_value=(0.0, 0))
    fake_store.get_project_wallet = AsyncMock(
        return_value=ProjectWallet(
            privy_wallet_id=None,
            privy_wallet_address=None,
            budget_cap_usd=Decimal("5.00"),
            spent_usd=Decimal("0"),
        )
    )
    fake_store.set_project_wallet = AsyncMock(return_value=None)

    with (
        patch("gecko_api.main.SessionStore.from_env", return_value=fake_store),
        TestClient(app) as c,
    ):
        c.fake_store = fake_store  # type: ignore[attr-defined]
        yield c

    os.environ.pop("PRIVY_APP_ID", None)
    os.environ.pop("PRIVY_APP_SECRET", None)


# ---------------------------------------------------------------------------
# S2-05 — Privy wallet provisioning at POST /projects
# ---------------------------------------------------------------------------


def test_create_project_provisions_privy_wallet(
    client_privy_on: TestClient,
    auth_headers: dict[str, str],
) -> None:
    row = _project_row(name="demo", budget=5.0)
    fake = client_privy_on.fake_store  # type: ignore[attr-defined]
    fake.get_project = AsyncMock(return_value=row)

    privy_router = respx.mock(base_url="https://api.privy.io", assert_all_called=True)
    privy_router.post("/v1/wallets").mock(
        return_value=Response(
            200,
            json={
                "id": "wallet-xyz",
                "address": "SoLaNa1111111111111111111111111111111111",
                "chain_type": "solana",
            },
        )
    )

    with _mock_frames_ok(), privy_router:
        r = client_privy_on.post(
            "/projects",
            json={"name": "demo", "budget_usd": 5.0},
            headers=auth_headers,
        )

    assert r.status_code == 201, r.text
    body = r.json()
    assert body["wallet_address"] == "SoLaNa1111111111111111111111111111111111"
    assert body["wallet_provider"] == "privy-direct"
    fake.set_project_wallet.assert_awaited_once()


def test_create_project_with_sentinel_privy_skips_silently(
    client_privy_off: TestClient,
    auth_headers: dict[str, str],
) -> None:
    row = _project_row(name="demo", budget=5.0)
    fake = client_privy_off.fake_store  # type: ignore[attr-defined]
    fake.get_project = AsyncMock(return_value=row)

    # No respx mock for Privy: any call would raise. The test asserts
    # provisioning is bypassed entirely.
    with _mock_frames_ok():
        r = client_privy_off.post(
            "/projects",
            json={"name": "demo", "budget_usd": 5.0},
            headers=auth_headers,
        )

    assert r.status_code == 201, r.text
    body = r.json()
    assert body.get("wallet_address") is None
    fake.set_project_wallet.assert_not_awaited()


# ---------------------------------------------------------------------------
# S2-06 — budget cap enforcement on /research
# ---------------------------------------------------------------------------


def test_research_blocked_when_project_over_budget(
    client_privy_on: TestClient,
) -> None:
    project_id = str(uuid4())
    fake = client_privy_on.fake_store  # type: ignore[attr-defined]
    fake.get_project_wallet = AsyncMock(
        return_value=ProjectWallet(
            privy_wallet_id="wallet-xyz",
            privy_wallet_address="SoLaNa11111",
            budget_cap_usd=Decimal("5.00"),
            spent_usd=Decimal("5.10"),
        )
    )

    headers = _paid_headers(client_privy_on)
    r = client_privy_on.post(
        "/research",
        json={
            "idea": "test idea long enough",
            "tier": "basic",
            "project_id": project_id,
        },
        headers=headers,
    )
    assert r.status_code == 402, r.text
    body = r.json()
    assert body["error"] == "project_budget_exceeded"
    assert body["project_id"] == project_id
    assert body["spent_usd"] == "5.10"
    assert body["budget_cap_usd"] == "5.00"


def test_research_under_budget_proceeds(
    client_privy_on: TestClient,
) -> None:
    project_id = str(uuid4())
    fake = client_privy_on.fake_store  # type: ignore[attr-defined]
    fake.get_project_wallet = AsyncMock(
        return_value=ProjectWallet(
            privy_wallet_id="wallet-xyz",
            privy_wallet_address="SoLaNa11111",
            budget_cap_usd=Decimal("5.00"),
            spent_usd=Decimal("0.50"),
        )
    )

    async def _noop(*args: object, **kwargs: object) -> None:
        return None

    headers = _paid_headers(client_privy_on)
    with patch("gecko_api.main._run_research_background", _noop):
        r = client_privy_on.post(
            "/research",
            json={
                "idea": "test idea long enough",
                "tier": "basic",
                "project_id": project_id,
            },
            headers=headers,
        )

    assert r.status_code == 202, r.text
    body = r.json()
    assert "session_id" in body


# ---------------------------------------------------------------------------
# Lazy provisioning on first paid call
# ---------------------------------------------------------------------------


def test_research_lazy_provisions_wallet_when_missing(
    client_privy_on: TestClient,
) -> None:
    """Existing project without wallet → first paid call provisions one."""
    project_id = str(uuid4())
    fake = client_privy_on.fake_store  # type: ignore[attr-defined]
    fake.get_project_wallet = AsyncMock(
        return_value=ProjectWallet(
            privy_wallet_id=None,
            privy_wallet_address=None,
            budget_cap_usd=Decimal("5.00"),
            spent_usd=Decimal("0"),
        )
    )

    privy_router = respx.mock(base_url="https://api.privy.io", assert_all_called=True)
    privy_router.post("/v1/wallets").mock(
        return_value=Response(
            200,
            json={
                "id": "wallet-lazy",
                "address": "SoLaNaLazy1111",
                "chain_type": "solana",
            },
        )
    )

    async def _noop(*args: object, **kwargs: object) -> None:
        return None

    headers = _paid_headers(client_privy_on)
    with privy_router, patch("gecko_api.main._run_research_background", _noop):
        r = client_privy_on.post(
            "/research",
            json={
                "idea": "test idea long enough",
                "tier": "basic",
                "project_id": project_id,
            },
            headers=headers,
        )

    assert r.status_code == 202, r.text
    fake.set_project_wallet.assert_awaited_once()


# ---------------------------------------------------------------------------
# No project_id path — zero behavioral change for users without projects.
# ---------------------------------------------------------------------------


def test_research_without_project_id_never_calls_privy(
    client_privy_on: TestClient,
) -> None:
    fake = client_privy_on.fake_store  # type: ignore[attr-defined]

    async def _noop(*args: object, **kwargs: object) -> None:
        return None

    # No respx mock for Privy: any outbound call would raise.
    headers = _paid_headers(client_privy_on)
    with patch("gecko_api.main._run_research_background", _noop):
        r = client_privy_on.post(
            "/research",
            json={"idea": "test idea long enough", "tier": "basic"},
            headers=headers,
        )

    assert r.status_code == 202, r.text
    fake.get_project_wallet.assert_not_awaited()
    fake.set_project_wallet.assert_not_awaited()
