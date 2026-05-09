"""Task 8 — end-to-end smoke for the trading-oracle endpoints.

Hits POST /trade_research and POST /trade_research/pro on a live gecko-api
HTTP surface. Drives the full Phase 10A x402 dance:

    1. POST without a payment header  -> expect HTTP 402 with the x402 v2
       challenge in the ``payment-required`` *header* (NOT body).
    2. Decode the challenge, build a stub-mode signed PAYMENT-SIGNATURE
       header, retry.
    3. Expect HTTP 200 with a TradePanelVerdict-shaped body:
         - verdict in {"act", "pass", "defer"}
         - confidence in [0, 1]
         - turns: list with >= 1 entry (>= 1 cite-bearing agent turn)
         - key_drivers and blocker_questions present (intent payload
           surrogate — the wire shape doesn't carry an "intent" field per
           se; the verdict + drivers + blockers + turns ARE the intent
           payload that callers feed into solana-claude / defi-engineer).

Run modes
---------
- Default ``pytest`` run: SKIPPED (GECKO_E2E_BASE_URL unset).
- Targeted: ``pytest -m e2e_smoke`` with::

      GECKO_E2E_BASE_URL=https://api.geckovision.tech \\
      uv run pytest tests/e2e/test_trade_oracle_smoke.py -v -m e2e_smoke

  Or against a local stack::

      GECKO_E2E_BASE_URL=http://localhost:8000 \\
      uv run pytest tests/e2e/test_trade_oracle_smoke.py -v -m e2e_smoke

Constraints
-----------
The deployed surface is in X402_MODE=stub today (founder gate — see
memory ``project_x402_stub_then_live``). The tests therefore submit a
stub payment payload; a live-mode flip would require regenerating the
header against the facilitator and is explicitly out of scope here.

The pro-tier wire shape currently carries no ``backtest`` field at the
HTTP layer — Phase 10A folded the backtest evidence into the
``key_drivers`` and ``turns`` of the same TradePanelVerdict envelope.
The smoke asserts that pro produces the same envelope at the higher
$0.75 price point so any future schema split (e.g. a real ``backtest``
field) is caught at the contract level.
"""

from __future__ import annotations

import base64
import json
import os
from collections.abc import Iterator

import httpx
import pytest

pytestmark = [pytest.mark.e2e_smoke]


_BASE_URL_ENV = "GECKO_E2E_BASE_URL"
_KAMINO_QUESTION = "Should a trader deposit USDC into Kamino's USDC reserve right now?"
_DEFAULT_PROTOCOL = "kamino"
_DEFAULT_VERTICAL = "defi-trading"
_VALID_DECISIONS: frozenset[str] = frozenset({"act", "pass", "defer"})


def _base_url() -> str:
    url = os.environ.get(_BASE_URL_ENV)
    if not url:
        pytest.skip(f"{_BASE_URL_ENV} not set; e2e smoke skipped")
    return url.rstrip("/")


def _decode_challenge(header_value: str) -> dict:
    """Decode the base64-JSON x402 v2 challenge from the response header."""
    return json.loads(base64.b64decode(header_value).decode("utf-8"))


def _stub_payment_header(accepts_entry: dict) -> str:
    """Build a stub-mode PAYMENT-SIGNATURE header.

    Mirrors the helper in
    ``packages/gecko-api/tests/test_trade_research_endpoint.py``. Only
    valid against a server running ``X402_MODE=stub``; live deployments
    will reject this signature at the facilitator.
    """
    payload_obj = {
        "x402Version": 2,
        "payload": {"signature": "stub-sig", "transaction": "stub-tx"},
        "accepted": accepts_entry,
    }
    return base64.b64encode(json.dumps(payload_obj).encode("utf-8")).decode("utf-8")


@pytest.fixture(scope="module")
def http_client() -> Iterator[httpx.Client]:
    base = _base_url()
    # Long timeout: the 7-agent panel + Mongo retrieval can run 30-60s.
    with httpx.Client(base_url=base, timeout=httpx.Timeout(120.0, connect=10.0)) as c:
        yield c


