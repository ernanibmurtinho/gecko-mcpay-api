"""Trade-panel backtest sub-package (Phase 9 v1).

Public surface
--------------

    from gecko_core.orchestration.trade_panel.backtest import (
        backtest_intent,
        BacktestReport,
        HistorySource,
        PythHermesHistorySource,
    )

    report = await backtest_intent(intent={...}, history=PythHermesHistorySource())

The function takes a Strategist-emitted intent dict (raw, free-form
keys), normalizes into :class:`BacktestIntent`, asks the
:class:`HistorySource` for candles, and replays via the naive PnL
simulator. Always returns a :class:`BacktestReport` — the
``unbacktestable=True`` shape is the graceful-degradation contract.

Defaults
--------

- Direction: pulled from ``intent['direction']`` (free-form
  long/short/neutral). Falls back to neutral when missing.
- Horizon: pulled from ``intent['horizon_days']`` or parsed from
  ``intent['exit_horizon']`` (e.g. ``"14d"``).
- Granularity: ``"1d"``. Hourly is reserved for Phase 9.5.
- Window: ``[T0 - horizon, T0]`` where T0 is now. Phase 9.5 will
  replay across N historical T0 windows.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from gecko_core.orchestration.trade_panel.backtest.history_source import (
    COINGECKO_OHLC_BASE_URL,
    PYTH_HERMES_BASE_URL,
    CoinGeckoOhlcHistorySource,
    HistorySource,
    PythHermesHistorySource,
)
from gecko_core.orchestration.trade_panel.backtest.models import (
    BacktestGranularity,
    BacktestIntent,
    BacktestReport,
    BacktestSource,
    Candle,
    TradeDirection,
)
from gecko_core.orchestration.trade_panel.backtest.simulator import simulate_naive_pnl

_log = logging.getLogger(__name__)

_HORIZON_RE = re.compile(r"^\s*(\d+)\s*([dhwm])\s*$", re.IGNORECASE)


def _parse_horizon_days(raw: Any) -> int | None:
    """Coerce a Strategist horizon hint into an integer day count.

    Accepts: ``int`` (interpreted as days), ``"14d"``, ``"3w"``, ``"6h"``,
    ``"1m"`` (months ~ 30d). Returns ``None`` when unparseable.
    """
    if isinstance(raw, int) and raw > 0:
        return raw
    if isinstance(raw, str):
        m = _HORIZON_RE.match(raw)
        if not m:
            return None
        n = int(m.group(1))
        unit = m.group(2).lower()
        if n <= 0:
            return None
        if unit == "d":
            return n
        if unit == "h":
            return max(1, n // 24)
        if unit == "w":
            return n * 7
        if unit == "m":
            return n * 30
    return None


def _coerce_direction(raw: Any) -> TradeDirection:
    if isinstance(raw, str):
        v = raw.strip().lower()
        if v in {"long", "short", "neutral"}:
            return v  # type: ignore[return-value]
    return "neutral"


def _normalize_intent(raw: dict[str, Any]) -> BacktestIntent | None:
    """Squash a free-form Strategist intent dict into ``BacktestIntent``.

    Returns ``None`` when the protocol is missing — the caller emits an
    unbacktestable report rather than raising. Direction defaults to
    ``"neutral"`` (which the simulator rejects with a stable reason).
    """
    protocol = raw.get("protocol")
    if not isinstance(protocol, str) or not protocol.strip():
        return None
    horizon = _parse_horizon_days(raw.get("horizon_days") or raw.get("exit_horizon"))
    if horizon is None:
        horizon = 14  # founder-pick default per the spec
    direction = _coerce_direction(raw.get("direction"))
    size_pct_raw = raw.get("size_pct")
    try:
        size_pct = float(size_pct_raw) if size_pct_raw is not None else 1.0
    except (TypeError, ValueError):
        size_pct = 1.0
    size_pct = max(0.0, min(100.0, size_pct))
    stop_raw = raw.get("stop_loss_pct")
    stop_loss_pct: float | None
    try:
        stop_loss_pct = float(stop_raw) if stop_raw is not None else None
    except (TypeError, ValueError):
        stop_loss_pct = None

    return BacktestIntent(
        protocol=protocol.strip().lower(),
        direction=direction,
        horizon_days=max(1, min(365, horizon)),
        size_pct=size_pct,
        stop_loss_pct=stop_loss_pct,
    )


_SECONDS_PER_DAY = 86_400


async def backtest_intent(
    intent: dict[str, Any],
    history: HistorySource,
    *,
    granularity: BacktestGranularity = "1d",
    now_ts: int | None = None,
) -> BacktestReport:
    """Replay a Strategist intent against historical candles.

    Always returns a :class:`BacktestReport`. When the price corpus can't
    satisfy the intent, the report has ``unbacktestable=True`` and a
    stable ``reason`` (``"invalid_intent"``, ``"pyth_no_history"``,
    ``"no_candles"``, ``"neutral_direction"``, ``"invalid_entry_price"``).

    Args:
        intent: Raw Strategist intent dict. Recognized keys:
            ``protocol``, ``direction``, ``horizon_days`` or
            ``exit_horizon`` (e.g. ``"14d"``), ``size_pct``,
            ``stop_loss_pct``.
        history: Anything implementing :class:`HistorySource`.
        granularity: Candle bucket. Defaults to ``"1d"`` per founder pick.
        now_ts: Override "now" for deterministic tests. Defaults to
            ``time.time()``.
    """
    normalized = _normalize_intent(intent)
    if normalized is None:
        return BacktestReport(unbacktestable=True, reason="invalid_intent")

    t_now = int(now_ts if now_ts is not None else time.time())
    ts_start = t_now - normalized.horizon_days * _SECONDS_PER_DAY
    ts_end = t_now

    candles = await history.fetch(
        normalized.protocol,
        granularity=granularity,
        ts_start=ts_start,
        ts_end=ts_end,
    )
    if not candles:
        return BacktestReport(
            unbacktestable=True,
            reason="pyth_no_history",
            source="pyth",
        )

    # Source label: trust the candles. Mixed-source slices fall back to
    # the first candle's source token (v1 doesn't blend providers).
    src_token = candles[0].source if candles else "pyth"
    return simulate_naive_pnl(normalized, candles, source=src_token)


__all__ = [
    "COINGECKO_OHLC_BASE_URL",
    "PYTH_HERMES_BASE_URL",
    "BacktestGranularity",
    "BacktestIntent",
    "BacktestReport",
    "BacktestSource",
    "Candle",
    "CoinGeckoOhlcHistorySource",
    "HistorySource",
    "PythHermesHistorySource",
    "TradeDirection",
    "backtest_intent",
    "simulate_naive_pnl",
]
