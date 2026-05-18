"""S24 WS-A — DeFi trade-verdict eval scorer.

Reads `tests/eval/suites/defi_trade_suite.json`, invokes the 7-voice trade
panel via `run_trade_panel_with_retrieval` (the production entry point used
by the MCP tool + REST endpoint), groups verdicts by (verdict_type x
confidence_bucket), and computes:

  - act-30d-profit-rate     ::  fraction of `act` verdicts whose
                                known_outcome_at_30d.profit_pct > 0
  - pass-drawdown-avoid-rate::  fraction of `pass` verdicts whose
                                known_outcome_at_30d.max_drawdown_pct <= -15
                                (i.e. pass correctly avoided a meaningful dd)
  - defer-rate              ::  fraction of total verdicts that are `defer`
  - brier_by_bucket         ::  Brier(prob_act_correct, actual_act_correct)
                                across {low, med, high} confidence buckets

Pattern E guardrail: the panel MUST NOT see `known_outcome_at_30d`. The
script ASSERTS that the field is not in the kwargs forwarded to the panel
before any call lands.

Usage:
    uv run python -m tests.eval.scripts.score_defi_trade_suite \\
        --limit 5 --tier pro

    uv run python -m tests.eval.scripts.score_defi_trade_suite --limit 10

Outputs JSON report to `tests/eval/live_runs/<date>-s24-defi-trade-{N}.json`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
SUITE_PATH = REPO_ROOT / "tests" / "eval" / "suites" / "defi_trade_suite.json"
LIVE_RUNS_DIR = REPO_ROOT / "tests" / "eval" / "live_runs"

# Confidence buckets — anchored to the coordinator prompt's 0.3/0.8 anchors.
_LOW_CONF = 0.5
_HIGH_CONF = 0.7


def _bucket(confidence: float) -> str:
    if confidence < _LOW_CONF:
        return "low"
    if confidence < _HIGH_CONF:
        return "med"
    return "high"


def _outcome_act_correct(known: dict[str, Any]) -> bool:
    """`act` is correct iff 30d profit > 0 AND dd not worse than -15%."""
    profit = known.get("profit_pct")
    dd = known.get("max_drawdown_pct")
    if profit is None:
        # Auxiliary-outcome fixtures (Jito tip band): label encodes truth.
        label = str(known.get("label", ""))
        return "act" in label or "won" in label or "played_out" in label
    if dd is None:
        return profit > 0
    return profit > 0 and dd > -15.0


def _outcome_pass_correct(known: dict[str, Any]) -> bool:
    """`pass` is correct iff the asset would have drawn down >= 15% OR returned <= 0."""
    profit = known.get("profit_pct")
    dd = known.get("max_drawdown_pct")
    if profit is None:
        label = str(known.get("label", ""))
        return "broken" in label or "lost" in label or "trap" in label
    if dd is not None and dd <= -15.0:
        return True
    return profit is not None and profit <= 0


def _load_fixtures(limit: int | None) -> list[dict[str, Any]]:
    with SUITE_PATH.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise SystemExit(f"suite root is not a list: {SUITE_PATH}")
    if limit is not None:
        data = data[:limit]
    return data


def _build_llm_config(api_key: str, tier: str) -> dict[str, Any]:
    # Pro tier uses gpt-4o for coordinator + bull_bear_debater (higher
    # synthesis quality); analysts stay on gpt-4o-mini for cost. Basic
    # uses gpt-4o-mini end-to-end.
    model = "gpt-4o-mini" if tier == "basic" else "gpt-4o"
    return {
        "config_list": [{"model": model, "api_key": api_key}],
        "temperature": 0.2,
    }


async def _score_one(
    fixture: dict[str, Any],
    *,
    tier: str,
    llm_config: dict[str, Any],
) -> dict[str, Any]:
    """Run the panel against a single fixture; never leak the outcome.

    The panel is invoked through the same public entry point the MCP tool
    uses, so retrieval is exercised end-to-end (Pattern E).
    """
    # Lazy import — keeps scaffolding callable in CI without AG2.
    from gecko_core.orchestration.trade_panel import run_trade_panel_with_retrieval

    # Pattern E reachability guardrail — the panel call sees only
    # (idea, protocol, vertical, tier). The outcome stays in this scope.
    panel_kwargs: dict[str, Any] = {
        "idea": fixture["question"],
        "protocol": fixture["protocol"],
        "vertical": fixture.get("vertical", "dex"),
        "tier": tier,
        "llm_config": llm_config,
    }
    forbidden = {"known_outcome_at_30d", "outcome_source", "corpus_freeze_date"}
    leaked = forbidden & set(panel_kwargs)
    assert not leaked, f"outcome leakage to panel: {leaked}"
    # Also assert no outcome string appears in the question itself.
    q = panel_kwargs["idea"]
    assert "known_outcome" not in q and "max_drawdown" not in q, (
        "fixture question text leaks outcome metadata"
    )

    t0 = time.perf_counter()
    verdict = await run_trade_panel_with_retrieval(**panel_kwargs)
    wall = time.perf_counter() - t0

    return {
        "id": fixture["id"],
        "protocol": fixture["protocol"],
        "verdict": verdict.verdict,
        "confidence": verdict.confidence,
        "dissent_count": verdict.dissent_count,
        "blocker_questions": list(verdict.blocker_questions),
        # S35-#99 — verdict envelope split into two cite lists.
        "n_citations": len(getattr(verdict, "evidence_citations", []) or [])
        + len(getattr(verdict, "framework_context", []) or []),
        "wall_seconds": round(wall, 2),
        # Outcome attached AFTER the panel returns, for scoring only.
        "known_outcome": fixture["known_outcome_at_30d"],
    }


def _aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    if n == 0:
        return {"n": 0}

    by_verdict: dict[str, list[dict[str, Any]]] = {"act": [], "pass": [], "defer": []}
    for r in rows:
        v = r["verdict"]
        if v in by_verdict:
            by_verdict[v].append(r)

    act_rows = by_verdict["act"]
    pass_rows = by_verdict["pass"]

    act_profit_rate = (
        sum(1 for r in act_rows if _outcome_act_correct(r["known_outcome"])) / len(act_rows)
        if act_rows
        else None
    )
    pass_dd_avoid_rate = (
        sum(1 for r in pass_rows if _outcome_pass_correct(r["known_outcome"])) / len(pass_rows)
        if pass_rows
        else None
    )
    defer_rate = len(by_verdict["defer"]) / n

    # Brier score: prob = confidence when verdict==act; (1 - confidence)
    # when verdict==pass. `defer` is excluded (not a directional call).
    # `actual = 1` iff the directional call was correct per outcome rules.
    brier_terms_by_bucket: dict[str, list[float]] = {"low": [], "med": [], "high": []}
    for r in rows:
        v = r["verdict"]
        if v == "defer":
            continue
        if v == "act":
            prob = r["confidence"]
            actual = 1.0 if _outcome_act_correct(r["known_outcome"]) else 0.0
        else:  # pass
            prob = r["confidence"]
            actual = 1.0 if _outcome_pass_correct(r["known_outcome"]) else 0.0
        brier_terms_by_bucket[_bucket(r["confidence"])].append((prob - actual) ** 2)

    brier_by_bucket = {
        k: (round(statistics.mean(v), 3) if v else None) for k, v in brier_terms_by_bucket.items()
    }
    all_terms = [t for lst in brier_terms_by_bucket.values() for t in lst]
    brier_overall = round(statistics.mean(all_terms), 3) if all_terms else None

    return {
        "n": n,
        "verdict_distribution": {k: len(v) for k, v in by_verdict.items()},
        "act_30d_profit_rate": (round(act_profit_rate, 3) if act_profit_rate is not None else None),
        "pass_drawdown_avoid_rate": (
            round(pass_dd_avoid_rate, 3) if pass_dd_avoid_rate is not None else None
        ),
        "defer_rate": round(defer_rate, 3),
        "brier_overall": brier_overall,
        "brier_by_bucket": brier_by_bucket,
    }


async def run(*, limit: int | None, tier: str) -> int:
    fixtures = _load_fixtures(limit)
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit("OPENAI_API_KEY required for live trade-panel runs.")
    llm_config = _build_llm_config(api_key, tier)

    print(f"S24 WS-A defi-trade scorer | n={len(fixtures)} | tier={tier} | suite={SUITE_PATH.name}")
    rows: list[dict[str, Any]] = []
    for f in fixtures:
        print(f"  [{f['id']}] protocol={f['protocol']} as_of={f['as_of_date']} ... ", end="")
        sys.stdout.flush()
        try:
            row = await _score_one(f, tier=tier, llm_config=llm_config)
        except Exception as exc:
            print(f"FAIL ({type(exc).__name__}: {exc})")
            continue
        print(
            f"verdict={row['verdict']:<5} conf={row['confidence']:.2f} "
            f"cites={row['n_citations']} dissent={row['dissent_count']} "
            f"wall={row['wall_seconds']:.1f}s"
        )
        rows.append(row)

    agg = _aggregate(rows)
    print(f"\nAggregate: {json.dumps(agg, indent=2)}")

    LIVE_RUNS_DIR.mkdir(parents=True, exist_ok=True)
    date = time.strftime("%Y-%m-%d")
    base = LIVE_RUNS_DIR / f"{date}-s24-defi-trade-{len(rows)}.json"
    out_path = base
    i = 2
    while out_path.exists():
        out_path = LIVE_RUNS_DIR / f"{date}-s24-defi-trade-{len(rows)}-{i}.json"
        i += 1

    payload = {
        "date": date,
        "tier": tier,
        "suite": SUITE_PATH.name,
        "n_fixtures_loaded": len(fixtures),
        "n_scored": len(rows),
        "aggregate": agg,
        "rows": rows,
    }
    out_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"Saved -> {out_path}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m tests.eval.scripts.score_defi_trade_suite",
        description="S24 WS-A defi-trade verdict scorer (live panel calls).",
    )
    p.add_argument("--limit", type=int, default=None, help="cap at first N fixtures (dry-run)")
    p.add_argument("--tier", choices=("basic", "pro"), default="pro")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    return asyncio.run(run(limit=args.limit, tier=args.tier))


if __name__ == "__main__":
    sys.exit(main())
