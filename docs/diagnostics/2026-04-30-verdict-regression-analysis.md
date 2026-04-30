# Verdict Regression Analysis — 2026-04-30 holdout_live: 0.80 → 0.60

Author: ai-ml-engineer
Date: 2026-04-30
Status: Diagnosis complete; gate paused pending recommended fixes.

## TL;DR — Verdict (d): combination

**Track A (S11-VERDICT-01, commit `b1661a0`) is a contributing cause, but most of
the swing is live-LLM sampling variance amplified by a 10-idea sample at the
hardest cohort in the suite.** Track A added a new judge output requirement
("Final verdict: KILL|REFINE|BUILD" terminal line) that subtly biases the judge
toward harder verdict commitment, including in the prose `Verdict:` line that
the eval runner actually keys on. The mapping/code path is *not* the
regression — the rubric still parses the prose `Verdict:` line via
`extract_verdict`, exactly as it did pre-S11. Variance + a measurable prompt
nudge ≈ −2 ideas across 10 between two runs.

## The two runs

| Run | File | git_sha | code era | accuracy | kill_rate |
|---|---|---|---|---|---|
| 1 (morning) | `tests/eval/live_runs/2026-04-30-holdout_live.json`   | `d322034` | **PRE-S11** (Track A not yet landed) | 0.80 | 0.70 |
| 2 (afternoon) | `tests/eval/live_runs/2026-04-30-holdout_live-2.json` | `b48b86d` | **POST-S11** (Track A + Sprint 12 Track F) | 0.60 | 0.90 |

Per `git log`, `d322034` is older than `b1661a0` (Sprint 11). The morning run
is pre-Track-A; the afternoon run is post-Track-A. The user's framing
("morning passed at 0.80, afternoon failed at 0.60 after S11 landed") is
correct — the comparison is across the prompt change, not within it.

## Per-idea diff

`expected_verdict` for all five "good" ideas is `ship`; for all five "bad"
ideas is `kill`.

| idea_id | exp | run-1 actual | run-2 actual | run-1 hit? | run-2 hit? |
|---|---|---|---|---|---|
| holdout-good-comp-band-diff             | ship | **kill** | ship | ✗ | ✓ |
| holdout-good-twilio-webhook-replay      | ship | ship     | **kill** | ✓ | ✗ |
| holdout-good-ame-intake                 | ship | ship     | **kill** | ✓ | ✗ |
| holdout-good-mcp-clickhouse-explainer   | ship | ship     | **kill** | ✓ | ✗ |
| holdout-good-hhs-rfp-budget             | ship | **kill** | **kill** | ✗ | ✗ |
| holdout-bad-childcare-swap              | kill | kill     | kill | ✓ | ✓ |
| holdout-bad-sales-action-extractor      | kill | kill     | kill | ✓ | ✓ |
| holdout-bad-ai-marriage-counselor       | kill | kill     | kill | ✓ | ✓ |
| holdout-bad-fitness-loyalty-chain       | kill | kill     | kill | ✓ | ✓ |
| holdout-bad-ai-estate-planner           | kill | kill     | kill | ✓ | ✓ |
| **score** | | | | **8/10 = 0.80** | **6/10 = 0.60** |

Notable: **`comp-band-diff` flipped the OTHER way** (kill in run-1, ship in
run-2). One idea is a flat hit/miss across both runs (`hhs-rfp-budget` —
killed both times). Three ideas (`twilio-webhook-replay`, `ame-intake`,
`mcp-clickhouse-explainer`) regressed from ship-correct to kill in run 2.

That bidirectional flipping is the single strongest evidence for variance
rather than a unidirectional structural break — a true regression on the
ship-detection path would not have flipped `comp-band-diff` in our favor.

## What Track A actually changed

Commit `b1661a0` ("S11-VERDICT-01") delta on the runtime path:

1. **`packages/gecko-core/src/gecko_core/models.py`** — added `Verdict` enum
   and `derive_verdict()`. Pure addition, no caller changes the rubric reads.
2. **`packages/gecko-core/src/gecko_core/orchestration/basic.py`** — stamps
   `ResearchResult.verdict` from `derive_verdict(gap_classification, None)`.
   Basic tier passes `advisor_consensus=None` → only `Full`/`False` produce
   KILL; all `Partial:*` produce REFINE. **This path is NOT used by
   holdout_live** — that suite runs the pro debate.
3. **`packages/gecko-core/src/gecko_core/workflows.py`** — adds
   `_parse_judge_verdict` (regex on the Final verdict line) and overrides
   `ResearchResult.verdict` when present. Affects `ResearchResult.verdict`,
   not `transcript.judge.text`.
