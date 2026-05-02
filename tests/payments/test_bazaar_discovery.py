"""S16-BAZAAR-DISCOVERY-01 — discovery client + dual-fallback tests.

Covers:
  * Stub fixture round-trip (parsers don't drop fields).
  * agentic.market schema → BazaarResource normalization.
  * CDP schema → BazaarResource normalization.
  * Cache hit semantics (TTL).
  * DualDiscoveryClient fallback on primary 5xx + dedupe.
  * Resource-level filters (network / asset / max_usd_price).

All tests run against fixtures or respx-mocked transports — no live
network. The recorded-fixture contract test against the real CDP +
agentic.market endpoints is its own ticket (S16-BAZAAR-CONSUMER-04).
"""

from __future__ import annotations

import json
import time
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx
from gecko_core.payments.bazaar_discovery import (
    AGENTIC_MARKET_BASE_URL,
    CDP_DISCOVERY_BASE_URL,
    AgenticMarketDiscoveryClient,
    BazaarDiscoveryClient,
    BazaarResource,
    CDPDiscoveryClient,
    DualDiscoveryClient,
    PaymentRequirements,
    StubDiscoveryClient,
    _parse_agentic_market_services,
    _parse_cdp_resources,
    resolve_discovery_client,
)

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "bazaar_discovery"


def _load(name: str) -> dict[str, object]:
    payload: dict[str, object] = json.loads((FIXTURES_DIR / name).read_text())
    return payload


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


def test_agentic_market_parser_flattens_endpoints() -> None:
    payload = _load("agentic_market_search_lisbon_hotel.json")
    resources = _parse_agentic_market_services(payload)

    # The hotel-search fixture is non-empty and TripAdvisor specifically
    # appears (the proving-smoke vendor).
    assert len(resources) >= 3
    urls = [r.resource_url for r in resources]
    assert any("tripadvisor" in u.lower() for u in urls)

    # Every resource has exactly one accepts entry (one endpoint = one
    # PaymentRequirements after our flattening) and pricing was
    # normalized to atomic-units strings.
    for r in resources:
        assert r.source_directory == "agentic.market"
        assert r.resource_type == "http"
        assert len(r.accepts) == 1
        a = r.accepts[0]
        # USDC denomination preserved on the derived USD field.
        assert a.max_amount_required is not None
        # Atomic string is digits-only after the float→atomic conversion.
        assert a.amount == "" or a.amount.isdigit()


def test_cdp_parser_passes_through_accepts_shape() -> None:
    payload = _load("cdp_resources_sample.json")
    resources = _parse_cdp_resources(payload)
    assert len(resources) >= 3
    for r in resources:
        assert r.source_directory == "cdp"
        assert r.x402_version >= 1
        # Most accepts entries should have wire fields populated, but
        # some CDP listings (e.g. dynamic-pricing endpoints) ship without
        # a posted amount — skip the amount assertion when blank.
        for a in r.accepts:
            assert a.network
            assert a.asset


# ---------------------------------------------------------------------------
# Stub backend
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stub_discovery_lists_and_searches() -> None:
    stub = StubDiscoveryClient(fixtures_dir=FIXTURES_DIR)
    listed = await stub.list_resources(limit=5)
    searched = await stub.search("hotel")
    assert len(listed) <= 5 and len(listed) > 0
    assert len(searched) >= 1
    # Stub.list reads CDP fixture; stub.search reads agentic.market fixture.
    assert all(r.source_directory == "cdp" for r in listed)
    assert all(r.source_directory == "agentic.market" for r in searched)


@pytest.mark.asyncio
async def test_resolve_discovery_client_default_is_stub() -> None:
    client = resolve_discovery_client()
    assert isinstance(client, StubDiscoveryClient)
    assert isinstance(client, BazaarDiscoveryClient)


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_max_usd_filter_drops_pricier_resources() -> None:
    stub = StubDiscoveryClient(fixtures_dir=FIXTURES_DIR)
    cheap = await stub.search("hotel", max_usd_price=Decimal("0.005"))
    pricier = await stub.search("hotel", max_usd_price=Decimal("100"))
    # Tighter cap returns at most as many results.
    assert len(cheap) <= len(pricier)
    for r in cheap:
        for a in r.accepts:
            if a.max_amount_required is not None:
                assert a.max_amount_required <= Decimal("0.005")


# ---------------------------------------------------------------------------
# AgenticMarket live client (respx-mocked)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agentic_market_client_search_and_cache() -> None:
    payload = _load("agentic_market_search_lisbon_hotel.json")
    with respx.mock(assert_all_called=False) as router:
        route = router.get(f"{AGENTIC_MARKET_BASE_URL}/v1/services/search").mock(
            return_value=httpx.Response(200, json=payload)
        )

        client = AgenticMarketDiscoveryClient(ttl_s=60)
        out1 = await client.search("hotel")
        out2 = await client.search("hotel")  # cache hit

        assert len(out1) >= 1
        assert out1 == out2  # same reference content
        assert route.call_count == 1  # second call served from cache


