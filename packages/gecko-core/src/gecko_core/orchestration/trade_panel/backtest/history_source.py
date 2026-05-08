"""History sources for the trade-panel backtest (Phase 9 v1).

Public types
------------

``HistorySource`` (Protocol): the contract the simulator consumes.
Anything that returns ``list[Candle]`` for a (protocol, granularity,
window) request satisfies it. Tests inject a canned source; production
wires :class:`PythHermesHistorySource`.

``PythHermesHistorySource``: tries to read cached candles from Mongo
first; on miss, attempts a Pyth Hermes fetch.

Pyth Hermes reality check
-------------------------

Pyth Hermes (``https://hermes.pyth.network``) is **real-time-focused**.
The endpoints we audited:

    - ``GET /v2/price_feeds``        — list of feed IDs + metadata.
    - ``GET /v2/updates/price/latest`` — current price.
    - ``GET /v2/updates/price/{publish_time}`` — point-in-time price at
      a specific publish_time (single tick, not OHLCV).

Hermes does NOT expose an OHLCV-bars endpoint. Building 1y/1d candles
from Hermes would require sampling latest-price every N minutes via a
cron and aggregating server-side — Phase 9.5 work.

For Phase 9 v1, the in-process behavior is:

    1. Read candles from the ``protocol_price_history`` Mongo cache.
    2. If cache is empty, return ``[]`` and let the caller emit a
       ``BacktestReport(unbacktestable=True, reason="pyth_no_history")``.
    3. The cache is intentionally empty until a separate ingestion job
       populates it. Phase 9.5 swaps in Birdeye / CoinGecko OHLCV.

This degrades gracefully and avoids a fake "we backtested it" report.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Protocol

from gecko_core.orchestration.trade_panel.backtest.models import (
    BacktestGranularity,
    Candle,
)
from gecko_core.orchestration.trade_panel.backtest.storage import read_candles

if TYPE_CHECKING:  # pragma: no cover - typing only
    import httpx

_log = logging.getLogger(__name__)

# Canonical Pyth feed IDs for the Solana DeFi protocols our trading-oracle
# corpus already covers. Source: Pyth's published price-feeds manifest at
# https://pyth.network/developers/price-feed-ids — values pinned here so a
# Pyth manifest update does not silently re-route lookups. Add a protocol
# = touch this dict only.
_PROTOCOL_TO_FEED: dict[str, str] = {
    "jupiter": "0x0a0408d619e9380abad35060f9192039ed5042fa6f82301d0e48bb52be830996",  # JUP/USD
    "kamino": "0xb17e5bc5de742a8a378b54c8c4f25b2a9d7e8e80e4a1a7e1aa1ad8b5a2e8d2e8",  # KMNO/USD (placeholder)
    "pyth": "0x0bbf28e9a841a1cc788f6a361b17ca072d0ea3098a1e5df1c3922d06719579ff",  # PYTH/USD
    "drift": "0x6c7e07a8a1e5f3f9a3d2c1d7cd2e2a3a3e2c1e5e7c4a9b8c7d6e5f4a3b2c1d0e",  # DRIFT/USD (placeholder)
    "jito": "0xa0255134973f4fdf2f8f7808354274a3b1ebc6ee438be898d045e8b56ba1fe13",  # JTO/USD
}

PYTH_HERMES_BASE_URL = "https://hermes.pyth.network"


class HistorySource(Protocol):
    """Returns ascending-by-ts candles for a protocol+granularity window.

    Implementations must be async + safe to call repeatedly. The simulator
    treats an empty list as "no history" and emits an unbacktestable
    report — implementations should not raise on missing data.
    """

    async def fetch(
        self,
        protocol: str,
        *,
        granularity: BacktestGranularity,
        ts_start: int,
        ts_end: int,
    ) -> list[Candle]:  # pragma: no cover - protocol
        ...


class PythHermesHistorySource:
    """Cache-first source backed by Mongo's ``protocol_price_history``.

    Hermes itself does not expose OHLCV; this class reads from the local
    Mongo cache. The ``_attempt_pyth_fetch`` hook is wired for Phase 9.5
    where we'll either sample-and-aggregate Hermes ourselves or swap in
    a real OHLCV provider behind the same Protocol surface.
    """

    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient | None = None,
        base_url: str = PYTH_HERMES_BASE_URL,
    ) -> None:
        self._http = http_client
        self._base_url = base_url

    async def fetch(
        self,
        protocol: str,
        *,
        granularity: BacktestGranularity,
        ts_start: int,
        ts_end: int,
    ) -> list[Candle]:
        proto = protocol.strip().lower()
        if not proto:
            return []
        # Cache hit path — the Mongo cache is the load-bearing source for v1.
        cached = await read_candles(
            proto,
            granularity=granularity,
            ts_start=ts_start,
            ts_end=ts_end,
        )
        if cached:
            return cached
        # Cache miss — Hermes does not serve OHLCV; degrade quietly.
        return await self._attempt_pyth_fetch(
            proto, granularity=granularity, ts_start=ts_start, ts_end=ts_end
        )

    async def _attempt_pyth_fetch(
        self,
        protocol: str,
        *,
        granularity: BacktestGranularity,
        ts_start: int,
        ts_end: int,
    ) -> list[Candle]:
        """Hook for live Pyth fetches. Returns ``[]`` in v1 (no OHLCV API).

        Kept as a method (not inlined) so Phase 9.5's CoinGecko/Birdeye
        swap is a small change here, not in ``fetch``. Tests subclass
        and override this to assert the cache-miss code path.
        """
        feed_id = _PROTOCOL_TO_FEED.get(protocol)
        if feed_id is None:
            return []
        # Pyth Hermes has no historical OHLCV endpoint as of 2026-05.
        # Returning empty triggers the unbacktestable=pyth_no_history path
        # in the caller. See module docstring for the upgrade plan.
        _log.info(
            "backtest.history.pyth_no_history protocol=%s granularity=%s ts_start=%s ts_end=%s",
            protocol,
            granularity,
            ts_start,
            ts_end,
        )
        return []


# ---------------------------------------------------------------------------
# Phase 9.5 — CoinGecko OHLC history source.
#
# CoinGecko's free `/v3/coins/{id}/ohlc` returns OHLC bars without an API
# key (subject to a 5-15 calls/min rate limit). Pyth Hermes does NOT
# expose historical OHLCV (Phase 9 finding); this is the swap. Volume is
# not in the free tier, so emitted candles carry vol_usd=0.0.
#
# Response shape (per CoinGecko docs):
#   [[timestamp_ms, open, high, low, close], ...]
# ---------------------------------------------------------------------------

# Pinned protocol → CoinGecko coin id mapping. Touch this dict to add a
# protocol; do not look up ids dynamically (a CoinGecko rename would
# silently re-route lookups).
_PROTOCOL_TO_COINGECKO_ID: dict[str, str] = {
    "jupiter": "jupiter-exchange-solana",  # JUP
    "kamino": "kamino",  # KMNO
    "pyth": "pyth-network",  # PYTH
    "drift": "drift-protocol",  # DRIFT
    "jito": "jito-governance-token",  # JTO
}

COINGECKO_OHLC_BASE_URL = "https://api.coingecko.com/api/v3"


def _granularity_to_days(granularity: BacktestGranularity, *, ts_start: int, ts_end: int) -> int:
    """Map (granularity, window) → CoinGecko `days` param.

    Free tier emits daily candles for `days >= 90` and hourly for
    `1 <= days <= 30`. We pick the smallest `days` value that covers the
    requested window AND lands in the right bucket for the requested
    granularity. v1 doesn't backfill mid-bucket.
    """
    span_seconds = max(1, ts_end - ts_start)
    span_days = max(1, (span_seconds + 86_399) // 86_400)
    if granularity == "1h":
        # Hourly bucket: 1, 7, 14, 30 are the supported steps.
        for step in (1, 7, 14, 30):
            if span_days <= step:
                return step
        return 30
    # Daily bucket: 90, 180, 365 are the supported steps for daily candles.
    for step in (90, 180, 365):
        if span_days <= step:
            return step
    return 365


class CoinGeckoOhlcHistorySource:
    """OHLC source backed by CoinGecko's free `/coins/{id}/ohlc` endpoint.

    Drops in for ``PythHermesHistorySource`` — implements the same
    ``HistorySource`` Protocol surface. Caller code does not change.

    Volume is not in the free tier; emitted candles carry ``vol_usd=0.0``.
    Unknown protocols return ``[]`` (graceful — caller emits
    ``unbacktestable=True, reason="no_candles"``). 429 responses retry once
    after a short backoff.
    """

    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient | None = None,
        base_url: str = COINGECKO_OHLC_BASE_URL,
        max_retries: int = 1,
        backoff_seconds: float = 1.0,
    ) -> None:
        self._http = http_client
        self._base_url = base_url.rstrip("/")
        self._max_retries = max(0, int(max_retries))
        self._backoff_seconds = max(0.0, float(backoff_seconds))

    async def fetch(
        self,
        protocol: str,
        *,
        granularity: BacktestGranularity,
        ts_start: int,
        ts_end: int,
    ) -> list[Candle]:
        proto = (protocol or "").strip().lower()
        if not proto:
            return []
        coin_id = _PROTOCOL_TO_COINGECKO_ID.get(proto)
        if coin_id is None:
            # Unknown-to-us protocol → empty list. Caller maps to
            # unbacktestable=True, reason="no_candles".
            _log.info(
                "backtest.history.coingecko_unknown_protocol protocol=%s",
                proto,
            )
            return []

        days = _granularity_to_days(granularity, ts_start=ts_start, ts_end=ts_end)
        url = f"{self._base_url}/coins/{coin_id}/ohlc"
        params = {"vs_currency": "usd", "days": str(days)}
        rows = await self._fetch_with_retry(url, params=params)
        if not rows:
            return []
        return [
            row
            for row in (_row_to_candle(r, protocol=proto, granularity=granularity) for r in rows)
            if row is not None
        ]

    async def _fetch_with_retry(self, url: str, *, params: dict[str, str]) -> list[list[Any]]:
        """GET ``url`` with one retry on 429. Owns the httpx client lifecycle."""
        import httpx

        attempt = 0
        client_owned = self._http is None
        client = self._http or httpx.AsyncClient(timeout=10.0)
        try:
            while True:
                resp = await client.get(url, params=params)
                if resp.status_code == 429 and attempt < self._max_retries:
                    attempt += 1
                    if self._backoff_seconds > 0:
                        await asyncio.sleep(self._backoff_seconds)
                    continue
                if resp.status_code != 200:
                    _log.warning(
                        "backtest.history.coingecko_http_error status=%s url=%s",
                        resp.status_code,
                        url,
                    )
                    return []
                data = resp.json()
                if not isinstance(data, list):
                    return []
                return data  # type: ignore[no-any-return]
        finally:
            if client_owned:
                await client.aclose()


def _row_to_candle(row: Any, *, protocol: str, granularity: BacktestGranularity) -> Candle | None:
    """Map a single ``[ts_ms, o, h, l, c]`` row into ``Candle``.

    Returns ``None`` on a malformed row rather than raising — bad rows
    should be dropped, not blow up the whole window.
    """
    if not isinstance(row, list | tuple) or len(row) < 5:
        return None
    try:
        ts_ms = int(row[0])
        o = float(row[1])
        h = float(row[2])
        low = float(row[3])
        c = float(row[4])
    except (TypeError, ValueError):
        return None
    return Candle(
        protocol=protocol,
        ts=ts_ms // 1000,
        granularity=granularity,
        source="coingecko",
        open=o,
        high=h,
        low=low,
        close=c,
        vol_usd=0.0,
    )


__all__ = [
    "COINGECKO_OHLC_BASE_URL",
    "PYTH_HERMES_BASE_URL",
    "CoinGeckoOhlcHistorySource",
    "HistorySource",
    "PythHermesHistorySource",
]
