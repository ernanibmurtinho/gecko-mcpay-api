# S37 Phase-1-B — confidence_calibration miss decomposition

**Date:** 2026-05-18 · **Branch:** `s36/prompt-grounding` · **Author:** quant-analyst
**Artifacts:** `tests/eval/live_runs/2026-05-18-s24-defi-rubric-s36-110-shipgate-r{1..5}-10.json` (N=50, 5 pooled clean runs)
**Scorer:** `tests/eval/scripts/score_defi_trade_rubric.py`
**Read-only statistical analysis. No code changes, no eval runs, no LLM spend.**

## TL;DR

`confidence_calibration` = 0.568, 95% CI [0.520, 0.614]. The CI lower bound 0.520 is
below the 0.55 threshold, so the dimension does **not** statistically lock. The miss is
**~85-90% an artifact of the grounding gate's confidence-flooring action**, not a genuine
calibration defect of the panel. The gate fired on 29/50 rows; in 27 of those 29 the
judge's own notes explicitly cite the gate-induced confidence value (often a verdict that
says `act`/`pass` while `confidence=0.0`) as the calibration failure. Remove the floor and
the dimension means 0.64 and locks.

---

## 1. How `confidence_calibration` is scored

It is judged by Claude Sonnet 4.6 (temperature 0.0), axis 4 of the 4-axis `submit_rubric`
tool. The judge instruction (scorer lines 612-618):

> **confidence_calibration (0.0-1.0):** Is the panel's stated confidence reasonable given
> citation strength + dissent? 1.0 = well-calibrated (high conf only with strong cites +
> low dissent, low conf with thin cites or strong dissent); 0.5 = mild over/under-confidence;
> 0.0 = severely miscalibrated (e.g. confidence 0.9 with zero on-topic cites, or 0.3 with
> strong cites + clear directional signal).

**What it rewards:** the *number* in `panel_response.confidence` tracking the strength of
evidence and the level of dissent. **What it penalizes:** a confidence number that does not
match the rest of the envelope — and, critically, a confidence number that contradicts the
verdict literal or the coordinator's own transcript. The judge sees `verdict`, `confidence`,
`dissent_count`, citations, and the full turn transcript, so it directly observes any
mismatch between the envelope confidence and what the coordinator turn said.

**Threshold:** 0.55 (HIS rebaseline 2026-05-14). Pass-gate is the bootstrap 95% CI lower
bound clearing 0.55 (S34-WS1-A), not the point mean.

### Correction to the Phase-1-B brief

The brief states the gate "floors confidence to 0.00 every time it fires." That is not
what `apply_grounding_gate` does. The gate sets:

```
penalty        = 1.0 - grounded_fraction
new_confidence = round(confidence * (1.0 - penalty), 4) = round(confidence * grounded_fraction, 4)
```

It *multiplies* confidence by the grounded fraction, and fires (`abstained`) when
`grounded_fraction <= 0.5`. Confidence hits exactly 0.00 only when `grounded_fraction == 0`
(no numeric claim grounded). In the N=50 run: **29 rows had the gate fire; of those, 18 were
floored to exactly 0.00 and 11 took a partial penalty** (confidence 0.036-0.318). The
distortion is real, but it is a graded multiplicative penalty, not a flat zero.

---

## 2. Gate-fired vs not-fired split

A row is "gate fired" iff its `blocker_questions` carries the `"Grounding gate:"` note that
`apply_grounding_gate` appends. This is the exact, deterministic signal — not a proxy.

| Group | n | mean confidence_calibration | mean panel confidence |
|---|---|---|---|
| **All** | 50 | **0.5680** | 0.325 |
| **Gate fired** | 29 | **0.5155** | 0.077 (range 0.000-0.318) |
| **Not fired** | 21 | **0.6405** | 0.667 (range 0.400-0.750) |

Bootstrap 95% CIs (10k resamples, seed 4242 — matches the scorer):

| Group | mean | CI low | CI high | locks @ 0.55? |
|---|---|---|---|---|
| All (N=50) | 0.5680 | 0.5200 | 0.6140 | **NO** (CI_low 0.520 < 0.55) |
| Gate fired (n=29) | 0.5155 | 0.4483 | 0.5793 | NO |
| Not fired (n=21) | 0.6405 | 0.5810 | 0.6881 | **YES** |