def _paid_post(client: httpx.Client, *, path: str, body: dict) -> httpx.Response:
    """Run the 402 -> paid retry dance once and return the second response.

    Asserts the first response carries a v2 challenge in the
    ``payment-required`` header (not body) per Phase 10A.
    """
    r0 = client.post(path, json=body)
    assert r0.status_code == 402, f"expected 402, got {r0.status_code}: {r0.text}"

    header_value = r0.headers.get("payment-required") or r0.headers.get("PAYMENT-REQUIRED")
    assert header_value, f"missing payment-required header on 402; headers={dict(r0.headers)!r}"
    # Body should NOT carry the challenge (header-only contract).
    try:
        body_json = r0.json()
    except ValueError:
        body_json = {}
    if isinstance(body_json, dict):
        assert "accepts" not in body_json, (
            "x402 v2 contract violation: challenge leaked into response body"
        )

    challenge = _decode_challenge(header_value)
    accepts = challenge.get("accepts") or []
    assert accepts, f"empty accepts list in challenge: {challenge!r}"
    accepts_entry = accepts[0]
    payment_header = _stub_payment_header(accepts_entry)
    return client.post(path, json=body, headers={"PAYMENT-SIGNATURE": payment_header})


def _assert_verdict_shape(body: dict) -> None:
    """Common shape assertions for both basic and pro responses."""
    assert body.get("verdict") in _VALID_DECISIONS, (
        f"verdict {body.get('verdict')!r} not in {_VALID_DECISIONS!r}"
    )
    confidence = body.get("confidence")
    assert isinstance(confidence, (int, float)), f"confidence missing or wrong type: {confidence!r}"
    assert 0.0 <= float(confidence) <= 1.0, f"confidence out of range: {confidence!r}"

    # Intent payload surrogates: drivers + blockers + turns. The skill in
    # gecko-claude's examples/trading-oracle reads these to shape the
    # Kamino intent.
    turns = body.get("turns")
    assert isinstance(turns, list) and len(turns) >= 1, (
        f"expected >=1 panel turn (cite-bearing), got {turns!r}"
    )
    assert isinstance(body.get("key_drivers"), list)
    assert isinstance(body.get("blocker_questions"), list)
    assert isinstance(body.get("dissent_count"), int)


def test_basic_route_serves_402_and_settles_in_stub(http_client: httpx.Client) -> None:
    body = {
        "idea": _KAMINO_QUESTION,
        "protocol": _DEFAULT_PROTOCOL,
        "vertical": _DEFAULT_VERTICAL,
    }
    r = _paid_post(http_client, path="/trade_research", body=body)
    assert r.status_code == 200, f"basic settle failed: {r.status_code} {r.text}"
    _assert_verdict_shape(r.json())


def test_pro_route_advertises_higher_price_and_settles(http_client: httpx.Client) -> None:
    # First, validate the 402 advertises the pro price ($0.75 == 750_000 atomic USDC).
    r0 = http_client.post(
        "/trade_research/pro",
        json={
            "idea": _KAMINO_QUESTION,
            "protocol": _DEFAULT_PROTOCOL,
            "vertical": _DEFAULT_VERTICAL,
        },
    )
    assert r0.status_code == 402
    challenge = _decode_challenge(r0.headers["payment-required"])
    amounts = {entry.get("amount") for entry in challenge.get("accepts", [])}
    assert "750000" in amounts, f"pro route did not advertise $0.75: amounts={amounts!r}"

    # Then run the full settle dance.
    r = _paid_post(
        http_client,
        path="/trade_research/pro",
        body={
            "idea": _KAMINO_QUESTION,
            "protocol": _DEFAULT_PROTOCOL,
            "vertical": _DEFAULT_VERTICAL,
        },
    )
    assert r.status_code == 200, f"pro settle failed: {r.status_code} {r.text}"
    _assert_verdict_shape(r.json())


def test_well_known_advertises_both_routes(http_client: httpx.Client) -> None:
    """Sanity probe — the catalog must expose both endpoints to discovery agents."""
    r = http_client.get("/.well-known/x402")
    assert r.status_code == 200, r.text
    routes = {entry["route"]: entry for entry in r.json().get("routes", [])}
    assert "POST /trade_research" in routes
    assert "POST /trade_research/pro" in routes
