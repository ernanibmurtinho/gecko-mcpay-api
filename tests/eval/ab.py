"""A/B harness for diffing two Pro prompt versions on the same suite.

Runs `tests.eval.runner` twice — once per prompts version — and prints a
per-idea verdict diff plus an aggregate delta on `verdict_accuracy`,
`median_score`, `median_weighted_score`, and `median_cost_usd`.

Usage::

    # Mock mode (default, $0) — useful for sanity checking new bundles
    # against the deterministic mock scorer:
    uv run python -m tests.eval.ab --suite general --a v5.3 --b v5.4

    # Live mode — costs real money. The harness inherits whatever the
    # runner's `--live` semantics are; pass --reruns 3 for majority-vote.
    uv run python -m tests.eval.ab --suite general --a v5.3 --b v5.4 \\
        --live --reruns 3

The output is two-column: one row per idea, columns are A's verdict vs
B's verdict, with `expected_verdict` for context. Wrong→right flips are
flagged ``[FIXED]``; right→wrong flips are flagged ``[REGRESSED]``.

Exit code:
  0 — B is no worse than A on `verdict_accuracy`.
  1 — B regressed `verdict_accuracy` vs A.

Cost note: this script does NOT enforce its own confirmation prompt. In
live mode you pay 2x the runner's per-pass cost. The S2X-15 gate script
(`scripts/run_eval_gate.sh`) is the only gate-bearing entry point — this
file is a developer tool for prompt iteration.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

from tests.eval import runner

SUITE_NAMES = ("general", "crypto", "saas", "all")


def _set_prompts_version(version: str) -> None:
    """Pin GECKO_PRO_PROMPTS_VERSION and bust the loader cache.

    The loader uses `lru_cache(maxsize=1)`; without busting it the second
    A/B run would see the first version's prompts.
    """
    os.environ["GECKO_PRO_PROMPTS_VERSION"] = version
    try:
        from gecko_core.orchestration.pro.prompts import load_prompts

        load_prompts.cache_clear()
    except ImportError:
        # Mock mode doesn't need the prompts loader at all.
        pass


async def _one_run(
    *,
    suite: str,
    live: bool,
    reruns: int,
    version: str,
) -> dict[str, Any]:
    _set_prompts_version(version)
    return await runner.run_eval(
        live=live,
        reruns=reruns,
        filter_id=None,
        suite=suite,
    )


def _ideas_from_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Pull the per-idea list from either single-suite or all-suites shape."""
    if "ideas" in payload:
        return list(payload["ideas"])
    out: list[dict[str, Any]] = []
    for body in payload.get("suites", {}).values():
        out.extend(body["ideas"])
    return out


def _print_diff(payload_a: dict[str, Any], payload_b: dict[str, Any], *, a: str, b: str) -> int:
    """Print a side-by-side per-idea diff. Returns non-zero on regression."""
    ideas_a = {i["id"]: i for i in _ideas_from_payload(payload_a)}
    ideas_b = {i["id"]: i for i in _ideas_from_payload(payload_b)}
    common = sorted(set(ideas_a) & set(ideas_b))

    print()
    print("=" * 96)
    print(f"  A/B verdict diff   A={a}   B={b}")
    print("=" * 96)
    header = f"  {'idea':<48} {'expected':<8} {'A':<7} {'B':<7} {'change'}"
    print(header)
    print("  " + "-" * 94)

    fixed = 0
    regressed = 0
    for idea_id in common:
        ra = ideas_a[idea_id]
        rb = ideas_b[idea_id]
        expected = str(ra["expected_verdict"])
        va = str(ra["actual_verdict"])
        vb = str(rb["actual_verdict"])
        a_correct = runner._normalize_verdict(va) == runner._normalize_verdict(expected)
        b_correct = runner._normalize_verdict(vb) == runner._normalize_verdict(expected)
        if not a_correct and b_correct:
            tag = "[FIXED]"
            fixed += 1
        elif a_correct and not b_correct:
            tag = "[REGRESSED]"
            regressed += 1
        elif va == vb:
            tag = ""
        else:
            tag = "[CHANGED]"
        print(f"  {idea_id:<48} {expected:<8} {va:<7} {vb:<7} {tag}")

    agg_a = payload_a["aggregate"]
    agg_b = payload_b["aggregate"]
    print()
    print(f"  Aggregate (A={a})  : {json.dumps(agg_a)}")
    print(f"  Aggregate (B={b})  : {json.dumps(agg_b)}")
    delta_acc = agg_b["verdict_accuracy"] - agg_a["verdict_accuracy"]
    delta_med = agg_b.get("median_weighted_score", 0.0) - agg_a.get("median_weighted_score", 0.0)
    delta_cost = agg_b["median_cost_usd"] - agg_a["median_cost_usd"]
    print(
        f"  Delta              : "
        f"verdict_accuracy={delta_acc:+.3f}  "
        f"median_weighted_score={delta_med:+.3f}  "
        f"median_cost_usd={delta_cost:+.4f}"
    )
    print(f"  FIXED: {fixed}    REGRESSED: {regressed}    n={len(common)}")
    print()

    return 1 if delta_acc < 0 else 0


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m tests.eval.ab",
        description="A/B-diff two Pro prompts versions on the same suite.",
    )
    p.add_argument(
        "--suite",
        choices=list(SUITE_NAMES),
        default="general",
        help="suite to run on both sides (default: general)",
    )
    p.add_argument("--a", required=True, help="prompts version A (e.g. v5.3)")
    p.add_argument("--b", required=True, help="prompts version B (e.g. v5.4)")
    p.add_argument("--live", action="store_true", help="live mode (costs 2x runner per-pass)")
    p.add_argument("--reruns", type=int, default=1, help="passes per idea (passed to runner)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    print(f"[A/B] running suite={args.suite} mode={'LIVE' if args.live else 'mock'} A={args.a}")
    payload_a = asyncio.run(
        _one_run(suite=args.suite, live=args.live, reruns=args.reruns, version=args.a)
    )
    print(f"[A/B] running suite={args.suite} mode={'LIVE' if args.live else 'mock'} B={args.b}")
    payload_b = asyncio.run(
        _one_run(suite=args.suite, live=args.live, reruns=args.reruns, version=args.b)
    )
    return _print_diff(payload_a, payload_b, a=args.a, b=args.b)


if __name__ == "__main__":
    sys.exit(main())
