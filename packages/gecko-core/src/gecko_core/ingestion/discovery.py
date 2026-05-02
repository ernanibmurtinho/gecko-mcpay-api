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
import os
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


# S17-DISCOVERY-01 — post-fetch URL blocklist for "dictionary noise".
# The 2026-04-30 e2e produced 5/8 Tavily hits to wordlist / dictionary
# dumps for a research-style idea — Tavily ranks them high because the
# idea's technical vocabulary matches dictionary tokens lexically. We
# can't pass these as `exclude_domains` to Tavily (some live on academic
# subdirectories of otherwise-useful CS dept hosts), so the gating runs
# *after* the search call, before scoring + truncation. Cost of a
# wasted Tavily credit is acceptable; cost of letting `dict.dat` into
# the index is not.
#
# `BLOCKED_URL_EXTENSIONS`: bare data dumps. ".pdf" stays out — PDFs
# are useful evidence (the S17-INGEST-FALLBACK-01 PDF parser is the
# load-bearing path for them). ".html" stays out for the obvious reason.
BLOCKED_URL_EXTENSIONS: tuple[str, ...] = (
    ".txt",
    ".dat",
    ".csv",
    ".json",
    ".tsv",
    ".log",
)

# Path-fragment patterns. Substring match on the lowercased path; we
# keep the list small and high-signal so an unrelated page that happens
# to mention "dictionary" in its slug doesn't get nuked.
BLOCKED_URL_PATH_PATTERNS: tuple[str, ...] = (
    "/dictionary/",
    "/wordlist/",
    "/wordlists/",
    "-words/",
    "/dict.dat",
    "/dict.txt",
)

# Curated host blocklist. These have repeated as dictionary-noise in
# production traces; each one costs us a wasted Tavily credit + a
# failed ingestion attempt + index-pollution if it sneaks through.
BLOCKED_DICTIONARY_HOSTS: tuple[str, ...] = (
    "snap.berkeley.edu",
    "ftp.cdc.gov",
    "april.eecs.umich.edu",
    "stat.wharton.upenn.edu",
    "people.brandeis.edu",
)


def _is_blocked_dictionary_url(url: str) -> bool:
    """True when ``url`` matches any dictionary / raw-data heuristic.

    Conservative on purpose: false-positives mean we miss a real source;
    false-negatives mean we burn Tavily credit on noise. We err toward
    the false-positive side and let the curated host list grow as
    operators flag specific offenders.
    """
    lowered = (url or "").lower()
    if not lowered:
        return True
    # Extension check: we look at the URL tail (before any querystring).
    path_only = lowered.split("?", 1)[0].split("#", 1)[0]
    if any(path_only.endswith(ext) for ext in BLOCKED_URL_EXTENSIONS):
        return True
    if any(fragment in path_only for fragment in BLOCKED_URL_PATH_PATTERNS):
        return True
    return any(host in lowered for host in BLOCKED_DICTIONARY_HOSTS)


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
    # S17-DISCOVERY-01: agentic-economy / web3-protocol / mcp / x402 ideas
    # benefit from the same builder-discussion bias as `saas` but tuned
    # toward x402 / agent-protocol venues. The classifier doesn't have
    # an `agentic-economy` label today; this entry is read directly when
    # `_unknown_query_hints_for_idea` flags an x402/mcp/agent token.
    "agentic-economy": (
        "Hacker News",
        "x402",
        "MCP",
        "founder blog",
        "agentic",
    ),
}


# Tokens that, when present in the idea, push us toward the
# agentic-economy hint set even when the classifier returns ∅. Same
# spirit as the Arxiv keyword-signal table; kept independent so the two
# heuristics can drift if needed.
_AGENTIC_ECONOMY_SIGNALS: frozenset[str] = frozenset(
    {"agent", "agentic", "x402", "mcp", "marketplace", "tradeable", "tradable"}
)