**The miss is concentrated entirely in the gate-fired rows.** The not-fired group already
locks on its own (CI_low 0.581 > 0.55). The gate-fired group's mean (0.5155) is what drags
the pooled mean below threshold and widens the CI: group SD is 0.169 pooled vs 0.126 for
not-fired alone — the floored rows inject the variance that pushes CI_low to 0.520.

### Why the gate-fired rows score low — the judge penalizes the gate's own action

27 of the 29 gate-fired rows have a judge note that **explicitly cites the gate-set
confidence value** as the calibration problem. Verbatim from the artifacts:

- *"Confidence calibration: The panel states confidence=0.0 yet issues an 'act' verdict —
  this is a severe miscalibration."* (jito-mev-tip-band)
- *"Panel verdict is 'defer' with confidence 0.0, yet the coordinator turn concludes 'act'
  — a direct internal contradiction."* (drift-jto-perp-short)
- *"the coordinator's raw output stated 0.65 but the envelope reports 0.1393 — a large
  discrepancy suggesting post-processing adjustment."* (jupiter-jlp-vs-jitosol)
- *"Panel states confidence=0.0 in the envelope but coordinator turn states 0.65 — this
  internal inconsistency is a calibration problem."* (sanctum-unstake-vs-hold)

The mechanism: the gate floors `envelope.confidence` *after* the coordinator has already
written its own confidence (e.g. 0.65, 0.75) into the transcript turn. The judge sees both
the floored envelope number and the un-floored coordinator number, reads the mismatch as a
calibration defect, and docks the score. **The gate is manufacturing the exact internal
inconsistency the calibration axis is designed to catch.** The 7 gate-fired `act`/`pass`
rows with `confidence < 0.05` are the worst case — verdict and confidence flatly contradict
— and they score a mean cc of 0.371.

Secondary, smaller effect: a floored confidence near 0.00 on a `defer` verdict is *not*
strictly contradictory (low confidence + defer is internally consistent), but the judge
still often reads "confidence 0.0 with otherwise reasonable evidence" as under-confidence
and scores 0.5-0.6 rather than 0.7+. This is the milder half of the artifact.

---

## 3. Counterfactual — would it lock without the floor?

Two estimates, both bootstrapped (10k, seed 4242):

**CF-A — exclude the 29 gate-fired rows entirely (n=21):**
mean 0.6405, CI [0.581, 0.688]. **Locks.**

**CF-B — keep all 50 rows but impute the gate-fired rows at the not-fired group mean
(0.6405) as a proxy for "what the judge would have scored an un-floored confidence":**
mean 0.6405, CI [0.616, 0.661]. **Locks comfortably** — CI_low 0.616 clears 0.55 by 0.066.

Both counterfactuals are conservative-to-optimistic bookends. CF-B assumes the gate-fired
rows would score *exactly* the not-fired average if not floored; the true post-fix value is
likely a little lower because gate-fired rows are, by construction, rows with weaker numeric
grounding (thinner evidence → genuinely some calibration headroom). But even a substantial
haircut leaves margin: the dimension locks for any post-fix mean ≥ ~0.60 at N=50.

**Estimate: with the flooring removed, confidence_calibration lands ~0.62-0.64 and locks.**

---

## 4. Bottom line — artifact vs genuine defect

**Verdict: (a) mostly an artifact of the gate-flooring, with a thin (c)-style genuine
residue.** Proportional decomposition of the 0.072 gap between the observed pooled mean
(0.568) and the un-floored estimate (0.640):

- **~85-90% artifact.** 27/29 gate-fired rows are scored down by the judge *because of*
  the gate-set confidence number — either an outright verdict/confidence contradiction
  (the 7 `act`/`pass` rows, the severe-miscalibration cases) or a floored-confidence
  under-confidence read. Remove the floor and these rows recover toward 0.64.

