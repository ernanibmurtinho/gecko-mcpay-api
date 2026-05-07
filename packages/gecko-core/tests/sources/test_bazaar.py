"""S22-BAZAAR-INGEST-01 — bazaar_manifest contract tests.

Pattern C: recorded-fixture style. The default test run never touches
the live Bazaar endpoint; the canned catalog pinned 2026-05-07 is the
contract. The ``@pytest.mark.live`` test below is the drift detector,
opted into manually or by a weekly job.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from gecko_core.sources.bazaar_manifest import (
    BAZAAR_CATALOG_URL,
    BAZAAR_HOST,
    BAZAAR_SEARCH_URL,
    BazaarCatalog,
    BazaarPriceSummary,
    BazaarService,
    _build_search_url,
    _format_networks,
    _format_price_band,
    _is_bazaar_url,
    fetch_catalog,
    filter_neobank_relevant,
)

_CATALOG_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "bazaar_catalog_2026_05_07.json"


def _load_fixture_text() -> str:
    return _CATALOG_FIXTURE_PATH.read_text(encoding="utf-8")


def _load_fixture_dict() -> dict:
    return json.loads(_load_fixture_text())


# ---------------------------------------------------------------------------
# Pure-helper unit tests — no orchestrator firing, no http.
# ---------------------------------------------------------------------------


def test_is_bazaar_url_accepts_apex_https() -> None:
    assert _is_bazaar_url(BAZAAR_CATALOG_URL) is True
    assert _is_bazaar_url(BAZAAR_SEARCH_URL) is True
    assert _is_bazaar_url("https://api.agentic.market/anything") is True


def test_is_bazaar_url_rejects_other_hosts() -> None:
    assert _is_bazaar_url("https://evil.example.com/x") is False
    # apex without the api subdomain — Bazaar marketplace UI lives at
    # agentic.market, but the API only at api.agentic.market.
    assert _is_bazaar_url("https://agentic.market/x") is False
    assert _is_bazaar_url("https://api.agentic.market.evil.com/") is False
    assert _is_bazaar_url("http://api.agentic.market/x") is False  # http
    assert _is_bazaar_url("file:///etc/passwd") is False
    assert _is_bazaar_url("") is False


def test_format_networks_renders_expected_phrase() -> None:
    assert _format_networks(["Base", "Solana"]) == "Networks: Base, Solana."
    assert _format_networks(["Base"]) == "Networks: Base."
    assert _format_networks([]) == ""


def test_format_price_band_renders_expected_strings() -> None:
    free = BazaarPriceSummary.model_construct(
        minAmount="", maxAmount="", avgCostPerTransaction="", avgCostBasis="", currency=""
    )
    assert _format_price_band(free) == ""

    flat = BazaarPriceSummary.model_construct(
        minAmount="0.01",
        maxAmount="0.01",
        avgCostPerTransaction="0.01",
        avgCostBasis="exact",
        currency="USDC",
    )
    assert _format_price_band(flat) == "Pricing: $0.01 USDC."

    band = BazaarPriceSummary.model_construct(
        minAmount="0.001",
        maxAmount="0.015",
        avgCostPerTransaction="0.001",
        avgCostBasis="exact",
        currency="USDC",
    )
    out = _format_price_band(band)
    assert "$0.001" in out and "$0.015" in out and "USDC" in out
    assert out.endswith(".")

    assert _format_price_band(None) == ""


def test_build_search_url_construction() -> None:
    url = _build_search_url("kyc verification")
    assert url.startswith(BAZAAR_SEARCH_URL + "?")
    assert "q=kyc+verification" in url or "q=kyc%20verification" in url


def test_pinned_catalog_validates_against_pydantic_model() -> None:
    """Schema-drift guard: the pinned fixture parses cleanly."""
    raw = _load_fixture_dict()
    catalog = BazaarCatalog.model_validate(raw)
    assert catalog.services
    assert all(s.id for s in catalog.services)
    # 2026-05-07 pinned shape.
    assert catalog.limit == 50
    assert catalog.offset == 0
    assert catalog.total >= len(catalog.services)


def test_filter_neobank_relevant_pure_function() -> None:
    """Category AND keyword must both match; one alone is insufficient."""
    yes = BazaarService.model_construct(
        id="a-b",
        category="Data",
        description="real-time market prices and treasury balances",
        networks=[],
        endpoints=[],
    )
    no_category = BazaarService.model_construct(
        id="c-d",
        category="Travel",
        description="market prices",
        networks=[],
        endpoints=[],
    )
    no_keyword = BazaarService.model_construct(
        id="e-f",
        category="Search",
        description="generic web search",
        networks=[],
        endpoints=[],
    )
    keyword_in_data = BazaarService.model_construct(
        id="g-h",
        category="Data",
        description="kyc and identity verification flows",
        networks=[],
        endpoints=[],
    )
    out = filter_neobank_relevant([yes, no_category, no_keyword, keyword_in_data])
    ids = [s.id for s in out]
    assert ids == ["a-b", "g-h"]


# ---------------------------------------------------------------------------
# Pattern C contract test — fixture replay through fetch_catalog.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_catalog_fixture_normalizes_into_chunks() -> None:
    fixture_body = _load_fixture_text()
    fixture = _load_fixture_dict()
    expected_count = len(fixture["services"])

    async def _handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == BAZAAR_HOST
        assert request.url.path == "/v1/services"
        return httpx.Response(
            200,
            text=fixture_body,
            headers={"content-type": "application/json"},
        )

    transport = httpx.MockTransport(_handler)
    client = httpx.AsyncClient(transport=transport)

    result = await fetch_catalog(client=client)
    await client.aclose()

    assert result.fired is True
    assert result.cost_usd == 0.0
    chunks = result.payload["chunks"]
    assert len(chunks) == expected_count
    for chunk in chunks:
        assert chunk["provider_kind"] == "free:bazaar_manifest"
        assert chunk["text"]
        assert chunk["metadata"]["source_url"].startswith("https://agentic.market/services/")
        assert "networks" in chunk["metadata"]
    assert result.payload["service_count"] == expected_count
    assert result.payload["limit"] == 50


# ---------------------------------------------------------------------------
# SSRF guard.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ssrf_guard_rejects_non_bazaar_host() -> None:
    """SSRF guard — passing a non-Bazaar URL must short-circuit before
    any HTTP round-trip."""
    handler_called = {"v": False}

    async def _handler(request: httpx.Request) -> httpx.Response:
        handler_called["v"] = True
        return httpx.Response(200, text="{}")

    transport = httpx.MockTransport(_handler)
    client = httpx.AsyncClient(transport=transport)

    result = await fetch_catalog(client=client, catalog_url="https://evil.example.com/v1/services")
    await client.aclose()

    assert result.fired is False
    assert result.error is not None
    assert "SSRF" in result.error
    assert handler_called["v"] is False


# ---------------------------------------------------------------------------
# Live drift detector — skipped by default; opt in with `-m live`.
# ---------------------------------------------------------------------------


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_catalog_matches_pinned_shape() -> None:
    """Hit the live Bazaar endpoint and assert the shape still parses
    against the pinned pydantic model. Run by hand or in a weekly job."""
    async with httpx.AsyncClient(timeout=8.0, follow_redirects=False) as client:
        resp = await client.get(BAZAAR_CATALOG_URL)
    assert resp.status_code == 200
    catalog = BazaarCatalog.model_validate(resp.json())
    assert catalog.services, "live catalog reported zero services"
    assert all(s.id for s in catalog.services)
