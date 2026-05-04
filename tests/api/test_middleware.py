"""x402 middleware integration tests for gecko-api.

Goals:
    1. Unpaid POST /research returns 402 with a parseable PaymentRequired
       header carrying at least one Solana entry.
    2. Paid POST /research (stub-mode payload) returns 200 with the mocked
       ResearchResult.
    3. /sessions/{id}/ask is FREE (not gated).
    4. /.well-known/x402 returns the route catalog with $20 / $0.75 entries.
    5. Replaying the same X-PAYMENT header doesn't double-bill (stub
       facilitator's settle is called once per request, not twice for one).

These tests mock `gecko_core.research` and `gecko_core.ask` so they never
touch OpenAI / Tavily / Supabase.
"""

from __future__ import annotations

import base64
import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

# Force stub mode BEFORE importing the app — settings are frozen at import time.
os.environ["X402_MODE"] = "stub"
os.environ["X402_NETWORK"] = "solana-devnet"
os.environ["GECKO_WALLET_ADDRESS"] = "STUB_WALLET_ADDRESS_NOT_FOR_LIVE"
# Pin prices explicitly — load_dotenv() at import time would otherwise reload
# the local .env override ($0.10) after os.environ.pop() cleared it.
os.environ["RESEARCH_BASIC_PRICE"] = "$20.00"
os.environ["RESEARCH_PRO_PRICE"] = "$0.75"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _fake_research_result(session_id: str = "11111111-1111-1111-1111-111111111111") -> dict:
    """Minimal ResearchResult-shaped dict for mocking gecko_core.research."""
    return {
        "session_id": session_id,
        "tier": "basic",
        "business_plan": {
            "problem": "p",
            "icp": "i",
            "solution": "s",
            "market": "m",
            "business_model": "bm",
            "channels": "c",
            "risks": ["r1"],
            "citations": [],
        },
        "validation_report": {
            "market_size_signal": "msig",
            "competitor_analysis": "ca",
            "demand_evidence": "de",
            "risk_flags": [],
            "citations": [],
        },
        "prd": {
            "v1_scope": ["v1"],
            "v2_scope": [],
            "v3_scope": [],
            "acceptance_criteria": ["ac"],
            "non_functional": [],
            "success_metrics": [],
            "citations": [],
        },
        "sources": [
            {
                "url": "https://example.com/a",
                "type": "web",
                "chunk_count": 3,
                "indexed_at": datetime.now(UTC).isoformat(),
            }
        ],
    }


@pytest.fixture
def client() -> Iterator[TestClient]:
    # gecko_api.main does Settings.from_env() at module-level import. tests/
    # conftest.py's load_dotenv() may have populated RESEARCH_BASIC_PRICE from
    # a developer .env (e.g. our $0.10 demo override). Scrub those, purge the
    # module cache, then re-import — that way the test asserts on the SDK's
    # default $20.00/$0.75 prices regardless of dev-env contamination.
    import sys

    os.environ["X402_MODE"] = "stub"
    os.environ["X402_NETWORK"] = "solana-devnet"
    os.environ["GECKO_WALLET_ADDRESS"] = "STUB_WALLET_ADDRESS_NOT_FOR_LIVE"
    os.environ["RESEARCH_BASIC_PRICE"] = "$20.00"
    os.environ["RESEARCH_PRO_PRICE"] = "$0.75"
    for mod in [m for m in sys.modules if m.startswith("gecko_api")]:
        sys.modules.pop(mod, None)

    from gecko_api.main import app
    from gecko_core.models import ResearchResult

    fake_result = ResearchResult.model_validate(_fake_research_result())

    # The async handler calls SessionStore.from_env() directly (to create the
    # row before kicking off the background task) and inside the background
    # task too. Stub it with a fake that returns a deterministic session_id
    # without touching Supabase.
    from uuid import UUID, uuid4

    fake_store = AsyncMock()
    fake_store.create = AsyncMock(side_effect=lambda *a, **kw: uuid4())
    fake_store.set_tx_signature = AsyncMock(return_value=None)
    fake_store.set_price = AsyncMock(return_value=None)
    fake_store.get = AsyncMock(return_value=None)
    fake_store.get_result = AsyncMock(return_value=None)
    fake_store.get_economics = AsyncMock(return_value=None)
    fake_store.set_result = AsyncMock(return_value=None)
    fake_store.set_error = AsyncMock(return_value=None)
    fake_store.update_status = AsyncMock(return_value=None)
    fake_store.list_sources = AsyncMock(return_value=[])
    fake_store.set_session_project = AsyncMock(return_value=None)
    fake_store._project_spend = AsyncMock(return_value=(0.0, 0))

    with (
        patch("gecko_api.main.SessionStore.from_env", return_value=fake_store),
        patch("gecko_api.main.gecko_core.research", new=AsyncMock(return_value=fake_result)),
        patch("gecko_api.main._run_research_background", new=AsyncMock(return_value=None)),
        TestClient(app) as c,
    ):
        yield c

    # Silence unused-import warning when the underscore-only branch is taken.
    _ = UUID


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _decode_payment_required_header(value: str) -> dict:
    """The middleware encodes the PaymentRequired object as base64 JSON."""
    return json.loads(base64.b64decode(value).decode("utf-8"))