def _unknown_query_hints_for_idea(idea: str) -> tuple[str, ...]:
    """Pick the right unknown-bucket hint set based on idea-text signals.

    Default = legacy builder-bias hints. Agentic-economy override is
    new in S17 — biases Tavily toward HN, founder blogs, and x402/mcp
    discussion venues for ideas that mention those tokens explicitly.
    """
    lowered = (idea or "").lower()
    if any(sig in lowered for sig in _AGENTIC_ECONOMY_SIGNALS):
        return _CATEGORY_QUERY_HINTS["agentic-economy"]
    return _UNKNOWN_QUERY_HINTS


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
        # baseline (or the agentic-economy override when the idea text
        # explicitly mentions x402/mcp/agentic tokens; see S17-DISCOVERY-01).
        for h in _unknown_query_hints_for_idea(idea):
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


async def discover(
    idea: str,
    max_results: int | None = None,
    *,
    provider: object | None = None,
) -> list[SourceCandidate]:
    """Surface up to `max_results` candidate sources for an idea.

    Network call dispatched via asyncio.to_thread because tavily-python is sync.
    Returns sorted (highest score first). Empty list if Tavily returns nothing.

    The Tavily query is built from `idea` plus category-aware keyword
    hints — see `_build_query` and the `_CATEGORY_QUERY_HINTS` table. The
    raw `idea` is **not** sent verbatim; that path was the source of the
    Sprint 6 builder-topic regression.

    S12-PROVIDER-01 — when called through the SourceProvider Protocol
    (FreeProvider in `providers/free_provider.py`), `discover` is the
    inner Tavily-specific implementation. The `provider` kwarg is
    accepted for forward-compatibility with S13's dispatcher; when
    None, the legacy direct Tavily path runs unchanged. Today's
    callers pass nothing.
    """
    if provider is not None:
        chunks = await provider.fetch(idea)  # type: ignore[attr-defined]
        return [c.candidate for c in chunks]
    settings = get_ingestion_settings()
    # S17-DISCOVERY-01 — Tavily is now the *fallback* primary, not the
    # default. Default quota dropped from 8→3; tunable via env so the
    # provider rebalance (workflows-side) and the discovery layer
    # agree on the same number without a redeploy.
    if max_results is None:
        try:
            max_results = max(1, int(os.environ.get("GECKO_PROVIDER_QUOTA_TAVILY", "3")))
        except ValueError:
            max_results = 3
    categories = await _safe_classify(idea)
    query = _build_query(idea, categories)
    logger.info(
        "discover: idea=%r categories=%s query=%r max_results=%d",
        idea[:80],
        sorted(categories) or "unknown",
        query[:120],
        max_results,
    )
    # Ask Tavily for ~2x the quota so the post-fetch dictionary blocklist
    # has headroom — better to over-fetch by a few and filter than to
    # come up short on a research-style idea where 60% of hits get
    # blocked. Caps at 10 to keep the credit cost bounded.
    over_fetch = min(max_results * 2, 10)
    raw = await asyncio.to_thread(
        _search_sync,
        settings.tavily_api_key.get_secret_value(),
        query,
        over_fetch,
    )

    # S17-DISCOVERY-01 — drop dictionary / wordlist / raw-data URLs
    # before scoring. Logged at info so the post-S17 dashboard can
    # tune the heuristic from production traces.
    filtered: list[dict[str, Any]] = []
    for item in raw:
        url_str = item.get("url") or ""
        if not url_str:
            continue
        if _is_blocked_dictionary_url(url_str):
            logger.info("discover: blocking dictionary-noise url=%s", url_str)
            continue
        filtered.append(item)

    candidates = [
        SourceCandidate(
            url=item["url"],
            title=item.get("title", "") or "",
            type=_infer_type(item["url"]),
            score=float(item.get("score", 0.0) or 0.0),
        )
        for item in filtered
    ]
    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates[:max_results]


__all__ = [
    "BLOCKED_DICTIONARY_HOSTS",
    "BLOCKED_URL_EXTENSIONS",
    "BLOCKED_URL_PATH_PATTERNS",
    "TAVILY_ADVANCED_SEARCH_USD",
    "_build_query",
    "_is_blocked_dictionary_url",
    "discover",
]
