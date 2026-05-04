"""Pricing assertions for /research and /research/pro x402 RouteConfig.

Workstream D1: confirm /research advertises the basic-tier price and
/research/pro advertises $0.75 USDC (= 750_000 base units, USDC has 6
decimals on Solana).

Tests run against the FastAPI TestClient under X402_MODE=stub. The 402
challenge headers are identical between stub and live — only the
facilitator's settle response differs — so stub gives us full coverage
of the advertised amounts without standing up a real facilitator.

The existing `test_middleware.py` covers the paid-path / settle behaviour;
this module is dedicated to the pricing contract.
"""

from __future__ import annotations

import base64
import json
import os
import sys
from collections.abc import Iterator
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient


def _decode_payment_required_header(value: str) -> dict:
    return json.loads(base64.b64decode(value).decode("utf-8"))


def _purge_gecko_api_modules() -> None:
    for mod in [m for m in sys.modules if m.startswith("gecko_api")]:
        sys.modules.pop(mod, None)


def _build_fake_store() -> AsyncMock:
    from uuid import uuid4

    store = AsyncMock()
    store.create = AsyncMock(side_effect=lambda *a, **kw: uuid4())
    store.set_tx_signature = AsyncMock(return_value=None)
    store.set_price = AsyncMock(return_value=None)
    store.get = AsyncMock(return_value=None)
    store.get_result = AsyncMock(return_value=None)
    store.set_result = AsyncMock(return_value=None)
    store.set_error = AsyncMock(return_value=None)
    store.update_status = AsyncMock(return_value=None)
    store.set_session_project = AsyncMock(return_value=None)
    store._project_spend = AsyncMock(return_value=(0.0, 0))
    return store


@pytest.fixture
def client() -> Iterator[TestClient]:
    """TestClient against gecko_api in X402_MODE=stub with default prices."""
    os.environ["X402_MODE"] = "stub"
    os.environ["X402_NETWORK"] = "solana-devnet"
    os.environ["GECKO_WALLET_ADDRESS"] = "STUB_WALLET_ADDRESS_NOT_FOR_LIVE"
    # Pin prices explicitly so local .env overrides don't bleed in via load_dotenv.
    os.environ["RESEARCH_BASIC_PRICE"] = "$20.00"
    os.environ["RESEARCH_PRO_PRICE"] = "$0.75"
    _purge_gecko_api_modules()

    from gecko_api.main import app

    fake_store = _build_fake_store()
    with (
        patch("gecko_api.main.SessionStore.from_env", return_value=fake_store),
        patch("gecko_api.main._run_research_background", new=AsyncMock(return_value=None)),
        TestClient(app) as c,
    ):
        yield c


# Expected USDC base-unit amounts at default prices (6 decimals on Solana).
BASIC_AMOUNT = "20000000"  # $20.00
PRO_AMOUNT = "750000"  # $0.75 — V1 Pro tier constant


# ---------------------------------------------------------------------------
# 402 challenge advertised amounts
# ---------------------------------------------------------------------------


def test_research_basic_402_advertises_basic_amount(client: TestClient) -> None:
    r = client.post("/research", json={"idea": "a hotel guide for Brazil", "tier": "basic"})
    assert r.status_code == 402
    decoded = _decode_payment_required_header(r.headers["payment-required"])
    amounts = {entry["amount"] for entry in decoded["accepts"]}
    assert BASIC_AMOUNT in amounts, f"basic 402 advertised {amounts}, expected {BASIC_AMOUNT}"


def test_research_pro_402_advertises_075_amount(client: TestClient) -> None:
    r = client.post("/research/pro", json={"idea": "a hotel guide for Brazil", "tier": "pro"})
    assert r.status_code == 402
    decoded = _decode_payment_required_header(r.headers["payment-required"])
    amounts = {entry["amount"] for entry in decoded["accepts"]}
    assert PRO_AMOUNT in amounts, f"pro 402 advertised {amounts}, expected {PRO_AMOUNT}"


def test_pro_route_advertises_solana_network(client: TestClient) -> None:
    """Pro tier shares the same chain + scheme as basic — only amount differs."""
    r = client.post("/research/pro", json={"idea": "a hotel guide for Brazil", "tier": "pro"})
    decoded = _decode_payment_required_header(r.headers["payment-required"])
    schemes = {entry["scheme"] for entry in decoded["accepts"]}
    networks = {entry["network"] for entry in decoded["accepts"]}
    assert "exact" in schemes
    assert any("solana" in n for n in networks)


def test_well_known_x402_advertises_both_tiers(client: TestClient) -> None:
    r = client.get("/.well-known/x402")
    assert r.status_code == 200
    body = r.json()
    routes = {entry["route"]: entry for entry in body["routes"]}
    assert "POST /research" in routes
    assert "POST /research/pro" in routes
    basic_prices = {a["price"] for a in routes["POST /research"]["accepts"]}
    pro_prices = {a["price"] for a in routes["POST /research/pro"]["accepts"]}
    assert "$20.00" in basic_prices
    assert "$0.75" in pro_prices


# ---------------------------------------------------------------------------
# Same paths register correctly in stub mode (no payment header → 402)
# ---------------------------------------------------------------------------


def test_stub_mode_basic_route_returns_402_without_payment(client: TestClient) -> None:
    """Stub mode mirrors live: middleware still issues a 402 challenge when
    no payment header is present. Stub only short-circuits *settle* (the
    facilitator call), not the challenge path.
    """
    r = client.post("/research", json={"idea": "a hotel guide for Brazil", "tier": "basic"})
    assert r.status_code == 402


def test_stub_mode_pro_route_returns_402_without_payment(client: TestClient) -> None:
    r = client.post("/research/pro", json={"idea": "a hotel guide for Brazil", "tier": "pro"})
    assert r.status_code == 402
