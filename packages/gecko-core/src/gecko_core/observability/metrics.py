"""Read-side aggregations over ``gecko_events.events``.

Powers ``GET /metrics`` on gecko-api (S24 WS-D Task #3). All windows are
computed in one pass per window — Mongo handles the heavy lifting via
``$match`` + ``$group`` so the FastAPI handler is just a dict assembler.

JSON shape (versioned via ``schema_version`` so /metrics consumers can
pin):

    {
      "schema_version": 1,
      "generated_at": "<iso8601>",
      "windows": {
        "24h": { ...counts/sums... },
        "7d":  { ...same shape + dod fields... }
      },
      "errors_24h": <int>,
      "x402_live_blocked_24h": <int>,
    }
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from gecko_core.observability.events import events_collection

logger = logging.getLogger(__name__)


_WINDOW_24H = timedelta(hours=24)
_WINDOW_7D = timedelta(days=7)


async def _window_stats(coll: Any, since: datetime) -> dict[str, Any]:
    """Aggregate counts/sums for one time window."""
    cursor = coll.find({"ts": {"$gte": since}})
    verdicts_served = 0
    cache_hits = 0
    cache_misses = 0
    revenue_usd = 0.0
    latencies: list[float] = []
    tier_counts: dict[str, int] = {}
    wallets: set[str] = set()
    async for doc in cursor:
        event = doc.get("event")
        payload = doc.get("payload") or {}
        wallet = doc.get("wallet")
        if wallet:
            wallets.add(wallet)
        if event == "verdict.served":
            verdicts_served += 1
            tier = payload.get("tier") or "unknown"
            tier_counts[tier] = tier_counts.get(tier, 0) + 1
            latency = payload.get("latency_ms")
            if isinstance(latency, (int, float)):
                latencies.append(float(latency))
        elif event == "cache.hit":
            cache_hits += 1
        elif event == "cache.miss":
            cache_misses += 1
        elif event == "x402.settle":
            amount = payload.get("amount_usdc")
            if isinstance(amount, (int, float)):
                revenue_usd += float(amount)
    total_cache = cache_hits + cache_misses
    cache_hit_ratio = (cache_hits / total_cache) if total_cache else 0.0
    p50 = _percentile(latencies, 0.50)
    p95 = _percentile(latencies, 0.95)
    top_tiers = sorted(tier_counts.items(), key=lambda kv: kv[1], reverse=True)[:3]
    return {
        "verdicts_served": verdicts_served,
        "revenue_usdc": round(revenue_usd, 4),
        "cache_hit_ratio": round(cache_hit_ratio, 4),
        "cache_hits": cache_hits,
        "cache_misses": cache_misses,
        "latency_p50_ms": p50,
        "latency_p95_ms": p95,
        "top_tiers": [{"tier": t, "count": c} for t, c in top_tiers],
        "distinct_wallets": len(wallets),
    }


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    # statistics.quantiles uses inclusive method; use simple index for tiny
    # samples so a 1-element list returns the element itself (instead of
    # raising on n<2 from statistics.quantiles).
    if len(s) == 1:
        return round(s[0], 2)
    idx = min(len(s) - 1, max(0, round(q * (len(s) - 1))))
    return round(s[idx], 2)


async def _count_events(coll: Any, *, event: str, since: datetime) -> int:
    return int(await coll.count_documents({"event": event, "ts": {"$gte": since}}))


async def aggregate_metrics(now: datetime | None = None) -> dict[str, Any]:
    """Read-only aggregate. Returns an empty-but-valid shell if mongo unset
    so the /metrics handler stays observable even pre-bootstrap.
    """
    now = now or datetime.now(UTC)
    coll = events_collection()
    if coll is None:
        return {
            "schema_version": 1,
            "generated_at": now.isoformat(),
            "mongo_configured": False,
            "windows": {"24h": _empty_window(), "7d": _empty_window()},
            "errors_24h": 0,
            "x402_live_blocked_24h": 0,
        }

    since_24h = now - _WINDOW_24H
    since_7d = now - _WINDOW_7D
    since_prev_24h = since_24h - _WINDOW_24H

    stats_24h = await _window_stats(coll, since_24h)
    stats_7d = await _window_stats(coll, since_7d)
    stats_prev_24h = await _window_stats(coll, since_prev_24h)

    # Day-over-day deltas live on the 7d block since the 24h block already
    # is "today"; the prev-24h baseline is yesterday.
    stats_7d["dod_verdicts_served_delta"] = stats_24h["verdicts_served"] - (
        stats_prev_24h["verdicts_served"] - stats_24h["verdicts_served"]
    )
    stats_7d["dod_revenue_usdc_delta"] = round(
        stats_24h["revenue_usdc"] - (stats_prev_24h["revenue_usdc"] - stats_24h["revenue_usdc"]),
        4,
    )

    errors_24h = await _count_events(coll, event="agent.error", since=since_24h)
    blocked_24h = await _count_events(coll, event="x402.live_blocked", since=since_24h)

    return {
        "schema_version": 1,
        "generated_at": now.isoformat(),
        "mongo_configured": True,
        "windows": {"24h": stats_24h, "7d": stats_7d},
        "errors_24h": errors_24h,
        "x402_live_blocked_24h": blocked_24h,
    }


def _empty_window() -> dict[str, Any]:
    return {
        "verdicts_served": 0,
        "revenue_usdc": 0.0,
        "cache_hit_ratio": 0.0,
        "cache_hits": 0,
        "cache_misses": 0,
        "latency_p50_ms": None,
        "latency_p95_ms": None,
        "top_tiers": [],
        "distinct_wallets": 0,
    }


__all__ = ["aggregate_metrics"]