def _build_payment_payload_header(accepts_entry: dict) -> str:
    """Build a base64 X-PAYMENT header that matches one accepted requirement.

    In stub mode the facilitator returns is_valid=True regardless of the
    payload contents — but the middleware still requires the payload's
    `accepted` block to match an existing PaymentRequirements (scheme,
    network, amount, asset, payTo all equal).
    """
    payload_obj = {
        "x402Version": 2,
        "payload": {"signature": "stub-sig", "transaction": "stub-tx"},
        "accepted": accepts_entry,
    }
    return base64.b64encode(json.dumps(payload_obj).encode("utf-8")).decode("utf-8")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_unpaid_research_returns_402_with_solana_accepts(client: TestClient) -> None:
    r = client.post("/research", json={"idea": "a hotel guide for Brazil", "tier": "basic"})
    assert r.status_code == 402

    # The accepts catalog is in the PAYMENT-REQUIRED header (base64 JSON),
    # not the body. Headers in starlette are case-insensitive.
    header_value = r.headers.get("payment-required") or r.headers.get("PAYMENT-REQUIRED")
    assert header_value, f"missing PAYMENT-REQUIRED header; got headers={dict(r.headers)}"
    decoded = _decode_payment_required_header(header_value)
    assert "accepts" in decoded
    assert len(decoded["accepts"]) >= 1
    networks = {entry.get("network") for entry in decoded["accepts"]}
    assert any("solana" in n for n in networks if n), f"no solana entry in {networks}"


def test_paid_research_returns_202(client: TestClient) -> None:
    """Async contract: paid /research returns 202 + session_id immediately.

    The full workflow runs as a background task; the client polls
    /sessions/{id}/result for completion. We assert only the 202 envelope here
    because the background task is mocked away in the fixture.
    """
    # Step 1 — unpaid call to get the requirement details.
    r = client.post("/research", json={"idea": "a hotel guide for Brazil", "tier": "basic"})
    assert r.status_code == 402
    decoded = _decode_payment_required_header(r.headers["payment-required"])
    accepts_entry = decoded["accepts"][0]

    # Step 2 — paid call with stub PAYMENT-SIGNATURE header.
    payment_header = _build_payment_payload_header(accepts_entry)
    r2 = client.post(
        "/research",
        json={"idea": "a hotel guide for Brazil", "tier": "basic"},
        headers={"PAYMENT-SIGNATURE": payment_header},
    )
    assert r2.status_code == 202, r2.text
    body = r2.json()
    assert body["status"] == "processing"
    assert body["session_id"]
    assert body["poll_url"].startswith("/sessions/")


def test_ask_route_is_free(client: TestClient) -> None:
    """No 402 on /sessions/{id}/ask — once paid, follow-ups are unlimited."""
    from gecko_core.models import AskResult

    fake_ask = AskResult(
        session_id="11111111-1111-1111-1111-111111111111",
        answer="grounded answer",
        citations=[],
    )
    with patch("gecko_api.main.gecko_core.ask", new=AsyncMock(return_value=fake_ask)):
        r = client.post(
            "/sessions/11111111-1111-1111-1111-111111111111/ask",
            json={"question": "what is the strongest validation signal?"},
        )
    # Either 200 (handler ran) or some non-402 status — the point is the
    # middleware doesn't gate this path.
    assert r.status_code != 402, r.text
    assert r.status_code == 200, r.text


