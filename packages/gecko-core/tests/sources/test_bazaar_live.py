"""S22-BAZAAR-INGEST-01 — bazaar_live contract tests.

Pattern C: recorded-fixture style. The default test run never spends
USDC; the JSON fixtures pinned 2026-05-07 are the contract. The
``@pytest.mark.live_bazaar`` tests below are the live-spend re-record
path, opted into manually by the operator (NEVER in CI).

Per ``feedback_lighter_tests.md``: the bulk of this module is pure-helper
unit tests (budget cap, redaction, service lookup, chunking). The
Pattern-C fixture-replay test exercises the integration seam — exactly
one ``PaidRequester`` stand-in, no monkeypatching of httpx or the
consumer.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from gecko_core.sources.bazaar_live import (
    BAZAAR_LIVE_BUDGET_USD,
    PROVIDER_KIND,
    SOURCE_NAME,
    BudgetExceeded,
    PaidResponse,
    ServiceHTTPError,
    ServiceNotFoundError,
    _build_chunks,
    _endpoint_min_price_usd,
    _enforce_budget,
    _hash_body,
    _is_safe_https,
    _ledger_entry,
    _lookup_service,
    _redact_secrets,
    _select_endpoint,
    _split_into_chunks,
    _stringify_response,
    fetch_paid,
)
from gecko_core.sources.bazaar_manifest import BazaarEndpoint, BazaarPricing, BazaarService

_FIXTURE_DIR = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Pure helper unit tests — no orchestrator firing, no http, no fixtures.
# ---------------------------------------------------------------------------


def test_redact_signature_solana_base58() -> None:
    sig = "5VERa3rqVj9XJK4hMAGhVkLxCqZJ4qy3PeSr7gN4XpYvDtBxNm9F8sZK6yCuRq3aE2WjXkLHbT8DfMpQrSnUvWxZ"
    msg = f"oh no the tx {sig} failed"
    redacted = _redact_secrets(msg)
    assert sig not in redacted
    assert "<redacted>" in redacted


def test_redact_bearer_and_apitoken() -> None:
    msg = "called with Authorization: Bearer abc.def-ghi and apiToken=xyz_123_long"
    redacted = _redact_secrets(msg)
    assert "abc.def-ghi" not in redacted
    assert "xyz_123_long" not in redacted


def test_is_safe_https_accepts_bazaar_provider_urls() -> None:
    assert _is_safe_https("https://api.exa.ai/search") is True
    assert _is_safe_https("https://api.anthropic.com/v1/messages") is True


def test_is_safe_https_rejects_non_https_and_private() -> None:
    assert _is_safe_https("http://api.exa.ai/search") is False
    assert _is_safe_https("https://localhost/x") is False
    assert _is_safe_https("https://10.0.0.1/x") is False
    assert _is_safe_https("file:///etc/passwd") is False
    assert _is_safe_https("") is False


def _make_endpoint(
    url: str = "https://api.exa.ai/search",
    *,
    min_amount: str = "0.007",
) -> BazaarEndpoint:
    pricing = BazaarPricing.model_construct(
        amount=min_amount,
        currency="USDC",
        network="Base",
        scheme="exact",
        maxAmount="",
        minAmount=min_amount,
    )
    return BazaarEndpoint.model_construct(
        url=url,
        description="search endpoint",
        method="POST",
        pricing=pricing,
    )


def _make_service(
    sid: str = "exa-ai",
    *,
    endpoint_url: str = "https://api.exa.ai/search",
    min_amount: str = "0.007",
    category: str = "Search",
) -> BazaarService:
    """Light fake using ``model_construct``."""
    return BazaarService.model_construct(
        id=sid,
        name="Exa",
        description="AI-powered web search + content retrieval",
        domain="exa.ai",
        provider="exa.ai",
        providerUrl="",
        category=category,
        networks=["Base"],
        enriched=True,
        endpoints=[_make_endpoint(endpoint_url, min_amount=min_amount)],
        integrationType="",
        isNew=False,
        priceSummary=None,
    )


def test_lookup_service_hit_and_miss() -> None:
    catalog = [_make_service("a-b"), _make_service("c-d")]
    assert _lookup_service("a-b", catalog).id == "a-b"
    with pytest.raises(ServiceNotFoundError):
        _lookup_service("nope", catalog)


def test_select_endpoint_picks_first_with_url() -> None:
    svc = _make_service()
    ep = _select_endpoint(svc)
    assert ep.url == "https://api.exa.ai/search"


def test_select_endpoint_raises_when_no_usable_endpoints() -> None:
    svc = BazaarService.model_construct(id="x", endpoints=[])
    with pytest.raises(ServiceNotFoundError):
        _select_endpoint(svc)


def test_endpoint_min_price_parses_string_amount() -> None:
    ep = _make_endpoint(min_amount="0.007")
    assert _endpoint_min_price_usd(ep) == pytest.approx(0.007)


def test_endpoint_min_price_handles_no_pricing() -> None:
    ep = BazaarEndpoint.model_construct(url="https://x.example.com/y", pricing=None)
    assert _endpoint_min_price_usd(ep) == 0.0


def test_enforce_budget_passes_within_envelope() -> None:
    cap = _enforce_budget(
        service_id="x", min_price_usd=0.01, max_cost_usd=0.10, remaining_budget_usd=5.0
    )
    assert cap == 0.10


def test_enforce_budget_caller_cap_below_min_price() -> None:
    with pytest.raises(BudgetExceeded, match="exceeds caller cap"):
        _enforce_budget(
            service_id="x", min_price_usd=0.01, max_cost_usd=0.001, remaining_budget_usd=5.0
        )


def test_enforce_budget_sprint_envelope_exhausted() -> None:
    with pytest.raises(BudgetExceeded, match="exceeds remaining sprint budget"):
        _enforce_budget(
            service_id="x", min_price_usd=0.01, max_cost_usd=None, remaining_budget_usd=0.005
        )


def test_split_into_chunks_short_body_single_chunk() -> None:
    assert len(_split_into_chunks("just five short words here.")) == 1


def test_split_into_chunks_long_body_segments() -> None:
    text = " ".join(["word"] * 1500)
    assert len(_split_into_chunks(text, words_per_chunk=500)) == 3


def test_stringify_response_json_dict_pretty_prints() -> None:
    out = _stringify_response({"a": 1, "b": "x"}, fallback_text="fallback")
    assert "a" in out and "b" in out
    assert "fallback" not in out


def test_stringify_response_string_passthrough() -> None:
    assert _stringify_response("hello world", fallback_text="x") == "hello world"


# ---------------------------------------------------------------------------
# Pattern C — recorded-fixture replay against a stand-in PaidRequester.
# ---------------------------------------------------------------------------


class _FixtureReplayRequester:
    """Replays a JSON fixture as if it were a live x402 paid response."""

    def __init__(self, fixture_path: Path) -> None:
        self._data: dict[str, Any] = json.loads(fixture_path.read_text(encoding="utf-8"))

    async def request(
        self,
        *,
        url: str,
        query: str,
        max_cost_usd: float,
        timeout_seconds: float,
    ) -> PaidResponse:
        resp = self._data["response"]
        body = resp["body"]
        text = json.dumps(body)
        return PaidResponse(
            status_code=int(resp["status_code"]),
            cost_usd=float(resp["cost_usd"]),
            response_body=body,
            response_text=text,
            content_type=str(resp["content_type"]),
            tx_signature=resp.get("tx_signature"),
            response_sha=_hash_body(text),
            headers={},
        )


@pytest.mark.asyncio
async def test_paid_call_against_recorded_fixture() -> None:
    """The contract test: replay the exa-ai fixture, assert chunks +
    ledger emitted with the canonical ``source="bazaar_live"`` literal."""
    fixture = _FIXTURE_DIR / "bazaar_live_exa_ai_2026_05_07.json"
    requester = _FixtureReplayRequester(fixture)
    service = _make_service()

    result = await fetch_paid(
        "exa-ai",
        "neobank market intelligence",
        x402_client=requester,
        catalog_services=[service],
        max_cost_usd=0.10,
        remaining_budget_usd=BAZAAR_LIVE_BUDGET_USD,
    )

    assert result.source_name == SOURCE_NAME == "bazaar_live"
    assert result.fired is True
    chunks = result.payload["chunks"]
    assert len(chunks) >= 1
    assert all(c["provider_kind"] == PROVIDER_KIND for c in chunks)
    assert all(c["metadata"]["id"] == "exa-ai" for c in chunks)
    ledger = result.payload["ledger"]
    assert ledger["provider"] == "exa-ai"
    assert ledger["cost_usd"] > 0
    assert ledger["sha"]
    assert result.payload["remaining_budget_usd"] == pytest.approx(
        BAZAAR_LIVE_BUDGET_USD - ledger["cost_usd"]
    )


@pytest.mark.asyncio
async def test_budget_cap_aborts_before_request() -> None:
    """Caller cap below min_price ⇒ no x402 call, ``BudgetExceeded`` raised."""

    class _ExplodingRequester:
        called = False

        async def request(self, **_: Any) -> PaidResponse:
            type(self).called = True
            raise AssertionError("request should never be called when budget gate fails")

    requester = _ExplodingRequester()
    service = _make_service(min_amount="0.01")

    with pytest.raises(BudgetExceeded):
        await fetch_paid(
            "exa-ai",
            "q",
            x402_client=requester,
            catalog_services=[service],
            max_cost_usd=0.001,
        )
    assert _ExplodingRequester.called is False


@pytest.mark.asyncio
async def test_redaction_of_signature_in_error() -> None:
    """Force a 500 response carrying a base58 signature; assert the
    raised error scrubs the signature substring."""
    leaking_sig = (
        "5VERa3rqVj9XJK4hMAGhVkLxCqZJ4qy3PeSr7gN4XpYvDtBxNm9F8sZK6yCuRq3aE2WjXkLHbT8DfMpQrSnUvWxZ"
    )

    class _500Requester:
        async def request(self, **_: Any) -> PaidResponse:
            text = f"server error; settlement signature was {leaking_sig} aborting"
            return PaidResponse(
                status_code=500,
                cost_usd=0.007,
                response_body={"error": "internal", "sig": leaking_sig},
                response_text=text,
                content_type="application/json",
                tx_signature=leaking_sig,
                response_sha=_hash_body(text),
                headers={"X-Trace-Sig": leaking_sig},
            )

    service = _make_service()
    with pytest.raises(ServiceHTTPError) as exc_info:
        await fetch_paid(
            "exa-ai",
            "q",
            x402_client=_500Requester(),
            catalog_services=[service],
        )
    assert leaking_sig not in str(exc_info.value)


# ---------------------------------------------------------------------------
# Pure-helper coverage for the chunk + ledger renderers.
# ---------------------------------------------------------------------------


def test_build_chunks_emits_bazaar_live_kind_and_citation() -> None:
    service = _make_service()
    endpoint = _select_endpoint(service)
    body = {"narrative": "hello " * 200}
    text = json.dumps(body)
    response = PaidResponse(
        status_code=200,
        cost_usd=0.007,
        response_body=body,
        response_text=text,
        content_type="application/json",
        tx_signature="sig",
        response_sha=_hash_body(text),
    )
    chunks = _build_chunks(
        service_id="exa-ai", service=service, endpoint=endpoint, response=response, query="q"
    )
    assert chunks
    assert all(c["provider_kind"] == PROVIDER_KIND for c in chunks)
    assert all(c["metadata"]["citation_id"] == "bazaar_live:exa-ai" for c in chunks)


def test_ledger_entry_shape() -> None:
    text = "ok"
    response = PaidResponse(
        status_code=200,
        cost_usd=0.01,
        response_body=text,
        response_text=text,
        content_type="text/plain",
        tx_signature="sig",
        response_sha=_hash_body(text),
    )
    entry = _ledger_entry(service_id="exa-ai", response=response)
    assert entry["provider"] == "exa-ai"
    assert entry["cost_usd"] == 0.01
    assert entry["sha"] == _hash_body(text)
    assert entry["status"] == 200


# ---------------------------------------------------------------------------
# Live spend tests — opted-in only via `pytest -m live_bazaar`. NEVER in CI.
# Operator command:
#     pytest -m live_bazaar packages/gecko-core/tests/sources/test_bazaar_live.py
# Total spend cap across these: $0.50.
# ---------------------------------------------------------------------------


@pytest.mark.live_bazaar
@pytest.mark.asyncio
async def test_live_bazaar_exa_ai() -> None:
    """Live x402 spend against exa-ai. Records the response to
    ``fixtures/bazaar_live_exa_ai_2026_05_07.json``."""
    pytest.skip(
        "live_bazaar marker — operator records this manually after wiring "
        "X402_MODE=live and a funded buyer wallet on Base. Replace the "
        "placeholder fixture with the captured PaidResponse JSON."
    )
