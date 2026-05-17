"""S34-#89 — ship-gate N=30 result + N=15-vs-N=30 minimum-N power analysis.

Pools the `rows` arrays of the 3 (+1 top-up) s34-shipgate rubric artifacts
into N=30, recomputes the S34-WS1 statistical gate (bootstrap 95% CI; a
dimension locks only when its CI lower bound clears the threshold), then
subsamples n=15 without replacement (2000 draws) to measure the stability
of an N=15 ship-gate call vs N=30. See
docs/eval/2026-05-17-s34-shipgate-N15-vs-N30.md for the writeup.

Bootstrap config (resamples=10000, seed=4242, 95% CI) mirrors
score_defi_trade_rubric._bootstrap_ci so the pooled gate is identical to
what the scorer would emit on a single N=30 run.

S35-#98 — the pooling path is hardened against contaminated artifacts.
Pooling went through `load_poolable_rows`, which rejects (strict mode,
the default) any artifact whose top-level `contaminated` flag is true. A
contaminated artifact's surviving rows are NOT a trustworthy sample, so
they must never silently concatenate into a clean pooled N.

Usage:
    uv run python tests/eval/scripts/shipgate_n15_vs_n30.py
"""

import json
import random
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from tests.eval.scripts.score_defi_trade_rubric import (  # noqa: E402
    ContaminatedArtifactError,
    load_poolable_rows,
)

LR = Path("tests/eval/live_runs")
artifacts = [
    "2026-05-17-s24-defi-rubric-s34-shipgate-r1-10.json",
    "2026-05-17-s24-defi-rubric-s34-shipgate-r2-10.json",
    "2026-05-17-s24-defi-rubric-s34-shipgate-r3-8.json",
    "2026-05-17-s24-defi-rubric-s34-shipgate-r3-topup-2.json",
]
# S35-#98 — reject any contaminated artifact before pooling. `load_poolable_rows`
# concatenates `rows` across artifacts and raises ContaminatedArtifactError
# (strict mode) on a `contaminated: true` artifact.
try:
    pooled, _excluded = load_poolable_rows([LR / a for a in artifacts], strict=True)
except ContaminatedArtifactError as exc:
    raise SystemExit(f"ABORT — pooling rejected a contaminated artifact:\n  {exc}") from exc
rows = [{"src": r["src"], "id": r["id"], **r["scores"]} for r in pooled]
print(f"pooled rows: {len(rows)}")

DIMS = [
    "verdict_accuracy",
    "citation_relevance",
    "provider_kind_coverage",
    "hallucination_score",
    "dissent_grounding",
    "confidence_calibration",
]
THR = {
    "verdict_accuracy": 0.85,
    "citation_relevance": 0.50,
    "provider_kind_coverage": 0.70,
    "hallucination_score": 0.30,
    "dissent_grounding": 0.50,
    "confidence_calibration": 0.55,
}
RESAMPLES = 10000
SEED = 4242
ALPHA = 0.05


def boot_ci(vals, seed=SEED, resamples=RESAMPLES):
    n = len(vals)
    if n == 0:
        return 0, 0, 0
    mean = statistics.mean(vals)
    if n == 1:
        return mean, mean, mean
    rng = random.Random(seed)
    ms = []
    for _ in range(resamples):
        s = [vals[rng.randrange(n)] for _ in range(n)]
        ms.append(sum(s) / n)
    ms.sort()
    lo = ms[int((ALPHA / 2) * resamples)]
    hi = ms[int((1 - ALPHA / 2) * resamples) - 1]
    return mean, lo, hi


print("\n=== STEP 3 — N=30 SHIP-GATE ===")
n30 = {}
for d in DIMS:
    vals = [r[d] for r in rows]
    m, lo, hi = boot_ci(vals)
    hw = (hi - lo) / 2
    locked = lo >= THR[d]
    n30[d] = {
        "mean": m,
        "lo": lo,
        "hi": hi,
        "hw": hw,
        "thr": THR[d],
        "locked": locked,
        "sd": statistics.pstdev(vals),
    }
    print(
        f"  {d:<24} mean={m:.3f} CI=[{lo:.3f},{hi:.3f}] hw={hw:.3f} thr={THR[d]:.2f} sd={n30[d]['sd']:.3f}  {'LOCKED' if locked else 'NOT-LOCKED'}"
    )
green30 = [d for d in DIMS if n30[d]["locked"]]
print(f"\n  => {len(green30)}/6 statistically locked: {green30}")
print(f"  ship_gate_pass = {len(green30) == 6}")

print("\n=== STEP 4 — N=15 SUBSAMPLE POWER ANALYSIS ===")
SUB_DRAWS = 2000
SUB_N = 15
rng = random.Random(99)
# For each subsample draw: draw 15 rows w/o replacement, compute bootstrap CI lower bound per dim, record lock
lock15_count = {d: 0 for d in DIMS}
hw15_acc = {d: [] for d in DIMS}
for draw in range(SUB_DRAWS):
    idx = rng.sample(range(len(rows)), SUB_N)
    sub = [rows[i] for i in idx]
    for d in DIMS:
        vals = [r[d] for r in sub]
        # fixed bootstrap seed per-dim so the CI is a deterministic property of the subsample
        m, lo, hi = boot_ci(vals, seed=SEED, resamples=2000)
        hw15_acc[d].append((hi - lo) / 2)
        if lo >= THR[d]:
            lock15_count[d] += 1

print(f"\n  {SUB_DRAWS} subsamples of n={SUB_N} drawn from the {len(rows)} pooled rows")
print(f"  {'dim':<24} {'P(lock@15)':<12} {'hw@15':<9} {'hw@30':<9} {'tighten':<9} lock@30")
flip_risk = {}
for d in DIMS:
    p_lock = lock15_count[d] / SUB_DRAWS
    hw15 = statistics.mean(hw15_acc[d])
    hw30 = n30[d]["hw"]
    tighten = hw15 / hw30 if hw30 > 0 else float("nan")
    locked30 = n30[d]["locked"]
    # flip prob: P(n15 verdict != n30 verdict)
    if locked30:
        flip = 1 - p_lock  # n30 says lock; flip = n15 fails to lock
    else:
        flip = p_lock  # n30 says not-lock; flip = n15 wrongly locks
    flip_risk[d] = flip
    ts = f"{tighten:.2f}x" if hw30 > 0 else "n/a"
    print(
        f"  {d:<24} {p_lock * 100:>6.1f}%      {hw15:.3f}    {hw30:.3f}    {ts:<9} {'LOCKED' if locked30 else 'not'}"
    )

print(f"\n  {'dim':<24} flip-risk (n15 verdict disagrees with n30)")
for d in DIMS:
    print(f"  {d:<24} {flip_risk[d] * 100:>5.1f}%")

# Save computed results for the doc
out = {
    "n30": {
        d: {k: round(v, 4) if isinstance(v, float) else v for k, v in n30[d].items()} for d in DIMS
    },
    "green30": green30,
    "ship_gate_pass": len(green30) == 6,
    "n15": {
        d: {
            "p_lock": round(lock15_count[d] / SUB_DRAWS, 4),
            "hw15": round(statistics.mean(hw15_acc[d]), 4),
            "hw30": round(n30[d]["hw"], 4),
            "flip_risk": round(flip_risk[d], 4),
        }
        for d in DIMS
    },
}
json.dump(out, open("/tmp/shipgate_results.json", "w"), indent=2)
print("\nresults -> /tmp/shipgate_results.json")
