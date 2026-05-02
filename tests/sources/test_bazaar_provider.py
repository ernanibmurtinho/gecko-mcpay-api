"""S16-BAZAAR-CONSUMER-03 — BazaarSourceProvider scaffold tests.

The acceptance criterion for this ticket is the **two-vertical test**:
the same code path picks a hospitality service for a hotel idea and a
market-data service for a finance idea — with no shims, no vendor knobs.
That's the catalog-led principle landing.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import httpx
import pytest
from gecko_core.payments.bazaar_discovery import (
    BazaarDiscoveryClient,
    BazaarResource,
    PaymentRequirements,
)
from gecko_core.payments.x402_consumer import StubX402Consumer, X402Consumer
from gecko_core.sources.bazaar.adapters.generic import (
    GenericBazaarAdapter,
    _chunks_from_json,
    _chunks_from_text,
)
from gecko_core.sources.bazaar.provider import BazaarSourceProvider
from gecko_core.sources.bazaar.types import BazaarChunk

# ---------------------------------------------------------------------------
# Fakes — explicit, in-test fixtures (the recorded VCR cassettes from
# tests/payments/fixtures/bazaar_discovery/ are exercised by the discovery
# package's own tests; here we test composition).
# ---------------------------------------------------------------------------


def _req(amount_usd: str, *, network: str = "Base") -> PaymentRequirements:
    """USD-priced PaymentRequirements with derived USD field populated."""
    return PaymentRequirements(
        network=network,
        asset="USDC",
        pay_to="0xpaytothistest",
        amount=str(int(Decimal(amount_usd) * Decimal(10) ** 6)),
        max_amount_required=Decimal(amount_usd),
    )


def _resource(
    *,
    url: str,
    rtype: str,
    accepts: list[PaymentRequirements],
    score: float = 0.0,
) -> BazaarResource:
    return BazaarResource(
        resource_url=url,
        resource_type=rtype,
        x402_version=2,
        accepts=accepts,
        metadata={"score": score},
        source_directory="agentic.market",
    )


class FakeDiscovery:
    """Deterministic discovery: returns whichever resources the test seeded.

    Selection mimics the catalog-led behavior — different queries yield
    different services, no code-path divergence.
    """

    name = "fake-discovery"
    mode = "stub"

    def __init__(self, resources_by_query: dict[str, list[BazaarResource]]) -> None:
        self._by_query = resources_by_query

    async def list_resources(
        self,
        *,
        network: str | None = None,
        asset: str | None = None,
        max_usd_price: Decimal | None = None,
        limit: int = 20,
    ) -> list[BazaarResource]:
        return []

    async def search(
        self,
        query: str,
        *,
        network: str | None = None,
        asset: str | None = None,
        max_usd_price: Decimal | None = None,
    ) -> list[BazaarResource]:
        for seed_query, resources in self._by_query.items():
            if seed_query in query.lower():
                return list(resources)
        return []


class CountingStubConsumer(StubX402Consumer):
    """Stub consumer that records pay() calls for assertion."""

    def __init__(self) -> None:
        self.calls: list[tuple[PaymentRequirements, Decimal]] = []

    async def pay(self, requirements: PaymentRequirements, *, max_usd: Decimal) -> Any:
        self.calls.append((requirements, max_usd))
        return await super().pay(requirements, max_usd=max_usd)


def _mock_transport(
    handler: Any,  # Callable[[httpx.Request], httpx.Response]
) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------------------
# Acceptance: two verticals, same code path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_verticals_same_codepath_no_shims() -> None:
    """Travel idea picks hospitality; finance idea picks market-data — same code."""
    hotel_resource = _resource(
        url="https://example.com/api/v1/search",
        rtype="hotel-search",
        accepts=[_req("0.05")],
        score=0.9,
    )
    market_resource = _resource(
        url="https://www.example.com/api/v1/quote",
        rtype="market-data",
        accepts=[_req("0.10")],
        score=0.8,
    )

    discovery = FakeDiscovery(
        {
            "lisbon hotel": [hotel_resource],
            "real-time options pricing": [market_resource],
        }
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if "X-PAYMENT" not in request.headers:
            return httpx.Response(402, json={"error": "payment required"})
        items = [{"id": i, "name": f"item-{i}"} for i in range(5)]
        return httpx.Response(
            200,
            json=items,
            headers={"content-type": "application/json"},
        )

    async with _mock_transport(handler) as http_client:
        adapter = GenericBazaarAdapter(http_client=http_client)
        consumer = CountingStubConsumer()
        provider = BazaarSourceProvider(
            discovery_client=discovery,
            x402_consumer=consumer,
            adapter_registry=[adapter],
        )

        result_a = await provider.fetch(
            idea="Lisbon hotel for digital nomads",
            categories={"travel", "hospitality"},
        )
        assert result_a.fired, result_a.error
        chunks_a = result_a.payload["chunks"]
        assert len(chunks_a) >= 3
        assert chunks_a[0]["provider_kind"] == "bazaar:hotel-search"
        assert result_a.payload["picked_resources"] == [hotel_resource.resource_url]

        result_b = await provider.fetch(
            idea="real-time options pricing",
            categories={"finance"},
        )
        assert result_b.fired, result_b.error
        chunks_b = result_b.payload["chunks"]
        assert len(chunks_b) >= 3
        assert chunks_b[0]["provider_kind"] == "bazaar:market-data"
        assert result_b.payload["picked_resources"] == [market_resource.resource_url]

        # Catalog-led check: both fetches paid once via the same single
        # GenericBazaarAdapter instance — no shim was invoked.
        assert len(consumer.calls) == 2


# ---------------------------------------------------------------------------
# SSRF defense
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ssrf_unsafe_resource_is_skipped() -> None:
    bad = _resource(
        url="http://127.0.0.1/foo",
        rtype="hotel-search",
        accepts=[_req("0.01")],
        score=0.9,
    )
    good = _resource(
        url="https://example.com/api/v1/search",
        rtype="hotel-search",
        accepts=[_req("0.01")],
        score=0.5,
    )
    discovery = FakeDiscovery({"hotel": [bad, good]})

    def handler(request: httpx.Request) -> httpx.Response:
        # Should never see a request to 127.0.0.1.
        assert request.url.host != "127.0.0.1"
        if "X-PAYMENT" not in request.headers:
            return httpx.Response(402)
        return httpx.Response(
            200,
            json=[{"x": 1}],
            headers={"content-type": "application/json"},
        )

    async with _mock_transport(handler) as http_client:
        provider = BazaarSourceProvider(
            discovery_client=discovery,
            x402_consumer=StubX402Consumer(),
            adapter_registry=[GenericBazaarAdapter(http_client=http_client)],
        )
        result = await provider.fetch(idea="hotel", categories={"travel"})
        assert result.fired
        assert result.payload["picked_resources"] == [good.resource_url]


# ---------------------------------------------------------------------------
# Cap enforcement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_over_budget_resource_is_skipped() -> None:
    expensive = _resource(
        url="https://www.example.com/api",
        rtype="market-data",
        accepts=[_req("1.00")],
        score=0.9,
    )
    discovery = FakeDiscovery({"finance": [expensive]})

    consumer = CountingStubConsumer()
    provider = BazaarSourceProvider(
        discovery_client=discovery,
        x402_consumer=consumer,
        session_cap_usd=Decimal("0.50"),
    )
    result = await provider.fetch(idea="finance", categories={"finance"})
    assert not result.fired
    assert "no eligible Bazaar candidates" in (result.error or "")
    assert consumer.calls == []
    assert any("no-eligible" in s for s in result.payload.get("degraded_sources", []))


# ---------------------------------------------------------------------------
# GenericBazaarAdapter normalization unit tests
# ---------------------------------------------------------------------------


def _bare_resource() -> BazaarResource:
    return _resource(
        url="https://example.com/api",
        rtype="json-feed",
        accepts=[],
    )


def test_generic_normalize_json_array() -> None:
    chunks = _chunks_from_json(
        [{"i": 1}, {"i": 2}, {"i": 3}],
        resource=_bare_resource(),
        cost_usd=Decimal("0.01"),
    )
    assert len(chunks) == 3
    assert all(c.provider_kind == "bazaar:json-feed" for c in chunks)
    assert json.loads(chunks[0].text) == {"i": 1}


def test_generic_normalize_json_object_with_array_field() -> None:
    chunks = _chunks_from_json(
        {"meta": "ok", "results": [{"a": 1}, {"a": 2}]},
        resource=_bare_resource(),
        cost_usd=Decimal("0.01"),
    )
    assert len(chunks) == 2
    assert json.loads(chunks[0].text) == {"a": 1}


def test_generic_normalize_plain_json_object() -> None:
    chunks = _chunks_from_json(
        {"hello": "world"},
        resource=_bare_resource(),
        cost_usd=Decimal("0.01"),
    )
    assert len(chunks) == 1
    assert json.loads(chunks[0].text) == {"hello": "world"}


def test_generic_normalize_text_paragraphs() -> None:
    chunks = _chunks_from_text(
        "para one\n\npara two\n\n\npara three",
        resource=_bare_resource(),
        cost_usd=Decimal("0.01"),
    )
    texts = [c.text for c in chunks]
    assert texts == ["para one", "para two", "para three"]


@pytest.mark.asyncio
async def test_generic_unknown_content_type_returns_empty(caplog: Any) -> None:
    resource = _resource(
        url="https://example.com/binary",
        rtype="blob",
        accepts=[],
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"\x00\x01\x02",
            headers={"content-type": "application/octet-stream"},
        )

    async with _mock_transport(handler) as http_client:
        adapter = GenericBazaarAdapter(http_client=http_client)
        with caplog.at_level("WARNING"):
            chunks = await adapter.fetch_and_normalize(
                resource, StubX402Consumer(), max_usd=Decimal("0.50")
            )
    assert chunks == []
    assert any("unsupported content-type" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_provider_conforms_to_source_protocol() -> None:
    from gecko_core.sources import Source

    provider = BazaarSourceProvider(
        discovery_client=FakeDiscovery({}),
        x402_consumer=StubX402Consumer(),
    )
    assert isinstance(provider, Source)


def test_discovery_and_consumer_protocols_runtime_checkable() -> None:
    assert isinstance(FakeDiscovery({}), BazaarDiscoveryClient)
    assert isinstance(StubX402Consumer(), X402Consumer)


# ---------------------------------------------------------------------------
# S16-BAZAAR-CONSUMER-02 — daily-cap ledger wiring
# ---------------------------------------------------------------------------


class _FakeLedger:
    """Test stand-in for ``BazaarSpendLedger``.

    ``would_exceed_daily`` is canned per-test; ``record`` captures calls
    so the test can assert the right amount + receipt landed.
    """

    def __init__(self, *, exceed: bool = False) -> None:
        self._exceed = exceed
        self.records: list[dict[str, Any]] = []
        self.would_exceed_calls: list[Decimal] = []

    async def would_exceed_daily(self, amount_usd: Decimal, *, daily_cap_usd: Decimal) -> bool:
        self.would_exceed_calls.append(amount_usd)
        return self._exceed

    async def record(self, **kwargs: Any) -> None:
        self.records.append(kwargs)


@pytest.mark.asyncio
async def test_daily_cap_exceeded_skips_pay_and_marks_degraded() -> None:
    """Pre-flight returns True → no pay() call, ``daily_cap_exceeded`` in degraded."""
    resource = _resource(
        url="https://example.com/api/v1/search",
        rtype="hotel-search",
        accepts=[_req("0.05")],
        score=0.9,
    )
    discovery = FakeDiscovery({"hotel": [resource]})
    consumer = CountingStubConsumer()
    ledger = _FakeLedger(exceed=True)

    provider = BazaarSourceProvider(
        discovery_client=discovery,
        x402_consumer=consumer,
        spend_ledger=ledger,  # type: ignore[arg-type]
    )
    result = await provider.fetch(idea="hotel", categories={"travel"})

    assert not result.fired
    assert "daily_cap_exceeded" in result.payload.get("degraded_sources", [])
    # Critical: pay() must NOT have been called.
    assert consumer.calls == []
    # And the pre-flight was actually consulted.
    assert ledger.would_exceed_calls, "would_exceed_daily was never called"


@pytest.mark.asyncio
async def test_successful_pay_records_to_ledger_once() -> None:
    """Ledger records exactly one row per successful settle, with the right amount."""
    resource = _resource(
        url="https://example.com/api/v1/search",
        rtype="hotel-search",
        accepts=[_req("0.07")],
        score=0.9,
    )
    discovery = FakeDiscovery({"hotel": [resource]})
    consumer = CountingStubConsumer()
    ledger = _FakeLedger(exceed=False)

    def handler(request: httpx.Request) -> httpx.Response:
        if "X-PAYMENT" not in request.headers:
            return httpx.Response(402, json={"error": "payment required"})
        return httpx.Response(
            200,
            json=[{"id": 1}, {"id": 2}, {"id": 3}],
            headers={"content-type": "application/json"},
        )

    async with _mock_transport(handler) as http_client:
        provider = BazaarSourceProvider(
            discovery_client=discovery,
            x402_consumer=consumer,
            adapter_registry=[GenericBazaarAdapter(http_client=http_client)],
            spend_ledger=ledger,  # type: ignore[arg-type]
        )
        result = await provider.fetch(idea="hotel", categories={"travel"})

    assert result.fired, result.error
    # Exactly one settle, one record. Amount matches the advertised price.
    assert len(consumer.calls) == 1
    assert len(ledger.records) == 1
    rec = ledger.records[0]
    assert rec["resource_url"] == resource.resource_url
    assert rec["amount_usd"] == Decimal("0.07")
    assert rec["mode"] == "stub"
    # Receipt round-tripped — tx_signature present from StubX402Consumer.
    assert rec["receipt"]["status"] == "success"


def test_bazaar_chunk_is_immutable() -> None:
    # frozen=True dataclass raises FrozenInstanceError on attr assignment.
    from dataclasses import FrozenInstanceError

    c = BazaarChunk(text="x", provider_kind="bazaar:t")
    with pytest.raises(FrozenInstanceError):
        c.text = "y"  # type: ignore[misc]
