"""Backtest harness — replay a strategy spec against historical data.

Two parallel arms over the same event stream:

* **gated** — every entry candidate is verdict-checked via the offline
  oracle fixture; trades only execute on ``verdict == "act"`` (and
  ``passes_filter`` ack of ``require_oracle_act``).
* **ungated** — same primitive-emitted candidates execute regardless.

Both arms share the *same* primitive call sequence — the harness
imports :mod:`gecko_core.trade_agent.primitives` directly so backtest
results predict live runtime behaviour. (See ``test_primitives_reuse``.)

Per Section 1.7 of the co-founder roadmap brief, the harness exists to
answer one question: *does verdict gating beat the ungated baseline by
>= +200 bps median?* The output ``delta_pnl_pct`` is the answer for one
run; the eval suite aggregates medians across many specs.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from gecko_core.trade_agent.backtest.history import (
    FixtureHistorySource,
    HistorySource,
)
from gecko_core.trade_agent.backtest.metrics import (
    hit_rate,
    max_drawdown_pct,
    pnl_pct,
    returns_from_equity,
    sharpe_annualized,
)
from gecko_core.trade_agent.backtest.models import (
    BacktestResult,
    BacktestRun,
    GatingMode,
    Trade,
)
from gecko_core.trade_agent.backtest.oracle_fixture import (
    PessimisticOracleFixture,
    RecordedOracleFixture,
)
from gecko_core.trade_agent.primitives import (
    RiskState,
    compute_size_usd,
    evaluate_entry,
    evaluate_exit,
    passes_filter,
    would_breach,
)
from gecko_core.trade_agent.spec import AgentSpec, load_spec
from gecko_core.trade_agent.state.models import AgentPosition

logger = logging.getLogger(__name__)

OracleCallable = Callable[..., Awaitable[dict[str, Any]]]

_INITIAL_BANKROLL = 10_000.0


def _ev_ts(ev: dict[str, Any], fallback: float) -> float:
    ts = ev.get("ts")
    return float(ts) if ts is not None else fallback


def _open_position(
    *,
    agent_id: str,
    mint: str,
    size_usd: float,
    entry_price: float,
    counter: int,
) -> AgentPosition:
    return AgentPosition(
        agent_id=agent_id,
        position_id=f"bt-pos-{counter:06d}",
        status="open",
        mint=mint,
        side="long",
        size_usd=size_usd,
        entry_price=entry_price,
    )


async def _run_arm(
    *,
    spec: AgentSpec,
    events: list[dict[str, Any]],
    gating: Literal["on", "off"],
    oracle: OracleCallable | None,
) -> BacktestRun:
    """Replay ``events`` once for one arm (gated or ungated)."""
    bankroll = _INITIAL_BANKROLL
    equity_curve: list[float] = [bankroll]
    trades: list[Trade] = []
    open_positions: dict[str, tuple[AgentPosition, dict[str, Any]]] = {}
    pos_counter = 0
    agent_id = f"backtest-{spec.spec_id}"

    for idx, ev in enumerate(events):
        ts = _ev_ts(ev, float(idx))
        # 1) Exit check on every open position first.
        for pid, (pos, _verdict) in list(open_positions.items()):
            decision = evaluate_exit(spec.exit, pos, ev)
            if decision is None:
                continue
            price = float(ev.get("price", pos.entry_price or 0.0))
            entry_price = pos.entry_price or price
            size = pos.size_usd or 0.0
            if entry_price > 0:
                pnl = size * ((price - entry_price) / entry_price)
            else:
                pnl = 0.0
            bankroll += pnl
            trades.append(
                Trade(
                    entry_ts=float(pos.opened_at.timestamp())
                    if hasattr(pos.opened_at, "timestamp")
                    else 0.0,
                    exit_ts=ts,
                    entry_price=entry_price,
                    exit_price=price,
                    size=size,
                    side=pos.side or "long",
                    verdict_id=(_verdict or {}).get("verdict_id"),
                    pnl=round(pnl, 6),
                )
            )
            open_positions.pop(pid, None)

        # 2) Entry candidate from primitives.
        candidate = evaluate_entry(spec.entry, ev)
        if candidate is not None:
            verdict: dict[str, Any] | None = None
            if gating == "on" and oracle is not None:
                verdict = await oracle(
                    idea=candidate.idea,
                    tier="basic",
                    agent_id=agent_id,
                )
            # Filter — in ungated mode we synthesize an `act` verdict so
            # the filter's require_oracle_act check doesn't blanket-veto
            # the ungated arm. The point of ungated is "no oracle
            # influence" — not "filter forbids everything."
            filter_verdict = verdict if gating == "on" else {"verdict": "act"}
            passes, _reason = passes_filter(spec.filter, ev, filter_verdict)
            if passes:
                # Sizing
                size_usd = compute_size_usd(
                    spec.sizing,
                    bankroll_usd=bankroll,
                    verdict=verdict,
                )
                candidate.nominal_size_usd = size_usd
                # Risk
                risk_state = RiskState(
                    open_positions=len(open_positions),
                    bankroll_usd=bankroll,
                    daily_loss_pct=0.0,
                    surviving_dissent_strength=None,
                )
                breach = would_breach(spec.risk, risk_state, candidate)
                if breach is None and size_usd > 0:
                    price = float(ev.get("price", 0.0))
                    if price > 0:
                        pos_counter += 1
                        pos = _open_position(
                            agent_id=agent_id,
                            mint=candidate.mint,
                            size_usd=size_usd,
                            entry_price=price,
                            counter=pos_counter,
                        )
                        open_positions[pos.position_id] = (pos, verdict or {})

        # 3) Mark-to-market the equity curve at end of step.
        mtm = bankroll
        last_price = float(ev.get("price", 0.0))
        for pos, _v in open_positions.values():
            if pos.entry_price and last_price > 0:
                mtm += (pos.size_usd or 0.0) * ((last_price - pos.entry_price) / pos.entry_price)
        equity_curve.append(round(mtm, 6))

    # Force-close any still-open positions at the last seen price so
    # the PnL is realised. Mirrors a runtime shutdown sweep.
    if events and open_positions:
        last_price = float(events[-1].get("price", 0.0))
        last_ts = _ev_ts(events[-1], float(len(events)))
        for _pid, (pos, _verdict) in list(open_positions.items()):
            entry_price = pos.entry_price or last_price
            size = pos.size_usd or 0.0
            if entry_price > 0 and last_price > 0:
                pnl = size * ((last_price - entry_price) / entry_price)
            else:
                pnl = 0.0
            bankroll += pnl
            trades.append(
                Trade(
                    entry_ts=0.0,
                    exit_ts=last_ts,
                    entry_price=entry_price,
                    exit_price=last_price,
                    size=size,
                    side=pos.side or "long",
                    verdict_id=(_verdict or {}).get("verdict_id"),
                    pnl=round(pnl, 6),
                )
            )
        open_positions.clear()
        # Replace final mark with realised bankroll.
        if equity_curve:
            equity_curve[-1] = round(bankroll, 6)

    returns = returns_from_equity(equity_curve)
    return BacktestRun(
        gating_mode=gating,
        sharpe=sharpe_annualized(returns),
        max_dd_pct=max_drawdown_pct(equity_curve),
        pnl_pct=pnl_pct(equity_curve),
        hit_rate=hit_rate(trades),
        n_trades=len(trades),
        equity_curve=equity_curve,
        trades=trades,
    )


class BacktestHarness:
    """Object-oriented entry point. Most callers use :func:`gecko_backtest`."""

    def __init__(
        self,
        spec: AgentSpec,
        *,
        history: HistorySource,
        oracle: OracleCallable | None = None,
    ) -> None:
        self.spec = spec
        self.history = history
        self.oracle = oracle

    async def _materialise(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        async for ev in self.history.__aiter__():
            out.append(ev)
        return out

    async def run(
        self,
        *,
        gating: GatingMode = "both",
        window_days: int = 90,
    ) -> BacktestResult:
        events = await self._materialise()
        gated: BacktestRun | None = None
        ungated: BacktestRun | None = None

        if gating in ("on", "both"):
            gated = await _run_arm(
                spec=self.spec,
                events=events,
                gating="on",
                oracle=self.oracle,
            )
        if gating in ("off", "both"):
            ungated = await _run_arm(
                spec=self.spec,
                events=events,
                gating="off",
                oracle=None,
            )

        delta_pnl = 0.0
        delta_sharpe = 0.0
        if gated is not None and ungated is not None:
            delta_pnl = round(gated.pnl_pct - ungated.pnl_pct, 6)
            delta_sharpe = round(gated.sharpe - ungated.sharpe, 6)

        call_count = 0
        cache_rate = 0.0
        if self.oracle is not None and hasattr(self.oracle, "call_count"):
            call_count = int(getattr(self.oracle, "call_count", 0))
            cache_rate = float(getattr(self.oracle, "cache_hit_rate", 0.0))

        return BacktestResult(
            gated=gated,
            ungated=ungated,
            delta_pnl_pct=delta_pnl,
            delta_sharpe=delta_sharpe,
            verdict_call_count=call_count,
            cache_hit_rate=cache_rate,
            window_days=window_days,
            spec_id=self.spec.spec_id,
        )


async def gecko_backtest(
    spec: dict[str, Any] | AgentSpec,
    *,
    gating: GatingMode = "both",
    window_days: int = 90,
    history_source: HistorySource | None = None,
    oracle_fixture: dict[str, Any] | None = None,
) -> BacktestResult:
    """Replay ``spec`` over a historical window.

    ``history_source`` defaults to a synthetic 30-day fixture if not
    provided — useful as a smoke check; production callers pass a real
    fixture or Mongo source.

    ``oracle_fixture`` is a dict of pre-computed verdicts (a
    :class:`RecordedOracleFixture` is constructed in-place). If ``None``,
    we fall back to :class:`PessimisticOracleFixture` so the gated arm
    has a deterministic floor — the misalignment between this and the
    optimistic fixture is exactly the ``delta_pnl_pct`` signal.

    Per founder constraint: this function NEVER calls
    ``gecko_trade_research`` for real.
    """
    if isinstance(spec, AgentSpec):
        agent_spec = spec
    else:
        agent_spec = load_spec(spec)

    if history_source is None:
        # Smallest reasonable synthetic — a flat tick stream. Tests
        # typically pass an explicit FixtureHistorySource.
        history_source = FixtureHistorySource(
            data={"prices": [{"ts": float(i), "price": 1.0} for i in range(30)], "mint": "SMOKE"}
        )

    oracle: OracleCallable | None
    if oracle_fixture is not None:
        oracle = RecordedOracleFixture(records=oracle_fixture)
    else:
        oracle = PessimisticOracleFixture()

    harness = BacktestHarness(agent_spec, history=history_source, oracle=oracle)
    return await harness.run(gating=gating, window_days=window_days)


def _spec_hash(spec: AgentSpec) -> str:
    """Deterministic short hash for filenames."""
    return hashlib.sha256(spec.spec_id.encode()).hexdigest()[:12]


# Convenience sync wrapper for the CLI bridge — Click handlers are
# easier as sync entry points.
def run_backtest_sync(
    spec: dict[str, Any] | AgentSpec,
    *,
    gating: GatingMode = "both",
    window_days: int = 90,
    history_source: HistorySource | None = None,
    oracle_fixture: dict[str, Any] | None = None,
) -> BacktestResult:
    return asyncio.run(
        gecko_backtest(
            spec,
            gating=gating,
            window_days=window_days,
            history_source=history_source,
            oracle_fixture=oracle_fixture,
        )
    )


__all__ = [
    "BacktestHarness",
    "gecko_backtest",
    "run_backtest_sync",
]
