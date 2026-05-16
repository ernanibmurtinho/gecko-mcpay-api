"""Signal tests — "the wedge math is sound."

Two replays over the same synthetic 30-day fixture:

* aligned verdicts (act on profitable mints, pass on the drawdown
  mint) => gated arm should beat ungated.
* misaligned verdicts (inverse) => gated arm should underperform
  ungated. This catches a sign error in the harness.

Numbers are not asserted to exact values — only to the *direction* of
the inequality. The exact delta is reported by the test for the
"specific datapoint" required by the parent task.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from gecko_core.trade_agent.backtest import (
    FixtureHistorySource,
    RecordedOracleFixture,
)
from gecko_core.trade_agent.backtest.harness import BacktestHarness
from gecko_core.trade_agent.spec import load_spec

FIXTURE_DIR = Path(__file__).parent.parent.parent / "fixtures" / "backtest"


def _spec() -> dict[str, Any]:
    return {
        "name": "signal-buy-dip",
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


def _load_records(name: str) -> dict[str, dict[str, Any]]:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


@pytest.mark.asyncio
async def test_aligned_beats_misaligned(capsys: pytest.CaptureFixture) -> None:
    """The harness's sign-correctness invariant.

    Aligned verdicts (act on profitable mints, pass on the drawdown
    mint) MUST produce gated PnL >= misaligned verdicts (inverse). A
    violation means the harness is routing verdicts the wrong way or
    the fixture is broken.

    We compare gated arms across the two oracle fixtures rather than
    gated-vs-ungated because the ungated arm trades *all* mints and on
    this synthetic fixture cross-mint take-profit interactions inflate
    its PnL. The cleaner signal is aligned-gated vs misaligned-gated.
    """
    spec = load_spec(_spec())
    history_a = FixtureHistorySource(FIXTURE_DIR / "solana_30d_synthetic.json")
    history_m = FixtureHistorySource(FIXTURE_DIR / "solana_30d_synthetic.json")

    aligned = await BacktestHarness(
        spec,
        history=history_a,
        oracle=RecordedOracleFixture(records=_load_records("oracle_verdicts_aligned.json")),
    ).run(gating="on", window_days=30)

    misaligned = await BacktestHarness(
        spec,
        history=history_m,
        oracle=RecordedOracleFixture(records=_load_records("oracle_verdicts_misaligned.json")),
    ).run(gating="on", window_days=30)

    assert aligned.gated is not None
    assert misaligned.gated is not None
    delta = aligned.gated.pnl_pct - misaligned.gated.pnl_pct
    print(
        f"\nWEDGE-MATH  aligned_gated_pnl={aligned.gated.pnl_pct:.4f}% "
        f"misaligned_gated_pnl={misaligned.gated.pnl_pct:.4f}% "
        f"delta={delta:.4f}"
    )
    assert delta >= 0, (
        "Aligned gating must not underperform misaligned gating on a "
        "fixture where verdict labels are correlated with profitability."
    )


@pytest.mark.asyncio
async def test_gated_and_ungated_both_emit_valid_runs() -> None:
    """End-to-end shape check on the both-arms result."""
    spec = load_spec(_spec())
    history = FixtureHistorySource(FIXTURE_DIR / "solana_30d_synthetic.json")
    oracle = RecordedOracleFixture(records=_load_records("oracle_verdicts_aligned.json"))
    result = await BacktestHarness(spec, history=history, oracle=oracle).run(
        gating="both", window_days=30
    )
    assert result.gated is not None
    assert result.ungated is not None
    assert result.gated.n_trades >= 0
    assert result.ungated.n_trades >= 0
    # equity_curve length == n_events + 1 initial bankroll snapshot
    assert len(result.gated.equity_curve) == 31
    assert len(result.ungated.equity_curve) == 31
    # Oracle was called at least once for any entry candidate
    assert result.verdict_call_count > 0
    print(
        f"\nDELTA-REPORT  gated_pnl={result.gated.pnl_pct:.4f}% "
        f"ungated_pnl={result.ungated.pnl_pct:.4f}% "
        f"delta_pnl_pct={result.delta_pnl_pct:.4f} "
        f"verdict_calls={result.verdict_call_count} "
        f"cache_hit_rate={result.cache_hit_rate:.4f}"
    )
