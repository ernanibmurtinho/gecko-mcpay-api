"""Assert the backtest harness invokes the SAME primitive functions the
runtime uses — no parallel implementation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from gecko_core.trade_agent.backtest import (
    FixtureHistorySource,
    OptimisticOracleFixture,
)
from gecko_core.trade_agent.backtest.harness import BacktestHarness
from gecko_core.trade_agent.spec import load_spec

FIXTURE_DIR = Path(__file__).parent.parent.parent / "fixtures" / "backtest"


def _spec() -> dict[str, Any]:
    return {
        "name": "reuse-buy-dip",
        "version": "0.1.0",
        "vertical": "dex",
        "protocol": "okx",
        "chain": "solana",
        "entry": {"primitive": "buy_dip", "params": {"drawdown_pct": 5}, "rule_id": "r1"},
        "exit": {"primitive": "take_profit", "params": {"target_pct": 8}, "rule_id": "r2"},
        "sizing": {"primitive": "fixed_usd", "params": {"amount_usd": 1000}, "rule_id": "r3"},
        "filter": {"require_oracle_act": True, "block_honeypot": True},
        "risk": {
            "max_concurrent_positions": 3,
            "max_single_position_pct": 50,
            "max_daily_loss_pct": 50,
        },
        "oracle_grounding": {
            "tool": "gecko_trade_research",
            "rule_verdicts": [
                {"rule_id": "r1", "verdict_id": "v-seed", "verdict": "act", "citations": ["c"]}
            ],
        },
    }


@pytest.mark.asyncio
async def test_harness_calls_runtime_primitives(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wrap the canonical primitives in the harness's import namespace
    and verify the wrapped versions actually fired during replay.

    The harness imports ``evaluate_entry`` etc. from
    :mod:`gecko_core.trade_agent.primitives` at module-load time, so we
    patch attributes on the harness module — that's where the names are
    bound.
    """
    from gecko_core.trade_agent.backtest import harness as harness_mod
    from gecko_core.trade_agent.primitives import (
        entry as entry_mod,
    )
    from gecko_core.trade_agent.primitives import (
        exit as exit_mod,
    )
    from gecko_core.trade_agent.primitives import (
        sizing as sizing_mod,
    )

    seen: dict[str, int] = {"entry": 0, "exit": 0, "size": 0}

    real_entry = entry_mod.evaluate_entry
    real_exit = exit_mod.evaluate_exit
    real_size = sizing_mod.compute_size_usd

    def spy_entry(spec_entry, event):  # type: ignore[no-untyped-def]
        seen["entry"] += 1
        return real_entry(spec_entry, event)

    def spy_exit(spec_exit, position, event):  # type: ignore[no-untyped-def]
        seen["exit"] += 1
        return real_exit(spec_exit, position, event)

    def spy_size(spec_sizing, *, bankroll_usd, verdict=None):  # type: ignore[no-untyped-def]
        seen["size"] += 1
        return real_size(spec_sizing, bankroll_usd=bankroll_usd, verdict=verdict)

    monkeypatch.setattr(harness_mod, "evaluate_entry", spy_entry)
    monkeypatch.setattr(harness_mod, "evaluate_exit", spy_exit)
    monkeypatch.setattr(harness_mod, "compute_size_usd", spy_size)

    spec = load_spec(_spec())
    history = FixtureHistorySource(FIXTURE_DIR / "solana_30d_synthetic.json")
    oracle = OptimisticOracleFixture()
    await BacktestHarness(spec, history=history, oracle=oracle).run(gating="on", window_days=30)

    # 30 events; entry evaluated each step; size only on trigger events.
    assert seen["entry"] >= 30
    assert seen["size"] > 0
    # Exit only invoked when a position is open; at least one trade should
    # have opened and been re-evaluated.
    assert seen["exit"] > 0
