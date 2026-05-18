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

Issue #15 (2026-05-09) + S35-#99 (2026-05-17): both basic and pro must
carry two top-level cite arrays — ``evidence_citations`` (protocol/
market data) and ``framework_context`` (investor-canon) — each with
structured ``{id, source, url, chunk_id, provider_kind, freshness_tier,
snippet}`` entries. The inline ``[N]`` markers inside ``turns[].content``
keep working. S35-#99 is a clean break: the legacy single ``citations``
key is gone. ``_assert_verdict_shape`` enforces this.

Issue #14 (2026-05-09): the pro envelope MUST carry a ``backtest``
field that the basic envelope does not. The dogfood that surfaced the
regression showed pro returning structurally identical wire envelopes
(same top-level keys, +1 cite ref). The pro smoke now asserts the
divergence at the wire level — ``pro.keys() != basic.keys()`` AND
``backtest`` is present on pro — so any future drift back to
shape-equivalence is caught at the contract layer.
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
_DEFAULT_VERTICAL = "dex"  # must match VERTICALS in gecko_core.knowledge.taxonomy; "defi-trading" is not valid and made retrieval return 0 hits silently (see PR #13)
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


_REQUIRED_CITATION_KEYS: frozenset[str] = frozenset(
    {"id", "source", "url", "chunk_id", "provider_kind", "freshness_tier", "snippet"}
)


def _assert_citation_shape(citations: list[dict]) -> None:
    """Issue #15 — every entry must carry the structured citation contract."""
    for entry in citations:
        assert isinstance(entry, dict), f"non-dict citation: {entry!r}"
        missing = _REQUIRED_CITATION_KEYS - set(entry.keys())
        assert not missing, f"citation missing keys {missing!r}: {entry!r}"


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

    # S35-#99: the verdict envelope splits citations into two top-level
    # lists — evidence_citations ("the data": protocol/market chunks) and
    # framework_context ("the lens": investor-canon chunks). Both carry the
    # same id/source/url/chunk_id/provider_kind/freshness_tier/snippet
    # contract. The kamino+dex smoke is expected to land >= 1 hit across
    # the two lists combined post-#16 ingest backfill.
    evidence_citations = body.get("evidence_citations")
    framework_context = body.get("framework_context")
    assert isinstance(evidence_citations, list), (
        f"missing evidence_citations[]: {evidence_citations!r}"
    )
    assert isinstance(framework_context, list), (
        f"missing framework_context[]: {framework_context!r}"
    )
    assert "citations" not in body, (
        "S35-#99 clean break: legacy citations[] must not be on the wire"
    )
    assert len(evidence_citations) + len(framework_context) >= 1, (
        "expected >= 1 structured citation across evidence_citations + "
        f"framework_context; got evidence={evidence_citations!r} "
        f"framework={framework_context!r} — regression or empty corpus"
    )
    _assert_citation_shape(evidence_citations)
    _assert_citation_shape(framework_context)


def test_basic_route_serves_402_and_settles_in_stub(http_client: httpx.Client) -> None:
    body = {
        "idea": _KAMINO_QUESTION,
        "protocol": _DEFAULT_PROTOCOL,
        "vertical": _DEFAULT_VERTICAL,
    }
    r = _paid_post(http_client, path="/trade_research", body=body)
    assert r.status_code == 200, f"basic settle failed: {r.status_code} {r.text}"
    basic = r.json()
    _assert_verdict_shape(basic)
    # Issue #14 — basic must NOT carry backtest. response_model_exclude_none=True
    # on the basic route strips the field from the wire.
    assert "backtest" not in basic, (
        f"basic envelope unexpectedly carries 'backtest': {basic.get('backtest')!r}"
    )


def test_pro_envelope_diverges_from_basic_with_backtest(http_client: httpx.Client) -> None:
    """Issue #14 AC — pro and basic must NOT be structurally identical."""
    body = {
        "idea": _KAMINO_QUESTION,
        "protocol": _DEFAULT_PROTOCOL,
        "vertical": _DEFAULT_VERTICAL,
    }
    rb = _paid_post(http_client, path="/trade_research", body=body)
    rp = _paid_post(http_client, path="/trade_research/pro", body=body)
    assert rb.status_code == 200 and rp.status_code == 200
    basic, pro = rb.json(), rp.json()

    assert set(pro.keys()) != set(basic.keys()), (
        f"issue #14 regression: pro and basic returned identical keys.\n"
        f"basic={sorted(basic.keys())!r}\npro={sorted(pro.keys())!r}"
    )
    assert pro.get("backtest") is not None, (
        f"pro envelope missing non-null 'backtest': {pro.get('backtest')!r}"
    )
    bt = pro["backtest"]
    assert isinstance(bt, dict)
    for required in ("pnl_pct", "drawdown_pct", "source", "unbacktestable"):
        assert required in bt, f"backtest missing required key {required!r}: {bt!r}"


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
