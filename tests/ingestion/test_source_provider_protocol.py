"""Protocol conformance tests for the SourceProvider seam (S12-PROVIDER-01)."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from gecko_core.ingestion.providers import (
    ProviderHealth,
    SourceChunk,
    SourceProvider,
)
from gecko_core.ingestion.providers.free_provider import (
    DEFAULT_FREE_PROVIDER,
    FreeProvider,
)
from gecko_core.models import SourceCandidate


def test_free_provider_satisfies_protocol() -> None:
    # runtime_checkable Protocol — isinstance() catches structural drift
    # if someone removes a method or renames a field.
    assert isinstance(DEFAULT_FREE_PROVIDER, SourceProvider)
    assert isinstance(FreeProvider(), SourceProvider)


def test_free_provider_has_required_attrs() -> None:
    fp = FreeProvider()
    assert fp.name == "tavily"
    assert fp.kind == "free"


@pytest.mark.asyncio
async def test_free_provider_health_reports_available() -> None:
    fp = FreeProvider()
    health = await fp.health()
    assert isinstance(health, ProviderHealth)
    assert health.available is True


@pytest.mark.asyncio
async def test_free_provider_cost_estimate_nonzero() -> None:
    # Tavily advanced search costs ~$0.008 per call; the provider
    # surfaces that so the dispatcher can budget. We don't pin the
    # exact figure (ops may re-baseline it); just assert it's > 0
    # and < $1 — anything outside that window is a config error.
    fp = FreeProvider()
    cost = await fp.cost_estimate("any query")
    assert 0.0 < cost < 1.0


@pytest.mark.asyncio
async def test_free_provider_fetch_returns_source_chunks() -> None:
    # Stub out the underlying Tavily call so the test runs offline.
    fake = [
        SourceCandidate(
            url="https://news.ycombinator.com/item?id=42",  # type: ignore[arg-type]
            title="HN thread",
            type="web",
            score=0.9,
        )
    ]
    with patch(
        "gecko_core.ingestion.providers.free_provider._discover_impl",
        return_value=fake,
    ):
        fp = FreeProvider()
        chunks = await fp.fetch("idea")
    assert len(chunks) == 1
    assert isinstance(chunks[0], SourceChunk)
    assert chunks[0].candidate.title == "HN thread"


@pytest.mark.asyncio
async def test_dispatch_providers_collects_candidates() -> None:
    from gecko_core.ingestion.pipeline import dispatch_providers

    fake = [
        SourceCandidate(
            url="https://example.com/a",  # type: ignore[arg-type]
            title="A",
            type="web",
            score=0.5,
        )
    ]
    with patch(
        "gecko_core.ingestion.providers.free_provider._discover_impl",
        return_value=fake,
    ):
        candidates, degraded = await dispatch_providers("idea")
    assert [str(c.url) for c in candidates] == ["https://example.com/a"]
    assert degraded == []


@pytest.mark.asyncio
async def test_dispatch_providers_degrades_on_failure() -> None:
    """A provider that raises is reported in degraded_sources, not propagated."""
    from gecko_core.ingestion.pipeline import dispatch_providers

    class ExplodingProvider:
        name = "exploding"
        kind = "x402-bazaar"

        async def cost_estimate(self, query: str) -> float:
            return 0.05

        async def health(self) -> ProviderHealth:
            return ProviderHealth(available=True)

        async def fetch(self, query: str) -> list[SourceChunk]:
            raise RuntimeError("upstream timeout")

    candidates, degraded = await dispatch_providers("idea", providers=[ExplodingProvider()])
    assert candidates == []
    assert degraded == ["exploding"]
