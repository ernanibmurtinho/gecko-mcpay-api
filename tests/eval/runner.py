"""Pro tier eval harness runner.

Defaults:
  --live False    — uses canned transcripts from `tests/eval/mocks.py`
  --reruns 1      — single pass per idea
  --baseline None — no diff; just save a new baseline JSON

Usage:
    uv run python -m tests.eval.runner
    uv run python -m tests.eval.runner --live
    uv run python -m tests.eval.runner --live --reruns 3
    uv run python -m tests.eval.runner --idea bad-uber-for-dogwalkers
    uv run python -m tests.eval.runner --baseline tests/eval/baselines/2026-04-29.json

Design notes:
  - The harness imports `gecko_core.orchestration.pro.generate` only when
    `--live` is set, so mock runs don't pull AG2 into the import graph.
  - JSON output is the canonical artifact. We don't write to Supabase or
    session_costs — this is a developer tool, not part of the user pipeline.
  - Exit code is non-zero on >15% regression on any of: verdict_accuracy,
    median_score, median_cost_usd. Used by CI.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from tests.eval.mocks import MOCK_TRANSCRIPTS, MockTranscript, get_mock_transcript
from tests.eval.rubric import (
    RubricScores,
    extract_verdict,
    score_transcript_live,
    score_transcript_mock,
)

EVAL_DIR = Path(__file__).parent
IDEAS_PATH = EVAL_DIR / "ideas.yaml"
BASELINES_DIR = EVAL_DIR / "baselines"

# Used to estimate cost in mock mode and as a sanity floor in live mode.
# These are the OpenRouter passthrough rates for the default model matrix
# at time of writing (April 2026). Update with the routing matrix from S1-02.
COST_PER_1K_IN_USD = 0.00015  # gpt-4o-mini in
COST_PER_1K_OUT_USD = 0.0006  # gpt-4o-mini out


@dataclass
class IdeaResult:
    id: str
    expected_verdict: str
    actual_verdict: str
    scores: dict[str, float]
    wall_seconds: float
    tokens_total: int
    cost_usd: float


def _load_ideas(filter_id: str | None) -> list[dict[str, str]]:
    with IDEAS_PATH.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    ideas: list[dict[str, str]] = list(data["ideas"])
    if filter_id:
        ideas = [i for i in ideas if i["id"] == filter_id]
        if not ideas:
            raise SystemExit(f"no idea with id={filter_id!r}")
    return ideas


def _git_sha() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=EVAL_DIR.parent.parent, text=True
        )
        return out.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


def _estimate_cost(tokens_in: int, tokens_out: int) -> float:
    return round(
        (tokens_in / 1000) * COST_PER_1K_IN_USD + (tokens_out / 1000) * COST_PER_1K_OUT_USD,
        4,
    )


def _transcript_total_tokens(t: MockTranscript) -> int:
    return sum(turn["tokens_in"] + turn["tokens_out"] for turn in t.values())


async def _run_live(idea_text: str) -> MockTranscript:
    """Run the real Pro debate; return a transcript shaped like a MockTranscript."""
    # Imported here so mock mode doesn't pay AG2 import cost.
    import os

    from gecko_core.orchestration.pro import generate

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit(
            "OPENAI_API_KEY is not set; --live requires it. "
            "Run without --live to use mock mode (default, $0)."
        )

    llm_config = {
        "config_list": [{"model": "gpt-4o-mini", "api_key": api_key}],
        "temperature": 0.3,
    }
    transcript = await generate(idea=idea_text, rag_context="", llm_config=llm_config)
    out: MockTranscript = {}
    for turn in transcript.turns:
        out[turn.agent] = {
            "text": turn.content,
            "tokens_in": turn.tokens_in,
            "tokens_out": turn.tokens_out,
        }
    return out


def _normalize_verdict(v: str) -> str:
    """Treat `pivot` as kill-equivalent for accuracy scoring.

    The judge often softens a real kill into a pivot recommendation; treating
    them as the same bucket keeps verdict_accuracy honest without forcing
    the judge into a binary it doesn't want.
    """
    return "kill" if v in ("kill", "pivot") else v


async def _evaluate_one(
    idea: dict[str, str],
    *,
    live: bool,
    reruns: int,
    cohort_median_tokens: float,
) -> IdeaResult:
    """Run one idea `reruns` times, average the scores, return an IdeaResult."""
    score_accum: list[RubricScores] = []
    verdicts: list[str] = []
    wall_accum: list[float] = []
    tokens_accum: list[int] = []
    cost_accum: list[float] = []

    for _ in range(reruns):
        t0 = time.perf_counter()
        if live:
            transcript = await _run_live(idea["text"])
            scores = score_transcript_live(transcript)
        else:
            transcript = get_mock_transcript(idea["id"])
            scores = score_transcript_mock(transcript, cohort_median_tokens)
        wall = time.perf_counter() - t0

        score_accum.append(scores)
        verdicts.append(extract_verdict(transcript.get("judge", {"text": ""})["text"]))
        total = _transcript_total_tokens(transcript)
        tokens_accum.append(total)
        in_total = sum(t["tokens_in"] for t in transcript.values())
        out_total = sum(t["tokens_out"] for t in transcript.values())
        cost_accum.append(_estimate_cost(in_total, out_total))
        wall_accum.append(wall)

    # Average scores across reruns; verdict = mode-equivalent (first one).
    avg = RubricScores(
        agent_voice=round(statistics.mean(s.agent_voice for s in score_accum), 2),
        source_grounding=round(statistics.mean(s.source_grounding for s in score_accum), 2),
        verdict_justification=round(
            statistics.mean(s.verdict_justification for s in score_accum), 2
        ),
        cost_predictability=round(statistics.mean(s.cost_predictability for s in score_accum), 2),
    )
    return IdeaResult(
        id=idea["id"],
        expected_verdict=idea["expected_verdict"],
        actual_verdict=verdicts[0],
        scores=avg.to_dict(),
        wall_seconds=round(statistics.mean(wall_accum), 3),
        tokens_total=int(statistics.mean(tokens_accum)),
        cost_usd=round(statistics.mean(cost_accum), 4),
    )


async def run_eval(
    *,
    live: bool,
    reruns: int,
    filter_id: str | None,
) -> dict[str, Any]:
    ideas = _load_ideas(filter_id)

    # Compute cohort median tokens FIRST so cost_predictability has a stable
    # reference. In mock mode we know the transcripts up-front; in live mode
    # we do a two-pass run (collect transcripts, then score) to get a real
    # cohort median.
    if not live:
        cohort_totals = [
            _transcript_total_tokens(MOCK_TRANSCRIPTS.get(i["id"], get_mock_transcript(i["id"])))
            for i in ideas
        ]
        cohort_median = statistics.median(cohort_totals) if cohort_totals else 0.0
    else:
        # For live, we don't have an a priori cohort — fall through to
        # per-idea scoring with cohort_median=0 (rubric mock path uses it,
        # live path ignores it).
        cohort_median = 0.0

    results: list[IdeaResult] = []
    for idea in ideas:
        r = await _evaluate_one(idea, live=live, reruns=reruns, cohort_median_tokens=cohort_median)
        results.append(r)
        print(
            f"  {r.id:<40} expected={r.expected_verdict:<5} actual={r.actual_verdict:<7} "
            f"median_score={statistics.median(r.scores.values()):.2f} "
            f"tokens={r.tokens_total} cost=${r.cost_usd:.4f}"
        )

    correct = sum(
        1
        for r in results
        if _normalize_verdict(r.actual_verdict) == _normalize_verdict(r.expected_verdict)
    )
    kills = sum(1 for r in results if _normalize_verdict(r.actual_verdict) == "kill")
    median_scores = [statistics.median(r.scores.values()) for r in results]

    aggregate = {
        "kill_rate": round(kills / len(results), 3) if results else 0.0,
        "verdict_accuracy": round(correct / len(results), 3) if results else 0.0,
        "median_score": round(statistics.median(median_scores), 3) if median_scores else 0.0,
        "median_cost_usd": round(statistics.median(r.cost_usd for r in results), 4)
        if results
        else 0.0,
    }
    payload: dict[str, Any] = {
        "date": time.strftime("%Y-%m-%d"),
        "git_sha": _git_sha(),
        "mode": "live" if live else "mock",
        "reruns": reruns,
        "ideas": [asdict(r) for r in results],
        "aggregate": aggregate,
    }
    return payload


def _save_baseline(payload: dict[str, Any]) -> Path:
    BASELINES_DIR.mkdir(parents=True, exist_ok=True)
    suffix = "" if payload["mode"] == "live" else "-mock"
    out_path = BASELINES_DIR / f"{payload['date']}{suffix}.json"
    # If the file already exists this run, append a counter so we don't
    # silently overwrite a prior run from the same day.
    if out_path.exists():
        i = 2
        while True:
            candidate = BASELINES_DIR / f"{payload['date']}{suffix}-{i}.json"
            if not candidate.exists():
                out_path = candidate
                break
            i += 1
    out_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return out_path


def _diff_against_baseline(current: dict[str, Any], baseline_path: Path) -> int:
    """Return non-zero exit code if any axis regressed >15% vs baseline."""
    base = json.loads(baseline_path.read_text(encoding="utf-8"))
    base_agg = base["aggregate"]
    cur_agg = current["aggregate"]

    THRESHOLD = 0.15
    regressions: list[str] = []

    def _check(name: str, *, lower_is_worse: bool) -> None:
        b = base_agg.get(name, 0.0)
        c = cur_agg.get(name, 0.0)
        if b == 0:
            return
        delta = (c - b) / b
        worse = (delta < -THRESHOLD) if lower_is_worse else (delta > THRESHOLD)
        marker = "WORSE" if worse else "ok"
        sign = "+" if delta >= 0 else ""
        print(
            f"  {name:<20} baseline={b:.3f}  current={c:.3f}  delta={sign}{delta * 100:.1f}%  [{marker}]"
        )
        if worse:
            regressions.append(name)

    print(f"\nDiff vs {baseline_path.name}:")
    _check("verdict_accuracy", lower_is_worse=True)
    _check("median_score", lower_is_worse=True)
    _check("median_cost_usd", lower_is_worse=False)  # cost going UP is bad

    if regressions:
        print(f"\nFAIL: regression on {regressions} exceeds {int(THRESHOLD * 100)}% threshold")
        return 1
    print("\nOK: no regressions exceed threshold")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m tests.eval.runner",
        description="Pro tier eval harness (mock by default).",
    )
    p.add_argument("--live", action="store_true", help="real API calls (~$3-5/run)")
    p.add_argument("--reruns", type=int, default=1, help="passes per idea")
    p.add_argument("--idea", type=str, default=None, help="filter to a single idea id")
    p.add_argument(
        "--baseline",
        type=str,
        default=None,
        help="path to baseline JSON; non-zero exit on >15%% regression",
    )
    p.add_argument(
        "--no-save",
        action="store_true",
        help="skip writing a new baseline JSON (useful for ad-hoc inspection)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    print(
        f"Pro eval harness | mode={'LIVE' if args.live else 'mock'} | reruns={args.reruns}"
        f"{' | filter=' + args.idea if args.idea else ''}"
    )

    payload = asyncio.run(run_eval(live=args.live, reruns=args.reruns, filter_id=args.idea))

    print(f"\nAggregate: {json.dumps(payload['aggregate'], indent=2)}")

    if not args.no_save:
        out = _save_baseline(payload)
        print(f"Saved baseline -> {out}")

    if args.baseline:
        return _diff_against_baseline(payload, Path(args.baseline))
    return 0


if __name__ == "__main__":
    sys.exit(main())