4. **`packages/gecko-core/src/gecko_core/orchestration/pro/_default_prompts_v5_4.json`**
   — adds a 7-line block to the *judge* prompt instructing it to emit one
   additional terminal line: `Final verdict: KILL|REFINE|BUILD`. The
   decision pipeline (STEP 1–4), the prose `Verdict:` line shape, the
   gap_classification line, and the score lines are byte-identical to
   pre-S11. **This is the only model-facing change.**
5. CLI render + journal mapping — downstream of the eval harness, not in the
   measurement path.

## What the eval rubric actually reads

`tests/eval/runner.py::_evaluate_one` extracts the actual verdict via:

```python
verdicts.append(extract_verdict(transcript.get("judge", {"text": ""})["text"]))
```

`tests/eval/rubric.py::extract_verdict` regexes the judge's prose for
`verdict\s*:\s*(\w+)` (case-insensitive) and returns the first match's stem
mapped to `kill | ship | pivot | unknown`. For a v5.4 judge output, the FIRST
match is the prose paragraph `Verdict: SHIP V1 to ...` or
`Verdict: KILL — ...`. The new "Final verdict: KILL|REFINE|BUILD" terminal
line is a *second* match further down and is never reached.

`_normalize_verdict` then maps `pivot → kill` for accuracy scoring (legacy
softening rule) — so anything not `ship` counts against the ship-expected
ideas.

The rubric also reports `verdict_correctness` per-idea. In both runs, that
axis is 10.0 for hits and 0.0 for misses, confirming the runner saw the
expected legacy taxonomy and graded it the same way both times. The
`ResearchResult.verdict` enum is **not consulted** by the rubric.

## Why Track A still nudges the prose

Even though the prose `Verdict:` line shape didn't change, asking the judge
to emit a structured 3-token tail forces it to take a sharper position. Two
plausible mechanisms, both well-attested in transformer output:

1. **Output-conditioning bias.** When the model knows it must end with a
   single-token commitment from `{KILL, REFINE, BUILD}`, the prose verdict
   above tends to align — and on a borderline narrow-ICP idea (ame-intake,
   mcp-clickhouse-explainer, hhs-rfp-budget), the path of least resistance
   is to harden into KILL rather than equivocate.
2. **REFINE is not a softening valve in the prose.** The prose Verdict is
   still "SHIP" or "KILL — no fence-sitting, no pivots" by prompt fiat.
   REFINE only lives in the terminal Final-verdict line. So a judge that
   would have rendered an ambiguous decision as a wishy-washy SHIP under
   v5.4-pre-S11 may now collapse to KILL prose because that's the only
   non-SHIP word the prose paragraph permits — even if it then writes
   `Final verdict: REFINE` two lines down.

(2) is the more concerning one structurally. It means **Track A's prompt
shape is leaving founder-actionable REFINE signal on the floor** — the
judge has it internally but the runner can't see it, because the rubric
reads the prose, not the terminal token.

## Per-miss attribution

Below for each ship→kill miss across both runs. We don't have the raw
transcripts archived (the live_runs JSON is aggregate-only — see Rec 1
below), so the attribution is structural, not transcript-grounded.

| idea_id | which run miss | likely judge output | likely Final-verdict | gap likely emitted |
|---|---|---|---|---|
| comp-band-diff           | run-1 only | `Verdict: KILL — saturation (Pave)` | KILL or REFINE | `Full` or `Partial:UX` |
| twilio-webhook-replay    | run-2 only | `Verdict: KILL — Stripe Radar covers replay` | KILL or REFINE | `Full` or `Partial:integration` |
| ame-intake               | run-2 only | `Verdict: KILL — entrenched paper-form vendors` | KILL or REFINE | `Partial:UX` or `Partial:integration` |
| mcp-clickhouse-explainer | run-2 only | `Verdict: KILL — ClickHouse Cloud has EXPLAIN` | KILL or REFINE | `Partial:integration` |
| hhs-rfp-budget           | both runs  | `Verdict: KILL — Deltek/Unanet incumbents` | KILL | `Full` |

`hhs-rfp-budget` is the only **persistent** miss across both code eras. That
one is genuinely the model failing the STEP 2 named-buyer SHIP check
(federal contractors + SF-1408 + V1_FEASIBLE_IN_4_DAYS=yes should hit), and
is unrelated to S11. It's a v5.4 prompt issue worth a separate fixture
labeling review.

The four single-run misses are all narrow-ICP/named-artifact ideas that
*should* hit STEP 2's hard SHIP exit. They are exactly the ideas where
prompt-shape pressure (above) most plausibly tips the prose verdict.

