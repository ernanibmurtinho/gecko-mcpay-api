"""Tavily-backed source discovery.

`discover(idea)` returns a ranked list of candidate URLs. The caller (CLI /
MCP / API) presents these to the user for approval before ingestion.

Query construction is **category-aware**: we classify the idea cheaply
(see `gecko_core.classify`) and inject domain-specific keyword hints so
Tavily returns founder/builder discussion (Reddit, HN, YC, IndieHackers,
Substack, etc.) rather than SEO-bait pages that happen to lexically
overlap with phrases like "AI co-founder" (security wordlists, dirbuster
directory lists, random LinkedIn profiles).

The classification step is best-effort: any failure (missing API key in
tests, network blip, threshold returns ∅) silently degrades to the
"unknown" branch, which still adds builder-community hints — the
asymmetry we observed was that crypto ideas are *self-disambiguating*
("Solana", "DeFi") while builder/SaaS ideas need lexical scaffolding to
out-rank wordlist spam.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from tavily import TavilyClient

from gecko_core.models import SourceCandidate, SourceType

from .settings import get_ingestion_settings

logger = logging.getLogger(__name__)


def _infer_type(url: str) -> SourceType:
    if "youtube.com" in url or "youtu.be" in url:
        return "youtube"
    return "web"


# Domains that aggressively bot-wall server-side fetches. We ask Tavily to
# skip them at discovery time so the user's source list is dominated by
# things we can actually index.
# Tavily list price for `search_depth="advanced"` is 2 credits per call,
# 1 credit ≈ $0.004 → ~$0.008 per discovery. Surfaced on the per-session
# economics view; recomputed if Tavily changes pricing.
TAVILY_ADVANCED_SEARCH_USD: float = 0.008


_BLOCKED_DOMAINS: tuple[str, ...] = (
    "booking.com",
    "expedia.com",
    "hotels.com",
    "tripadvisor.com",
    "tripadvisor.com.br",
    "kayak.com",
    "agoda.com",
    "trivago.com",
    "airbnb.com",
    # Bot-walled aggregators that fail both direct fetch and Tavily Extract,
    # so the per-URL Tavily cost is wasted. Add to this list as we observe
    # repeat offenders in production.
    "similarweb.com",
    # Career listing / SEO-bait sites that surfaced on builder-topic
    # discovery (CBT Nuggets career page, etc). Indexed pages mention
    # "co-founder" / "founder" lexically but contain no signal.
    "cbtnuggets.com",
    "ziprecruiter.com",
)


# Per-category keyword hints appended to the search query. These bias
# Tavily toward founder/builder discussion without using `include_domains`
# (a hard filter that would gut recall on ideas that legitimately span
# blog posts on personal sites or company engineering blogs).
#
# Empty tuple = no hints (we trust the idea's own keywords). The default
# / unknown bucket gets the builder-community hint set, which is what
# fixes the regression for ideas like "AI co-founder for indie hackers".
_CATEGORY_QUERY_HINTS: dict[str, tuple[str, ...]] = {
    "crypto": (),  # self-disambiguating; "Solana"/"DeFi" already discriminate
    "defi": (),
    "devtools": (
        "Hacker News",
        "indie hackers",
        "developer tools discussion",
    ),
    "saas": (
        "indie hackers",
        "SaaS founders",
        "Hacker News",
        "Y Combinator",
    ),
    "regulated": (
        "compliance discussion",
        "founders",
    ),
    "hackathon-team": (
        "hackathon",
        "indie hackers",
        "founders",
    ),
}

# Hints used when classification returns ∅ ("unknown"). This is the safe
# baseline for the long tail of builder-style ideas — biases toward the
# venues where founders actually discuss this stuff.
_UNKNOWN_QUERY_HINTS: tuple[str, ...] = (
    "indie hackers",
    "Hacker News",
    "Y Combinator",
    "founders discussion",
)


# Tavily's /search endpoint hard-caps query length at 400 chars. Reserving a
# 20-char prefix below ("best sources about: ") leaves us 380 for the idea.
# Pro tier idea strings can run 400-800 chars; truncate at a word boundary so
# the query stays semantic. The full idea still feeds the agents — only the
# discovery search query is shortened.
_TAVILY_QUERY_BUDGET = 380


def _truncate_for_tavily(idea: str) -> str:
    if len(idea) <= _TAVILY_QUERY_BUDGET:
        return idea
    head = idea[:_TAVILY_QUERY_BUDGET]
    last_space = head.rfind(" ")
    return head[:last_space] if last_space > 200 else head


def _build_query(idea: str, categories: set[str]) -> str:
    """Build the Tavily search string for `idea` + classified `categories`.

    Crypto/defi ideas use the legacy "best sources about: X" form because
    the keyword density of their idea text already discriminates well
    (Sprint 2 eval gate measured 1.0 on crypto). Other categories get a
    category-specific hint suffix so Tavily ranks founder/builder
    discussion above SEO-bait wordlists.
    """
    truncated = _truncate_for_tavily(idea)

    # Collect hints from every selected category. Multi-label classify_idea
    # can return up to 2 cats; merge their hint sets and dedupe while
    # preserving order (insertion order = priority).
    hints: list[str] = []
    seen: set[str] = set()
    if categories:
        for cat in categories:
            for h in _CATEGORY_QUERY_HINTS.get(cat, ()):
                if h not in seen:
                    seen.add(h)
                    hints.append(h)
    else:
        # Unknown / classifier returned nothing — apply the builder-bias
        # baseline. This is the codepath that fixes the dogfood regression.
        for h in _UNKNOWN_QUERY_HINTS:
            seen.add(h)
            hints.append(h)

    if not hints:
        # Crypto/defi path: legacy phrasing, unchanged.
        return f"best sources about: {truncated}"

    # Builder/SaaS/unknown path: weave in hints. We lead with the idea
    # text so semantic search still anchors on it, then append discussion
    # venues and audience descriptors as additional ranking signal.
    suffix = " ".join(hints)
    query = f"{truncated} discussion {suffix}"
    # Defensive: respect Tavily's 400-char hard cap even after the suffix.
    if len(query) > 400:
        query = query[:400].rsplit(" ", 1)[0]
    return query


def _search_sync(api_key: str, query: str, max_results: int) -> list[dict[str, Any]]:
    client = TavilyClient(api_key=api_key)
    response = client.search(
        query,
        max_results=max_results,
        search_depth="advanced",
        topic="general",
        exclude_domains=list(_BLOCKED_DOMAINS),
    )
    results = response.get("results", []) if isinstance(response, dict) else []
    return list(results)


async def _safe_classify(idea: str) -> set[str]:
    """Classify or return ∅ on any failure (missing key, network, etc).

    Discovery must never fail because the cheap classifier had a hiccup;
    the unknown-bucket query hints already bias the results sensibly.
    """
    try:
        from gecko_core.classify import classify_idea

        return await classify_idea(idea)
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("discover: classify_idea failed (%s); using unknown bucket", exc)
        return set()


async def discover(idea: str, max_results: int = 8) -> list[SourceCandidate]:
    """Surface up to `max_results` candidate sources for an idea.

    Network call dispatched via asyncio.to_thread because tavily-python is sync.
    Returns sorted (highest score first). Empty list if Tavily returns nothing.

    The Tavily query is built from `idea` plus category-aware keyword
    hints — see `_build_query` and the `_CATEGORY_QUERY_HINTS` table. The
    raw `idea` is **not** sent verbatim; that path was the source of the
    Sprint 6 builder-topic regression.
    """
    settings = get_ingestion_settings()
    categories = await _safe_classify(idea)
    query = _build_query(idea, categories)
    logger.info(
        "discover: idea=%r categories=%s query=%r",
        idea[:80],
        sorted(categories) or "unknown",
        query[:120],
    )
    raw = await asyncio.to_thread(
        _search_sync,
        settings.tavily_api_key.get_secret_value(),
        query,
        max_results,
    )

    candidates = [
        SourceCandidate(
            url=item["url"],
            title=item.get("title", "") or "",
            type=_infer_type(item["url"]),
            score=float(item.get("score", 0.0) or 0.0),
        )
        for item in raw
        if item.get("url")
    ]
    return sorted(candidates, key=lambda c: c.score, reverse=True)


__all__ = ["_build_query", "discover"]
