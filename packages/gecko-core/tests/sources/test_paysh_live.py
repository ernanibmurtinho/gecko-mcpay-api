"""S22-N4 — paysh_live contract tests.

Pattern C (CLAUDE.md): recorded-fixture style. The default test run never
spends USDC; the JSON fixtures pinned 2026-05-07 are the contract. The
``@pytest.mark.live_paysh`` tests below are the live-spend re-record path,
opted into manually by the operator (NEVER in CI).

Per ``feedback_lighter_tests.md``: the bulk of this module is pure-helper
unit tests (budget cap, redaction, fqn lookup, chunking). The Pattern-C
fixture-replay test exercises the integration seam — exactly one
``PaidRequester`` stand-in, no monkeypatching of httpx or the consumer.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from gecko_core.sources.paysh_live import (
    PAYSH_LIVE_BUDGET_USD,
    PROVIDER_KIND,
    SOURCE_NAME,
    BudgetExceeded,
    PaidResponse,
    ProviderHTTPError,
    ProviderNotFoundError,
    _build_chunks,
    _enforce_budget,
    _hash_body,
    _is_safe_https,
    _ledger_entry,
    _lookup_provider,
    _redact_secrets,
    _split_into_chunks,
    _stringify_response,
    fetch_paid,
)
from gecko_core.sources.paysh_manifest import PayshCatalogProvider

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


def test_redact_idempotent_on_clean_message() -> None:
    msg = "provider returned 500 with body 'service down'"
    assert _redact_secrets(msg) == msg


def test_is_safe_https_accepts_pay_sh_provider_urls() -> None:
    assert _is_safe_https("https://x402.api.agentmail.to") is True
    assert _is_safe_https("https://pro-api.coingecko.com/api/v3/x402/onchain") is True


def test_is_safe_https_rejects_non_https_and_private() -> None:
    assert _is_safe_https("http://x402.api.agentmail.to") is False
    assert _is_safe_https("https://localhost/x") is False
    assert _is_safe_https("https://10.0.0.1/x") is False
    assert _is_safe_https("https://127.0.0.1/x") is False
    assert _is_safe_https("file:///etc/passwd") is False
    assert _is_safe_https("") is False


def _make_provider(
    fqn: str = "agentmail/email",
    *,
    min_price_usd: float = 0.05,
    service_url: str = "https://x402.api.agentmail.to",
    title: str = "AgentMail",
    category: str = "messaging",
) -> PayshCatalogProvider:
    """Light fake using ``model_construct`` per ``feedback_lighter_tests``.

    Skips the validator path so tests don't accidentally exercise the
    pydantic schema beyond what the helper actually needs.
    """
    return PayshCatalogProvider.model_construct(
        fqn=fqn,
        title=title,
        description="",
        use_case="",
        category=category,
        service_url=service_url,
        endpoint_count=1,
        has_metering=True,
        has_free_tier=False,
        min_price_usd=min_price_usd,
        max_price_usd=min_price_usd,
        sha="",
    )


def test_lookup_provider_hit_and_miss() -> None:
    catalog = [_make_provider("a/b"), _make_provider("c/d")]
    assert _lookup_provider("a/b", catalog).fqn == "a/b"
    with pytest.raises(ProviderNotFoundError):
        _lookup_provider("nope/missing", catalog)


def test_enforce_budget_passes_within_envelope() -> None:
    cap = _enforce_budget(
        fqn="x/y",
        min_price_usd=0.01,
        max_cost_usd=0.10,
        remaining_budget_usd=5.0,
    )
    assert cap == 0.10  # caller cap is the binding constraint


def test_enforce_budget_caller_cap_below_min_price() -> None:
    with pytest.raises(BudgetExceeded, match="exceeds caller cap"):
        _enforce_budget(
            fqn="x/y",
            min_price_usd=0.01,
            max_cost_usd=0.001,
            remaining_budget_usd=5.0,
        )


def test_enforce_budget_sprint_envelope_exhausted() -> None:
    with pytest.raises(BudgetExceeded, match="exceeds remaining sprint budget"):
        _enforce_budget(
            fqn="x/y",
            min_price_usd=0.01,
            max_cost_usd=None,
            remaining_budget_usd=0.005,
        )


def test_enforce_budget_zero_caller_cap_rejected() -> None:
    with pytest.raises(BudgetExceeded, match="must be positive"):
        _enforce_budget(
            fqn="x/y",
            min_price_usd=0.0,
            max_cost_usd=0.0,
            remaining_budget_usd=5.0,
        )


def test_split_into_chunks_short_body_single_chunk() -> None:
    chunks = _split_into_chunks("just five short words here.")
    assert len(chunks) == 1


def test_split_into_chunks_long_body_segments() -> None:
    text = " ".join(["word"] * 1500)
    chunks = _split_into_chunks(text, words_per_chunk=500)
    assert len(chunks) == 3


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
    """Replays a JSON fixture as if it were a live x402 paid response.

    Narrow on purpose: one method, one fixture, one return shape. Tests
    that need different responses build a fresh instance per case.
    """

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
    """The contract test: replay the agentmail fixture, assert chunks +
    ledger emitted with the canonical ``source="paysh_live"`` literal."""
    fixture = _FIXTURE_DIR / "paysh_live_agentmail_email_2026_05_07.json"
    requester = _FixtureReplayRequester(fixture)
    provider = _make_provider(
        fqn="agentmail/email",
        min_price_usd=0.05,
        service_url="https://x402.api.agentmail.to",
        title="AgentMail",
        category="messaging",
    )

    result = await fetch_paid(
        "agentmail/email",
        "neobank kyc verification inbox",
        x402_client=requester,
        catalog_providers=[provider],
        max_cost_usd=0.10,
        remaining_budget_usd=PAYSH_LIVE_BUDGET_USD,
    )

    assert result.source_name == SOURCE_NAME == "paysh_live"
    assert result.fired is True
    chunks = result.payload["chunks"]
    assert len(chunks) >= 1
    assert all(c["provider_kind"] == PROVIDER_KIND for c in chunks)
    assert all(c["metadata"]["fqn"] == "agentmail/email" for c in chunks)
    ledger = result.payload["ledger"]
    assert ledger["provider"] == "agentmail/email"
    assert ledger["cost_usd"] > 0
    assert ledger["sha"]
    # Budget decremented correctly.
    assert result.payload["remaining_budget_usd"] == pytest.approx(
        PAYSH_LIVE_BUDGET_USD - ledger["cost_usd"]
    )


@pytest.mark.asyncio
async def test_budget_cap_aborts_before_request() -> None:
    """Set ``max_cost_usd=0.001`` against a provider whose
    ``min_price_usd=0.01``; assert no x402 call is issued and
    ``BudgetExceeded`` is raised."""

    class _ExplodingRequester:
        called = False

        async def request(self, **_: Any) -> PaidResponse:
            type(self).called = True
            raise AssertionError("request should never be called when budget gate fails")

    requester = _ExplodingRequester()
    provider = _make_provider(min_price_usd=0.01)

    with pytest.raises(BudgetExceeded):
        await fetch_paid(
            "agentmail/email",
            "q",
            x402_client=requester,
            catalog_providers=[provider],
            max_cost_usd=0.001,
        )
    assert _ExplodingRequester.called is False


@pytest.mark.asyncio
async def test_redaction_of_signature_in_error() -> None:
    """Force a 500 response carrying a base58 signature in the body;
    assert the raised error scrubs the signature substring."""
    leaking_sig = (
        "5VERa3rqVj9XJK4hMAGhVkLxCqZJ4qy3PeSr7gN4XpYvDtBxNm9F8sZK6yCuRq3aE2WjXkLHbT8DfMpQrSnUvWxZ"
    )

    class _500Requester:
        async def request(self, **_: Any) -> PaidResponse:
            text = f"server error; settlement signature was {leaking_sig} aborting"
            return PaidResponse(
                status_code=500,
                cost_usd=0.05,
                response_body={"error": "internal", "sig": leaking_sig},
                response_text=text,
                content_type="application/json",
                tx_signature=leaking_sig,
                response_sha=_hash_body(text),
                headers={"X-Trace-Sig": leaking_sig},
            )

    provider = _make_provider(min_price_usd=0.05)
    with pytest.raises(ProviderHTTPError) as exc_info:
        await fetch_paid(
            "agentmail/email",
            "q",
            x402_client=_500Requester(),
            catalog_providers=[provider],
        )
    assert leaking_sig not in str(exc_info.value)


# ---------------------------------------------------------------------------
# Pure-helper coverage for the chunk + ledger renderers.
# ---------------------------------------------------------------------------


def test_build_chunks_emits_paysh_live_kind_and_citation() -> None:
    provider = _make_provider()
    body = {"narrative": "hello " * 200}
    text = json.dumps(body)
    response = PaidResponse(
        status_code=200,
        cost_usd=0.05,
        response_body=body,
        response_text=text,
        content_type="application/json",
        tx_signature="sig",
        response_sha=_hash_body(text),
    )
    chunks = _build_chunks(fqn="agentmail/email", provider=provider, response=response, query="q")
    assert chunks
    assert all(c["provider_kind"] == PROVIDER_KIND for c in chunks)
    assert all(c["metadata"]["citation_id"] == "paysh_live:agentmail/email" for c in chunks)


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
    entry = _ledger_entry(fqn="x/y", response=response)
    assert entry["provider"] == "x/y"
    assert entry["cost_usd"] == 0.01
    assert entry["sha"] == _hash_body(text)
    assert entry["status"] == 200


# ---------------------------------------------------------------------------
# Live spend tests — opted-in only via `pytest -m live_paysh`. NEVER in CI.
# Operator command:
#     pytest -m live_paysh packages/gecko-core/tests/sources/test_paysh_live.py
# Total spend cap across these three: $0.50.
# ---------------------------------------------------------------------------


@pytest.mark.live_paysh
@pytest.mark.asyncio
async def test_live_paysh_agentmail_email() -> None:
    """Live x402 spend against agentmail/email. Records the response to
    ``fixtures/paysh_live_agentmail_email_2026_05_07.json``."""
    pytest.skip(
        "live_paysh marker — operator records this manually after wiring "
        "X402_MODE=live and a funded buyer wallet. Replace the placeholder "
        "fixture with the captured PaidResponse JSON."
    )


@pytest.mark.live_paysh
@pytest.mark.asyncio
async def test_live_paysh_stablecrypto_market_data() -> None:
    """Live x402 spend against merit-systems/stablecrypto/market-data."""
    pytest.skip(
        "live_paysh marker — operator records this manually after wiring "
        "X402_MODE=live and a funded buyer wallet."
    )


@pytest.mark.live_paysh
@pytest.mark.asyncio
async def test_live_paysh_paysponge_coingecko() -> None:
    """Live x402 spend against paysponge/coingecko (free-tier provider —
    may settle at $0)."""
    pytest.skip(
        "live_paysh marker — operator records this manually after wiring "
        "X402_MODE=live and a funded buyer wallet."
    )