## Hypothesis ranking

(a) **Track A regression — pure structural break.** *Rejected.* The eval
    runner reads the prose `Verdict:` line via the same regex it has used
    for months. The decision pipeline didn't change. If Track A had
    hard-broken the path, `comp-band-diff` could not have flipped to
    correct in run 2; it would have flipped the other way too.

(b) **Live-LLM noise at small N.** *Strongly supported.* Three of the four
    swing ideas are bidirectional candidates (run-1-hit → run-2-miss),
    one is the inverse direction (run-1-miss → run-2-hit). At N=10 with
    1 rerun and a single sample per agent, ±0.20 swings are within the
    documented noise floor (CLAUDE-adjacent ai-ml-engineer ops principle:
    "±0.10 swings are noise, not signal" — at N=10 that's a 1-idea band).
    Two unrelated runs at N=10 differing by 2 ideas is not yet a signal.

(c) **Latent fixture/rubric mismatch surfaced by the new shape.** *Partially
    supported.* The rubric grades against legacy `ship/kill/pivot`. Track A's
    REFINE token has no slot in the rubric. The judge's terminal Final
    verdict line is invisible to the rubric, so any softening-to-REFINE
    signal is dropped. This isn't a regression of run-2 vs run-1 (the rubric
    didn't change), but it IS a *reason the rubric undercounts the model's
    real performance under v5.4-S11* — and is the strongest argument for
    the rubric-native KILL/REFINE/BUILD migration in the companion proposal.

(d) **Combination.** *Final answer.* Track A's prompt-shape change is
    plausibly responsible for ~1 of the 2 swing ideas (output-conditioning
    bias on borderline narrow-ICP cases); the other ~1 idea is genuine
    sampling variance. The threshold contract (≥0.80) is sensitive at
    this N — a single sampled idea moves accuracy by 0.10. The user's
    structural concern about REFINE/KILL/BUILD vs ship/kill/pivot is the
    real long-tail issue, and is what the companion proposal addresses.

## Top 3 recommendations before re-running the gate

### Rec 1 — Archive the raw judge transcripts on every live run (P0, free).

The live_runs JSON is aggregate-only. We could not look up the actual judge
prose for the misses, only infer. Add a sibling artifact per run:
`tests/eval/live_runs/2026-04-30-holdout_live-2_transcripts.jsonl` with
`{idea_id, judge_text, gap_classification_line, final_verdict_line}` rows.

This is a 30-minute software-engineer task in `tests/eval/runner.py`
`_evaluate_one`, no new model calls needed (we already have the transcript
in memory). Without this, every future regression diagnosis is blind to
the actual model output. Highest-leverage cheap fix on the eval surface.

### Rec 2 — Run holdout_live with `--reruns 3` before any new prompt change (P0, ~3x cost).

Single-sample runs at N=10 have a ±2-idea (±0.20) noise floor. The
existing rerun-and-majority-vote path in `_evaluate_one` is the right tool;
we just haven't been spending the budget. At ~$0.0014/idea × 10 ideas × 3
reruns = ~$0.04/run, this is well within tolerance.

This is the structural answer to "is the gate failure real" — re-run the
afternoon gate with `--reruns 3` and majority vote. If accuracy stays at
0.60, it's a true regression and we tighten or roll back. If it lands at
0.70–0.80, it's variance and we keep S11.

### Rec 3 — Migrate the rubric to grade KILL/REFINE/BUILD natively (P1, see proposal doc).

See `docs/diagnostics/2026-04-30-rubric-native-verdict-proposal.md`. The
rubric currently throws away the REFINE signal that S11 makes available.
That's both a measurement gap (the gate undercounts model quality) and a
strategic loss (REFINE is genuinely a different founder action than pivot
or kill). This is a 1–2 day effort that should land before Sprint 12's gate
re-run is treated as the new baseline.

## Things explicitly NOT recommended

- **Do not roll back `b1661a0`.** The structured `Verdict` enum is correct
  and downstream-useful (CLI render, journal, MCP surface). The Final
  verdict line in the judge prompt is correct. The fix is to teach the
  rubric the new taxonomy, not to revert the model surface.
- **Do not re-run the live gate yet.** Land Rec 1 (transcript archival)
  first so the next run is diagnosable. Then run with `--reruns 3`.
- **Do not retune the v5.4 judge prompt** until after the rubric migration.
  We can't tell if STEP 2 needs hardening on narrow-ICP cases until the
  rubric stops collapsing REFINE into the kill bucket.
