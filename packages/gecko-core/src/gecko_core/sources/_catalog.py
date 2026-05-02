"""Static catalog of every source Gecko knows about.

Loaded as a side-effect of `available_sources()`. We keep the metadata
here (rather than on each `Source` class) so the catalog stays defined
even when the concrete fetcher hasn't shipped yet — Wave 2 sources
(reddit, twit_sh) land in follow-up commits but the introspection surface
needs them visible from day one of the MCP/CLI rollout.

`gating` describes when a source fires (categories or "always"). It is
descriptive, not authoritative — the runtime decision lives in each
`Source.applies_to()`. `cost_per_call` is the upper-bound USD figure
surfaced to users; per-session economics are reported separately.
"""

from __future__ import annotations

from gecko_core.sources import SourceCatalogEntry, register_source

# Catalog order is preserved for rendering. Group by "always" sources first,
# then category-gated ones — that's the order users care about when reading
# the table.
_ENTRIES: tuple[SourceCatalogEntry, ...] = (
    SourceCatalogEntry(
        name="tavily",
        description="Generic web discovery via Tavily search API.",
        gating="Always",
        cost_per_call="$0.005",
    ),
    SourceCatalogEntry(
        name="hn",
        description="Hacker News story + comment search via Algolia HN API.",
        gating="Always",
        cost_per_call="Free",
    ),
    SourceCatalogEntry(
        name="reddit",
        description="Targeted subreddit search for builder/founder discussion.",
        gating="Always",
        cost_per_call="Free",
    ),
    SourceCatalogEntry(
        name="gecko_precedent",
        description="Internal flywheel: prior Gecko verdicts on similar ideas.",
        gating="Always (cosine-threshold gated)",
        cost_per_call="Free",
    ),
    SourceCatalogEntry(
        name="colosseum",
        description="Solana Colosseum hackathon track + winners corpus.",
        gating="Crypto/defi/hackathon-team only",
        cost_per_call="Free",
    ),
    SourceCatalogEntry(
        name="twit_sh",
        description="twit.sh signal API for crypto Twitter alpha + sentiment.",
        gating="Crypto/defi only",
        cost_per_call="$0.005",
    ),
    # S16-BAZAAR-CONSUMER-03: catalog-led x402 buyer. Always applies — the
    # Bazaar catalog is universal and per-query relevance is decided at
    # discovery time. Live-mode pay() gated on S16-BAZAAR-CONSUMER-04.
    SourceCatalogEntry(
        name="bazaar",
        description="x402-paid Bazaar resources (catalog-led; one generic adapter consumes any).",
        gating="Always (per-query catalog discovery)",
        cost_per_call="≤ $0.50 per session (capped)",
    ),
    # S17-DISCOVERY-01: free, structured paper-abstract provider. Fires
    # for technical / research / web3-protocol / agentic-economy ideas
    # via classifier categories OR idea-text signals.
    SourceCatalogEntry(
        name="arxiv",
        description="Arxiv abstracts via the public export API (no key, no rate limit).",
        gating="Technical / research / agentic-economy ideas",
        cost_per_call="Free",
    ),
)

for _entry in _ENTRIES:
    register_source(_entry)


__all__ = ["_ENTRIES"]
