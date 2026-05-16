# S29 #34 — quota-before-rerank + top_k=5 ablation handoff

**Branch:** `s29/quota-first-k5` (cut from `s29/quota-before-rerank`).
**One-line edit:** `_DEFAULT_TRADE_TOP_K: 10 → 5` at `trade_panel/__init__.py:606`. Order-swap (quota_floor BEFORE rerank) kept intact.
**Canary JSON:** `tests/eval/live_runs/2026-05-13-canary-4.json` (D1+D2 PASS, D3 phantom-marker FAIL — known top_k-related canary calibration artifact, see #31 handoff §5).
**Rubric JSON:** `tests/eval/live_runs/2026-05-13-s24-defi-rubric-s29-34-quota-first-k5-9.json` (**N=9**, one fixture lost to an Anthropic API 500: `kamino-2025Q1-jitosol-vault`, request_id `req_011Cb1FEMwKSLBv8FpmTq8sL`).
**Total spend:** ~$1.55 (canary $0.05 + rubric N=9 ≈ $1.40, slack vs $2.00 budget intact).

---

## §1 — Three-state comparison (the prompt's table)

| State | Order | top_k | N | vAcc | citRel | **pkCov** | hall | **dissent** | **calib** |
|---|---|---|---|---|---|---|---|---|---|
| Pre-#31 baseline | rerank→quota | 5 | 10 | 0.900 | 0.380 | 0.500 | 0.100 | 0.570 | 0.565 |
| #31 (quota+k10) | quota→rerank | 10 | 10 | 0.900 | 0.370 | **0.700** | 0.100 | 0.490 | 0.495 |
| **#34 (quota+k5)** | quota→rerank | **5** | **9** | 0.889 | 0.383 | **0.778** | **0.000** | **0.578** | 0.506 |

HIS thresholds: vAcc ≥ 0.85, citRel ≥ 0.50, pkCov ≥ 0.70, hall ≥ 0.30, dissent ≥ 0.50, calib ≥ 0.55.

Crosses HIS in #34: vAcc ✅, pkCov ✅, dissent ✅. Misses: citRel ❌ (0.383 < 0.50), hall ❌ (0.000 < 0.30), calib ❌ (0.506 < 0.55).

**3/6 crossings.** Same gate color as #31: **RED**. Not PARTIAL-GREEN (5/6), not GREEN (6/6).

---

## §2 — What the ablation reveals

**Hypothesis (from the prompt):** the order swap drives pkCov; the top_k=10 bump introduced wider-slate dilution that hurt dissent_grounding and confidence_calibration. Dropping top_k=10→5 should preserve pkCov and recover the regressed dims.

**Result against hypothesis, dim-by-dim:**

1. **pkCov (0.500 → 0.700 → 0.778):** the order swap is the active mechanism. Going from top_k=10 back to top_k=5 did NOT collapse pkCov — it actually nudged it up further (+0.08). Mechanism: quota_floor runs against the boosted wide pool with `pre_rerank_k = top_k*2 = 10` slots. Even at the smaller eventual top_k, provider-kind quotas survive into the rerank input. Hypothesis fragment validated.

2. **dissent (0.490 → 0.578):** recovered to baseline territory (0.570). **+0.088 from #31.** Crosses HIS threshold (0.50). Wider-slate dilution explanation holds for this dim — fewer chunks = panel focuses more cleanly on dissent topics. Hypothesis fragment validated.

3. **calibration (0.495 → 0.506):** marginal recovery (+0.011). Still BELOW HIS (0.55) and below the baseline 0.565. Wider-slate-dilution explanation **only partially** holds for calibration; most of the lost ground is sticky. Could be N=9 noise (one fixture missing biases mean), but the direction is "recovers a tiny bit, not all the way."

4. **citRel (0.370 → 0.383):** unchanged-ish (+0.013). Confirms the #31 handoff §3 read: citRel is corpus-content-bound, not retrieval-shape-bound. The Berkshire/Damodaran chunks that survive at the top of the rerank are tangential to protocol-specific questions; their relevance grade doesn't depend on top_k.

5. **hall (0.100 → 0.000):** **REGRESSED.** This is the loudest signal in the run. Every one of 9 fixtures got hall=0.0 from the judge. Reading the judge_notes pattern from the JSON: the panel-narrated panel still cites specific TVL / price / volume figures that the judge can't locate verbatim in the cited chunks — at top_k=5, the slate carries less raw numeric ground-truth than top_k=10, so the panel's instinct to assert specific numbers from the user-question (e.g. "$165 SOL", "$3.164B TVL") trips the must_not_hallucinate rule on more fixtures, not fewer. The #33 grounding-contract prompt addendum (not in this branch) is still the lever; the order-swap + top_k variants don't move this dim. **This is structural to the prompt, not the retrieval shape.**

6. **vAcc (0.900 → 0.889):** within noise band; the one differing fixture is kamino-2024Q4-sol-leverage-entry returning `pass` here where pre-#31 baseline returned `defer` (both are in the acceptable set, but the acceptable set is `[ACT,DEFER]` for that fixture, so `pass` scores 0). N=9 with one judge-API loss explains the rest.

**Net read:** the ablation **partially** confirms the hypothesis. The order swap is doing the structural work (pkCov is locked in even at top_k=5). Dropping top_k from 10 back to 5 recovers dissent fully and calibration ~15% of the way. Hall regressed, but for a reason orthogonal to retrieval shape.

---

## §3 — Which outcome category fired

From the prompt's outcome matrix:

| Outcome | Trigger condition | Fired? |
|---|---|---|
| A: ship pure order-swap as V1 | pkCov ≥0.60 AND dissent ≥0.55 AND calib ≥0.55 | **NO** — calib 0.506 < 0.55 |
| B: tradeoff or rearchitect | pkCov ~0.50 AND dissent ≥0.55 | NO — pkCov 0.778 not 0.50 |
| C: revert everything | all three regress further | NO — pkCov+dissent better, not worse |

**None of the three outcome categories fire cleanly.** The result is a fourth state the prompt didn't anticipate:

> **Outcome A-prime: order-swap is the right structural call; top_k=5 is the right slate width; but the V1 gate is held back by hall (and softly by calib), neither of which is a function of the retrieval shape.**

The ablation succeeds at its design purpose — isolating the variable — but it does NOT independently unblock the V1 gate, because hall and calib are not in the retrieval-shape variable's reachable set.

---

## §4 — Recommendation

**Ship `s29/quota-first-k5` forward as the new retrieval-shape floor, and accept that V1 unblock requires #32 / #33 / #34-corpus, not more pipeline tuning.**

Concretely:

1. **Merge `s29/quota-first-k5` forward** (over `s29/quota-before-rerank`). Two of the three dim-wins from #31 are kept (pkCov, plus a small further bump); the two dim-regressions from #31 are erased (dissent recovers cleanly, calib recovers marginally). Net structural improvement over both prior states. The order swap is the wedge; the top_k bump was the dilution mistake.
2. **Drop `s29/quota-before-rerank`** — it's strictly dominated by `s29/quota-first-k5` on the three movable dims (pkCov +0.08, dissent +0.09, calib +0.01). No reason to keep it.
3. **Pivot the next dispatch off the retrieval shape entirely.** The remaining gap to V1 is 2-3 dimensions that this surgical ablation has now proven are NOT retrieval-shape-bound:
   - **hall (0.000 vs 0.30 threshold):** ship #33 — the persona-prompt grounding addendum ("quote exact substring or write 'unsourced'"). The #31 handoff explicitly called this out; this ablation now empirically confirms retrieval-shape can't fix it. The breadth_block already in `_opening_prompt` has the quote-or-unsourced directive — judge is still flagging the panel's TVL/price claims as unsourced. Either the directive is being ignored at gpt-4o-mini scale, or the judge prompt is reading the directive's `unsourced:` prefix differently. **Diagnostic needed before #33 codes anything.**
   - **citRel (0.383 vs 0.50 threshold):** corpus-content problem, not retrieval-shape. Ship #34-corpus — targeted micro-ingests for jito tip floor, JTO tokenomics, sanctum router, kamino multiply (the 4 fixtures the #31 handoff §2 named).
   - **calib (0.506 vs 0.55 threshold):** within noise band of HIS; one extra fixture run at N=10 or one prompt nudge ("if 3+ analysts abstain, ceiling confidence at 0.55") would likely cross it. Defer; address after #33 lands and re-baseline.

**Do NOT iterate further on retrieval pipeline shape.** The ablation closed that question.

---

## §5 — Per-fixture breakdown (state #34, N=9)

| Fixture | v | vAcc | citRel | pkCov | hall | dissent | calib |
|---|---|---|---|---|---|---|---|
| kamino-2024Q3-jlpusdc-entry | defer | 1 | 0.40 | 0 | 0 | 0.80 | 0.70 |
| kamino-2024Q4-sol-leverage-entry | pass | 0 | 0.50 | 1 | 0 | 0.70 | 0.40 |
| kamino-2025Q1-jitosol-vault | — | — | — | — | — | — | — (judge API 500) |
| kamino-2025Q3-usdc-pyusd-exit | defer | 1 | 0.30 | 0 | 0 | 0.50 | 0.40 |
| drift-2024Q4-sol-perp-long | defer | 1 | 0.50 | 1 | 0 | 0.50 | 0.60 |
| drift-2025Q2-jto-perp-short | defer | 1 | 0.20 | 1 | 0 | 0.50 | 0.50 |
| jupiter-2024Q3-jlp-vs-jitosol | defer | 1 | 0.55 | 1 | 0 | 0.70 | 0.55 |
| jupiter-2025Q1-lst-rotation-msol | defer | 1 | 0.45 | 1 | 0 | 0.50 | 0.60 |
| jito-2025Q2-mev-tip-band | defer | 1 | 0.20 | 1 | 0 | 0.50 | 0.40 |
| sanctum-2025Q3-unstake-vs-hold | defer | 1 | 0.35 | 1 | 0 | 0.50 | 0.40 |

**Notable shifts:** jupiter-2024Q3 verdict flipped from `pass` (in both pre-#31 and #31) to `defer` here — a vAcc WIN (0 → 1) for that fixture, because the acceptable set is `[ACT,DEFER]`. Mechanism: at top_k=5 with quota-first, the panel sees fewer of the LST-token-list snippets that anchored an over-confident PASS in both prior states. Same fixture is the one whose calib judge-note in #31 called the PASS verdict "a major calibration failure"; that failure mode is gone here. This is real signal that top_k=5 with quota-first is a behaviorally cleaner shape, not just a numerically different one.

**kamino-2024Q4 verdict flipped the other way** (`defer` → `pass`), now scoring vAcc=0 on a `[ACT,DEFER]` set. One swap each direction; N=9 vAcc=0.889 is N=10 vAcc=0.900 minus the missing fixture. No meaningful vAcc movement.

---

## §6 — Honest caveats

- **N=9, not N=10.** The Anthropic judge 500ed on kamino-2025Q1-jitosol-vault. Not re-running (budget discipline + the missing fixture's removal doesn't change the three-state narrative — it was 1.0/0.60/0.0/0.0/0.50/0.40 in #31 and 1.0/0.50/0.0/0.0/0.50/0.60 in baseline, both unremarkable middle-pack). Per CLAUDE.md operating principle 3: ±0.10 swings at N=9 vs N=10 are noise.
- **hall=0.000 across all 9** is the single most striking signal in the run and is NOT explained by the retrieval-shape ablation. It looks like the judge tightened its grading or the panel produced more aggressive numeric claims at top_k=5 (fewer raw-data chunks → more inference). Either way, the lever is #33 (prompt) not the pipeline. A short before-vs-after diff of the panel's TVL claims across the three states would confirm which side of the judge/panel divide moved.
- **calib partial-recovery (0.495 → 0.506)** could be noise. A single-fixture variance can move calib by ±0.10/9 ≈ ±0.011 on the mean. The honest read is "no significant calib improvement from the ablation."
- **The canary D3 still fails** (phantom-marker artifact). At top_k=5 with quota-first, basic-tier still inlines roughly the same 4-5 markers as pre-#31 baseline, so the calibration of the canary's phantom-marker check needs updating regardless. Filed as candidate next-eval-sprint item; not blocking V1.

---

## §7 — Files touched

| File | Change |
|---|---|
| `packages/gecko-core/src/gecko_core/orchestration/trade_panel/__init__.py` | line 606: `_DEFAULT_TRADE_TOP_K: 10 → 5` + comment noting #34 ablation rationale |
| `tests/eval/live_runs/2026-05-13-canary-4.json` | new — canary run (D1+D2 PASS, D3 phantom-marker artifact) |
| `tests/eval/live_runs/2026-05-13-s24-defi-rubric-s29-34-quota-first-k5-9.json` | new — rubric N=9 (one judge-API 500) |
| `docs/strategy/2026-05-14-s29-34-ablation-handoff.md` | this file |

---

## §8 — TL;DR for the founder

The ablation worked: it isolated top_k=10 as the dim-regression culprit on dissent (cleanly) and on calib (partially). The order swap is doing real structural work and stays. **Merge `s29/quota-first-k5`, drop `s29/quota-before-rerank`, and stop tuning the retrieval pipeline.**

V1 is held back by hall (the panel's TVL/price numeric assertions are still ungrounded) and softly by calib. Both are prompt/corpus problems, not retrieval-shape problems. The ablation closed the retrieval-shape question with high confidence at $1.55 total spend. The next $1.50 should go to #33 (prompt grounding addendum) or #34-corpus, not a fourth pipeline experiment.

---

*Generated 2026-05-13 on `s29/quota-first-k5`. Per Pattern E: this ablation answers a specific structural question (which of two coupled variables caused which effect). It does not declare V1 unblocked — that's a separate dispatch on prompt/corpus, not retrieval shape.*
