"""Provider router — pure-function build of a per-session provider plan.

Sprint 14 S14-TWITSH-01: minimum viable router that takes a classifier's
``category`` output + the raw idea string and returns the list of
``SourceProvider`` instances the dispatcher should fan out across.

Today the only S14 routing rule we need is the Colosseum-judge filter:

  if category in {crypto, defi, hackathon-team}
       AND idea matches Solana keyword regex:
     plan += TwitshProvider(author_allowlist=COLOSSEUM_JUDGES)
  elif category in {crypto, defi, hackathon-team}:
     plan += TwitshProvider()      # unfiltered

The free Tavily-backed FreeProvider is always included (no router gate).
Other paid providers (ParagraphProvider, etc.) are wired in by their
respective tickets — this module's surface is intentionally tiny so the
addition of more rules stays a one-liner.

The ``TWITSH_RESEARCH_ENABLED`` flag is checked inside ``TwitshProvider``
itself (via ``health()`` and ``fetch()``); the router does not duplicate
the gate. The router's job is _shape_, not feature-flag enforcement.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING

from .free_provider import DEFAULT_FREE_PROVIDER

if TYPE_CHECKING:
    from . import SourceChunk, SourceProvider

logger = logging.getLogger(__name__)

# Per-provider timeout for parallel fan-out. The Sprint 13 twit.sh probe
# measured ~7.5s settlement latency on the cold path; budget 15s/provider
# so the slower x402 leg has headroom without dragging the dispatcher
# past the per-session ceiling. Override via ``GECKO_PROVIDER_TIMEOUT_S``.
DEFAULT_PROVIDER_TIMEOUT_S: float = 15.0


# Categories where twit.sh is worth paying for. Mirrors the source-side
# `_FIRES_FOR` constant; kept duplicated to avoid an ingestion → sources
# import back-edge from the router (sources/__init__.py is import-order
# sensitive; see the dispatcher.py docstring).
_TWITSH_CATEGORIES: frozenset[str] = frozenset({"crypto", "defi", "hackathon-team"})

# Solana-adjacent keyword regex. Matches whole tokens, case-insensitive.
# Word-boundary (\b) keeps "renaissance" from accidentally matching
# longer English words that happen to contain the substring.
_SOLANA_KEYWORD_RE = re.compile(
    r"\b(solana|colosseum|breakpoint|radar|cypherpunk|breakout|renaissance)\b",
    re.IGNORECASE,
)


def _matches_solana_keywords(idea: str) -> bool:
    return bool(_SOLANA_KEYWORD_RE.search(idea or ""))


def build_provider_plan(
    *,
    idea: str,
    category: str | None,
) -> list[SourceProvider]:
    """Return the ordered provider list for a session.

    FreeProvider is always first (cheapest, broadest); paid providers
    follow per the routing rules. Order matters for the dispatcher's
    budget-pre-check (S13+ Track F): cheaper providers run first so the
    per-session cap is consumed predictably.
    """
    plan: list[SourceProvider] = [DEFAULT_FREE_PROVIDER]

    cat = (category or "").strip().lower()
    if cat in _TWITSH_CATEGORIES:
        # Lazy import: keeps the router importable without forcing the
        # twit.sh deps (httpx/x402) into every Gecko import path.
        from .twitsh_provider import TwitshProvider, load_colosseum_judges

        if _matches_solana_keywords(idea):
            allowlist = load_colosseum_judges()
            # Empty allowlist (file missing or all cycles drained) →
            # fall through to unfiltered. Better to surface signal than
            # silently emit zero citations from the provider.
            if allowlist:
                plan.append(TwitshProvider(author_allowlist=allowlist))
            else:
                plan.append(TwitshProvider())
        else:
            plan.append(TwitshProvider())

    return plan


async def fanout_fetch(
    providers: list[SourceProvider],
    *,
    query: str,
    timeout_s: float = DEFAULT_PROVIDER_TIMEOUT_S,
) -> tuple[list[SourceChunk], list[str]]:
    """Run every provider's ``fetch`` in parallel via ``asyncio.gather``.

    Sprint 14 S14-TWITSH-04: the probe measured ~7.5s settlement latency
    on the cold twit.sh path. Sequential fan-out adds that to Tavily's
    ~3-5s and blows the per-session latency budget; parallel via
    ``asyncio.gather(..., return_exceptions=True)`` keeps total wall-
    clock bounded by the slowest provider.

    Returns ``(chunks, degraded_sources)``: ``degraded_sources`` carries
    the names of providers that timed out, raised, or otherwise failed
    to deliver, mirroring the per-Citation degraded-tracking pattern
    that paragraph_provider + the dispatcher already use.
    """

    async def _one(provider: SourceProvider) -> list[SourceChunk]:
        # Per-provider timeout — keeps a slow provider from blocking
        # results from a fast peer past the per-session ceiling.
        return await asyncio.wait_for(provider.fetch(query), timeout=timeout_s)

    if not providers:
        return [], []

    results = await asyncio.gather(
        *(_one(p) for p in providers),
        return_exceptions=True,
    )

    chunks: list[SourceChunk] = []
    degraded: list[str] = []
    for provider, result in zip(providers, results, strict=True):
        if isinstance(result, BaseException):
            logger.warning(
                "provider_router: %s failed (%s); marking degraded",
                provider.name,
                type(result).__name__,
            )
            degraded.append(provider.name)
            continue
        chunks.extend(result)
    return chunks, degraded


__all__ = ["DEFAULT_PROVIDER_TIMEOUT_S", "build_provider_plan", "fanout_fetch"]