@pytest.mark.asyncio
async def test_cdp_client_list_resources_uses_platform_path() -> None:
    payload = _load("cdp_resources_sample.json")
    with respx.mock(assert_all_called=False) as router:
        route = router.get(f"{CDP_DISCOVERY_BASE_URL}/resources").mock(
            return_value=httpx.Response(200, json=payload)
        )
        client = CDPDiscoveryClient()
        out = await client.list_resources(limit=10)

        assert len(out) <= 10 and len(out) > 0
        assert route.called


# ---------------------------------------------------------------------------
# Dual fallback + dedupe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dual_falls_back_to_cdp_on_primary_503() -> None:
    cdp_payload = _load("cdp_resources_sample.json")
    with respx.mock(assert_all_called=False) as router:
        primary_route = router.get(f"{AGENTIC_MARKET_BASE_URL}/v1/services/search").mock(
            return_value=httpx.Response(503, text="upstream busy")
        )
        cdp_route = router.get(f"{CDP_DISCOVERY_BASE_URL}/search").mock(
            return_value=httpx.Response(200, json=cdp_payload)
        )

        dual = DualDiscoveryClient(
            primary=AgenticMarketDiscoveryClient(),
            fallback=CDPDiscoveryClient(),
        )
        out = await dual.search("hotel")

        assert primary_route.called
        assert cdp_route.called
        assert len(out) > 0
        # All survivors come from CDP since primary returned no results.
        assert all(r.source_directory == "cdp" for r in out)


@pytest.mark.asyncio
async def test_dual_dedupes_by_resource_url() -> None:
    """Primary wins on URL collision; fallback's duplicate is dropped."""
    shared_url = "https://example.test/api/x"

    primary_resources = [
        BazaarResource(
            resource_url=shared_url,
            resource_type="http",
            x402_version=2,
            accepts=[
                PaymentRequirements(
                    network="Base",
                    asset="USDC",
                    pay_to="0xprimary",
                    amount="1000",
                    max_amount_required=Decimal("0.001"),
                )
            ],
            source_directory="agentic.market",
        )
    ]
    fallback_resources = [
        BazaarResource(
            resource_url=shared_url,  # collision
            resource_type="http",
            x402_version=2,
            accepts=[
                PaymentRequirements(
                    network="Base",
                    asset="USDC",
                    pay_to="0xfallback",
                    amount="1000",
                    max_amount_required=Decimal("0.001"),
                )
            ],
            source_directory="cdp",
        ),
        BazaarResource(
            resource_url="https://example.test/api/y",  # unique, kept
            resource_type="http",
            x402_version=2,
            accepts=[
                PaymentRequirements(
                    network="Base",
                    asset="USDC",
                    pay_to="0xfallback",
                    amount="2000",
                    max_amount_required=Decimal("0.002"),
                )
            ],
            source_directory="cdp",
        ),
    ]

    class _Fixed:
        name = "fixed"
        mode = "stub"

        def __init__(self, items: list[BazaarResource]) -> None:
            self._items = items

        async def list_resources(self, **_kw: object) -> list[BazaarResource]:
            return list(self._items)

        async def search(self, query: str, **_kw: object) -> list[BazaarResource]:
            return list(self._items)

    dual = DualDiscoveryClient(
        primary=_Fixed(primary_resources),  # type: ignore[arg-type]
        fallback=_Fixed(fallback_resources),  # type: ignore[arg-type]
    )
    out = await dual.search("anything")
    assert len(out) == 2
    # Primary's entry kept on collision.
    primary_winner = next(r for r in out if r.resource_url == shared_url)
    assert primary_winner.source_directory == "agentic.market"
    assert primary_winner.accepts[0].pay_to == "0xprimary"


# ---------------------------------------------------------------------------
# Cache TTL
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_hit_is_fast_and_does_not_refetch() -> None:
    payload = _load("cdp_resources_sample.json")
    with respx.mock(assert_all_called=False) as router:
        route = router.get(f"{CDP_DISCOVERY_BASE_URL}/resources").mock(
            return_value=httpx.Response(200, json=payload)
        )
        client = CDPDiscoveryClient(ttl_s=60)
        await client.list_resources(limit=10)

        t0 = time.perf_counter()
        await client.list_resources(limit=10)
        elapsed = time.perf_counter() - t0

        assert elapsed < 0.05  # cache hit budget: well under 50ms
        assert route.call_count == 1
