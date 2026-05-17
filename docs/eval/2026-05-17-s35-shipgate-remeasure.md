# S35 Ship-Gate Re-measure — N=30 Paid Rubric Run

**Ticket:** s35-#94 | **Branch:** `s35/citation-relevance` | **Date:** 2026-05-17
**Run:** the one founder-budgeted N=30 paid rubric run for S35.
**Artifacts:** `tests/eval/live_runs/2026-05-17-s24-defi-rubric-s35-shipgate-r{1,2,3}-10.json`
**Pool:** 3 independent N=10 runs of `defi_trade_rubric_suite.json` (the SHIP_GATE_N=30 pooling mechanism, S34-WS1-A path 2).

## Headline

`ship_gate_pass = false`. **2 of 6** dimensions statistically locked (CI lower bound ≥ threshold).
This is a **regression** from #89 (3/6 locked) — `hallucination_score` and `provider_kind_coverage`
hold; `dissent_grounding` slipped out of lock. **`citation_relevance` did NOT clear 0.50 — it
moved the wrong way (0.468 → 0.398).**

## Retrieval pre-gate (free, deterministic, production top_k=15)

PASSED. `canon_floor_rate = 1.00` (target 0.80), `provider_kind_coverage = 0.967` (target 0.80).
Retrieval is healthy — the rubric run was cleared to spend. Artifact:
`2026-05-17-s33-retrieval-eval-s35-shipgate-pregate-10.json`.

## Before / after — 6 dimensions, #89 (S34 N=30) vs S35 (N=30)

Bootstrap 95% CI, 10k resamples, seed=4242. A dimension is **locked** when CI-low ≥ threshold.

| Dimension | Thr | #89 mean | #89 CI | #89 lock | S35 mean | S35 CI | S35 lock | Δ mean |
|---|---|---|---|---|---|---|---|---|
| verdict_accuracy       | 0.85 | 0.933 | [0.833, 1.000] | NO  | 0.867 | [0.733, 0.967] | NO  | −0.066 |
| citation_relevance     | 0.50 | 0.468 | [0.422, 0.512] | NO  | 0.398 | [0.353, 0.442] | NO  | **−0.070** |
| provider_kind_coverage | 0.70 | 1.000 | [1.000, 1.000] | YES | 1.000 | [1.000, 1.000] | YES | 0.000 |
| hallucination_score    | 0.30 | 0.467 | [0.300, 0.633] | YES | 0.567 | [0.400, 0.733] | YES | +0.100 |
| dissent_grounding      | 0.50 | 0.603 | [0.557, 0.653] | YES | 0.375 | [0.267, 0.482] | **NO**  | **−0.228** |
| confidence_calibration | 0.55 | 0.588 | [0.547, 0.628] | NO  | 0.513 | [0.465, 0.562] | NO  | −0.075 |

**Locked:** #89 = 3/6 (`provider_kind_coverage`, `hallucination_score`, `dissent_grounding`).
S35 = **2/6** (`provider_kind_coverage`, `hallucination_score`).

## Question 1 — did `citation_relevance` clear 0.50?

**No.** Mean **0.398**, 95% CI **[0.353, 0.442]**. The CI *upper* bound (0.442) is below the
0.50 threshold — at N=30 we can say with 95% confidence the true value is under the bar. It does
not lock; it does not even point-pass. WS2 projected 0.65–0.85; the measured result is 0.398, a
**0.07 regression vs the #89 baseline of 0.468.** WS2's projection was wrong.

**Root cause — the coverage floor is an un-trimmable canon tax.** WS2's `select_emitted_citations`
trim *does* work on the protocol side: S34 emitted a flat 15 citations on every fixture; S35 emits
6–12 (mean 10.4). But every one of the 30 rows still carries **~5 canon citations** — the coverage
floor re-adds one chunk for each canon `provider_kind` present in retrieval (canon_macro,
canon_valuation, canon_psychology, …) whether or not any panel turn cited it.

Measured: mean **canon fraction = 0.503** — half of every emitted citation set is canon
boilerplate. `corr(canon_fraction, citation_relevance) = −0.41`. The judge's notes name it
directly: "citations 8–12 are canon boilerplate (Marks, Damodaran, Mauboussin, Fed paper,
Berkshire letter) with only tangential relevance." The trim removed *unused protocol* chunks but
the canon floor is mandatory and un-trimmable, so the canon *share* of the (smaller) emitted set
went **up**, and `citation_relevance` went **down**.

## Question 2 — did `provider_kind_coverage` hold?