def test_sources_route_is_free(client: TestClient) -> None:
    with patch("gecko_api.main.gecko_core.sources", new=AsyncMock(return_value=[])):
        r = client.get("/sessions/11111111-1111-1111-1111-111111111111/sources")
    assert r.status_code != 402
    assert r.status_code == 200
    assert r.json() == []


def test_well_known_x402_catalog(client: TestClient) -> None:
    r = client.get("/.well-known/x402")
    assert r.status_code == 200
    body = r.json()
    assert body["x402_version"] == 2
    routes = {entry["route"]: entry for entry in body["routes"]}
    assert "POST /research" in routes
    assert "POST /research/pro" in routes

    basic_prices = {a["price"] for a in routes["POST /research"]["accepts"]}
    pro_prices = {a["price"] for a in routes["POST /research/pro"]["accepts"]}
    assert "$20.00" in basic_prices
    assert "$0.75" in pro_prices


def test_idempotency_replayed_payment_header(client: TestClient) -> None:
    """Replaying the same payment header twice yields two valid responses
    without the stub facilitator double-counting (it has no state, so each
    request settles independently — what we're guarding is that the second
    call doesn't *fail* due to nonce/replay errors and that the handler
    response shape is identical)."""
    r0 = client.post("/research", json={"idea": "a hotel guide for Brazil", "tier": "basic"})
    assert r0.status_code == 402
    accepts_entry = _decode_payment_required_header(r0.headers["payment-required"])["accepts"][0]
    payment_header = _build_payment_payload_header(accepts_entry)

    r1 = client.post(
        "/research",
        json={"idea": "a hotel guide for Brazil", "tier": "basic"},
        headers={"PAYMENT-SIGNATURE": payment_header},
    )
    r2 = client.post(
        "/research",
        json={"idea": "a hotel guide for Brazil", "tier": "basic"},
        headers={"PAYMENT-SIGNATURE": payment_header},
    )
    assert r1.status_code == 202, r1.text
    assert r2.status_code == 202, r2.text
    # Both calls returned a session id — they may differ (each replay creates
    # a new session). Idempotency at the payment layer is provided by the
    # x402 facilitator, not the handler.
    assert r1.json()["session_id"]
    assert r2.json()["session_id"]


def test_paid_research_with_project_id_calls_set_session_project(
    client: TestClient,
) -> None:
    """Phase B5 v1 — passing project_id+frames_username on /research must
    cause set_session_project to be invoked with paid_from_wallet_address
    formatted as `<frames_username>:main`.
    """
    from gecko_api.main import SessionStore

    r0 = client.post("/research", json={"idea": "a hotel guide for Brazil", "tier": "basic"})
    assert r0.status_code == 402
    accepts_entry = _decode_payment_required_header(r0.headers["payment-required"])["accepts"][0]
    payment_header = _build_payment_payload_header(accepts_entry)

    project_id = "11111111-2222-3333-4444-555555555555"
    r = client.post(
        "/research",
        json={
            "idea": "a hotel guide for Brazil",
            "tier": "basic",
            "project_id": project_id,
            "frames_username": "alice",
        },
        headers={"PAYMENT-SIGNATURE": payment_header},
    )
    assert r.status_code == 202, r.text

    fake_store = SessionStore.from_env()
    fake_store.set_session_project.assert_awaited()  # type: ignore[attr-defined]
    call = fake_store.set_session_project.await_args  # type: ignore[attr-defined]
    assert str(call.args[1]) == project_id
    assert call.kwargs["paid_from_wallet_address"] == "alice:main"


def test_spent_by_project_endpoint_is_free(client: TestClient) -> None:
    project_id = "11111111-2222-3333-4444-555555555555"
    r = client.get(f"/sessions/spent-by-project/{project_id}")
    assert r.status_code != 402
    assert r.status_code == 200
    body = r.json()
    assert body["project_id"] == project_id
    assert body["total_spent_usd"] == 0.0
    assert body["sessions_count"] == 0


def test_spent_by_project_invalid_uuid(client: TestClient) -> None:
    r = client.get("/sessions/spent-by-project/not-a-uuid")
    assert r.status_code == 400


def test_healthz_is_free(client: TestClient) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert r.json()["payments"] == "stub"