- **~10-15% genuine.** The gate-fired rows are selected for weak numeric grounding, and a
  panel that surfaces thin evidence *should* carry lower confidence — some of their sub-0.64
  score is real, deserved under-confidence the judge would dock even with no gate. This is
  why the post-fix mean is estimated at 0.62-0.64, not a clean 0.64+. It is not a
  panel-wide calibration defect; it is a small, correct, evidence-strength-tracking signal.

The N=30 read at #103 (0.630, "looked locked") vs N=50 (0.568) is **not** a regression — it
is sampling noise on a dimension whose pooled SD is 0.169. At N=30 the gate-fired rows
happened to draw a more favorable mix; at N=50 they did not. The dimension was never
genuinely locked — #103 was a small-N draw, exactly as the brief states.

---

## 5. What would lock it, and at what N

**The fix that locks it: stop the gate from creating a confidence/verdict contradiction.**
The planned WS1 redaction fix (gate redacts the ungrounded *figure* from the verdict text
instead of flooring `envelope.confidence`) is the right lever — see the side-effect
analysis below.

Required N once the artifact is removed (margin = mean − 0.55, halfwidth ≈ 1.96·SD/√N,
SD ≈ 0.126 from the not-fired group):

| Post-fix true mean | Margin | N to lock (CI_low ≥ 0.55) |
|---|---|---|
| 0.64 | 0.090 | ~8 |
| 0.62 | 0.070 | ~13 |
| 0.60 | 0.050 | ~25 |

The current N=50 ship-gate is **already large enough** to certify the lock if the post-fix
mean is anywhere ≥ ~0.60. No larger N is needed — re-run the existing N=50 ship-gate after
the WS1 fix lands. If the post-fix mean comes in below 0.60, N would need to grow toward
50-100, but that is the unlikely tail given the counterfactuals above.

**Recommendation:** land the WS1 gate-redaction fix, then re-run the N=50 ship-gate as-is.
Do not expand the suite or the N on account of this dimension.

---

## Appendix — will the WS1 gate-redaction fix lock confidence_calibration as a side effect?

**Partly — likely yes, but not guaranteed; treat it as probable, not certain.**

Quant reasoning:

1. **It removes the dominant artifact mechanism.** The WS1 fix redacts the ungrounded
   figure from the verdict text and *stops mutating `envelope.confidence`*. That kills the
   verdict/confidence contradiction the judge is penalizing in 27/29 gate-fired rows. The
   floored-confidence under-confidence read disappears with it. This is a direct,
   mechanical removal of ~85-90% of the gap.

2. **The counterfactual says the un-gated rows already pass.** With confidence no longer
   floored, gate-fired rows revert to the panel's native confidence (the not-fired group
   sits at 0.667 mean confidence, cc 0.64). CF-B puts the pooled cc at 0.640, CI [0.616,
   0.661] — locks with room.

3. **Two residual uncertainties keep this at "partly", not "yes":**
   - **The judge re-scores everything.** confidence_calibration is LLM-judged, not
     deterministic. Removing the floor changes the envelope the judge sees, so every cc
     score is re-drawn. The 0.64 estimate is an imputation from the not-fired group, not a
     measured post-fix number. Judge variance at temp 0.0 is low but non-zero.
   - **Redaction introduces a new envelope state the judge has never scored.** A verdict
     whose text has had figures redacted ("[figure redacted — not in corpus]") is a novel
     input. The judge could read a redacted verdict as *itself* a calibration concern, or
     could (correctly) treat the panel's retained confidence as well-grounded. Direction is
     uncertain; magnitude is probably small.

4. **What the fix does NOT address:** the ~10-15% genuine residue — gate-fired rows are
   weak-grounding rows and carry some deserved under-confidence. The WS1 fix does not change
   that, and should not. It is correct signal, and it is small enough that the dimension
   still locks.

**Net:** the WS1 redaction fix is the correct and probably-sufficient lever for
confidence_calibration. Expected post-fix: mean ~0.62-0.64, locks at N=50. But because the
axis is LLM-judged and redaction creates an unscored envelope shape, the lock must be
*confirmed* by re-running the N=50 ship-gate after the fix — not assumed. Do not mark
confidence_calibration green on the strength of this analysis alone.