**Yes — perfectly. Mean 1.000, CI [1.000, 1.000], locked.** The coverage floor did its job: it
guarantees ≥1 citation per required `provider_kind`, so the deterministic coverage check is 1.0 on
every fixture.

**The structural tension WS2 flagged is now measured and confirmed.** `provider_kind_coverage` and
`citation_relevance` are not independent — they are *anti-correlated by construction*. The same
~5 canon chunks the floor re-adds to hold coverage at 1.00 are the exact chunks the judge marks
tangential. r=−0.41 across 30 rows. You cannot improve `citation_relevance` without either
(a) weakening the coverage floor (which drops `provider_kind_coverage` below its lock), or
(b) changing what the judge is shown / what the floor re-adds. The two dimensions are pulling
against each other through one shared mechanism.

## The other 4 dimensions vs #89

- **verdict_accuracy** 0.933 → 0.867 (−0.066). Still not locked; CI-low 0.733 well under 0.85.
  Within run-to-run panel noise (sd=0.34, binary dimension); not a real move.
- **hallucination_score** 0.467 → 0.567 (+0.100). Still locked. Note the sd is ~0.50 (near-binary,
  bimodal) — the lock is real but the CI is wide [0.400, 0.733]; do not over-read the +0.10.
- **dissent_grounding** 0.603 → 0.375 (−0.228). **Lost its lock — this is the headline regression
  besides citation_relevance.** Cause: run r3 returned `dissent_count = 0` on **all 10 fixtures**
  (r1 sum=10, r2 sum=16, r3 sum=0). Every r3 panel reached unanimity with empty
  `blocker_questions`. r3's dissent_grounding is all-zeros; r1/r2 average ~0.56. This is a
  **panel-side anomaly in r3**, not judge noise — the 7-turn transcript ran normally but the
  coordinator surfaced zero surviving dissent. It looks like an intermittent dissent-collapse bug
  (gpt-4o-mini rounding toward consensus — cf. MEMORY `feedback_prompt_iteration_plateau`), not a
  WS2 side-effect. Worth a #95 diagnostic: with r3 excluded, dissent_grounding pools to ~0.56 and
  would still lock. The N=30 number is dragged down by one bad run.
- **confidence_calibration** 0.588 → 0.513 (−0.075). Still not locked; CI [0.465, 0.562] straddles
  0.55. Plausibly the same r3 contamination — calibration depends on dissent signal, and r3's
  zero-dissent panels confuse the judge's "confidence vs dissent" axis. r3 alone was 0.460.

## Honest read — did S35-WS2 work?

**Partially, and not enough.** WS2 did exactly what it was scoped to do mechanically: it decoupled
the citation envelope from reasoning context and cut emitted citations 15 → 6–12. `provider_kind_
coverage` held at a perfect lock — the coverage floor protected it as designed.

But WS2 **did not move `citation_relevance` toward its goal — it moved it backwards.** The
projection (0.65–0.85) assumed trimming would raise the *relevant share* of citations. It didn't,
because the canon coverage floor is a fixed ~5-chunk tax that the trim cannot touch, and shrinking
the protocol side only raised canon's *proportion*. The structural tension WS2 itself flagged is
the binding constraint, and it is now quantified: r = −0.41, canon fraction = 0.50.

## What still blocks a clean 6/6

1. **The citation_relevance ↔ provider_kind_coverage tension is unresolved and structural.**
   This is the #1 blocker. A clean fix decouples the two: e.g. emit canon-floor chunks under a
   separate `coverage_context` field the relevance judge does NOT score, OR have the judge score
   relevance only over *panel-used* citations while coverage is checked over the full emitted set.
   Right now one mechanism feeds both dimensions with opposite sign. Route to `ai-ml-engineer` /
   `staff-engineer` — this is an envelope-schema decision, not a tuning knob.
2. **The r3 dissent-collapse anomaly.** dissent_grounding lost its lock purely because one of the
   three pooled runs returned zero dissent on all 10 fixtures. This is intermittent and needs a
   #95 diagnostic before the next ship-gate, or N=30 dissent numbers stay untrustworthy.
3. **verdict_accuracy and confidence_calibration remain unlocked** (as they were at #89) — neither
   is a new regression, but neither is close. confidence_calibration may recover once the r3
   anomaly is fixed.

**Bottom line:** S35-WS2 shipped a correct mechanism that does not solve the problem it targeted.
`citation_relevance` is now *provably* below 0.50 at N=30 (CI upper bound 0.442 < 0.50). 6/6 is
not reachable without (a) restructuring the citation envelope to break the canon-floor /
relevance coupling and (b) fixing the r3 dissent-collapse bug. Do not ship on this run.
