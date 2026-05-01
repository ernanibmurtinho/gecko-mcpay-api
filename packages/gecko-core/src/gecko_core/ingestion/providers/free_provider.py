"""FreeProvider — wraps the existing Tavily/web/youtube dispatch.

This is the default `SourceProvider` implementation. It delegates to the
pre-existing `discovery.discover()` so all the category-aware query
hints, blocked-domain filtering, and Tavily settings stay exactly where
they are — they remain FreeProvider-internal config, not the
dispatcher's concern (per staff-eng review § 1a).

Cost is the listed Tavily advanced-search call price; health always
reports available because we don't yet track Tavily-side failures (the
existing pipeline already isolates per-source failures inside
`pipeline._process_one`).
"""

from __future__ import annotations

from gecko_core.ingestion.discovery import (
    TAVILY_ADVANCED_SEARCH_USD,
)
from gecko_core.ingestion.discovery import (
    discover as _discover_impl,
)

from . import ProviderHealth, ProviderKind, SourceChunk, SourceProvider


class FreeProvider:
    """Tavily-backed discovery + the existing per-URL extraction chain.

    Conforms to `SourceProvider` structurally (Protocol). Stays a plain
    class so downstream code that wants to introspect or swap one
    instance can do so without import gymnastics.
    """

    name: str = "tavily"
    kind: ProviderKind = "free"

    def __init__(self, *, max_results: int = 8) -> None:
        self._max_results = max_results

    async def cost_estimate(self, query: str) -> float:
        # Single advanced-search call per discover() invocation. Per-URL
        # extraction costs (tavily Extract fallback, deepgram audio) are
        # billed downstream by the pipeline, not by the provider.
        return TAVILY_ADVANCED_SEARCH_USD

    async def health(self) -> ProviderHealth:
        # No rolling-window tracking yet (S13+). Treat as always available;
        # per-call failures still surface through the pipeline's
        # SourceOutcome("failed") path, not through the provider health
        # gate.
        return ProviderHealth(available=True, success_rate=1.0, last_error=None)

    async def fetch(self, query: str) -> list[SourceChunk]:
        candidates = await _discover_impl(query, max_results=self._max_results)
        # FreeProvider hands back SourceCandidate pointers; the existing
        # pipeline owns extraction + chunking from here. text/metadata
        # stay empty (see SourceChunk docstring).
        return [SourceChunk(candidate=c) for c in candidates]


# Module-level singleton. The dispatcher uses this as the default
# provider when no explicit list is passed; tests and S13 vertical
# suites can swap it for a custom instance.
DEFAULT_FREE_PROVIDER: SourceProvider = FreeProvider()


__all__ = ["DEFAULT_FREE_PROVIDER", "FreeProvider"]
