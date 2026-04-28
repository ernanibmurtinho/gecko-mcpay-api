"""Tavily-backed source discovery.

`discover(idea)` returns a ranked list of candidate URLs. The caller (CLI /
MCP / API) presents these to the user for approval before ingestion.
"""

from __future__ import annotations

import asyncio
from typing import Any

from tavily import TavilyClient

from gecko_core.models import SourceCandidate, SourceType

from .settings import get_ingestion_settings


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


def _search_sync(api_key: str, query: str, max_results: int) -> list[dict[str, Any]]:
    client = TavilyClient(api_key=api_key)
    response = client.search(
        f"best sources about: {_truncate_for_tavily(query)}",
        max_results=max_results,
        search_depth="advanced",
        exclude_domains=list(_BLOCKED_DOMAINS),
    )
    results = response.get("results", []) if isinstance(response, dict) else []
    return list(results)


async def discover(idea: str, max_results: int = 8) -> list[SourceCandidate]:
    """Surface up to `max_results` candidate sources for an idea.

    Network call dispatched via asyncio.to_thread because tavily-python is sync.
    Returns sorted (highest score first). Empty list if Tavily returns nothing.
    """
    settings = get_ingestion_settings()
    raw = await asyncio.to_thread(
        _search_sync,
        settings.tavily_api_key.get_secret_value(),
        idea,
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


__all__ = ["discover"]
