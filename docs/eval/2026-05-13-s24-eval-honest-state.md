# S24 night-shift — eval honest state, founder morning review

**Date:** 2026-05-12 (night) → 2026-05-13 (morning review)
**Branch:** `s24/wave-1-execution`
**Suite:** `tests/eval/suites/defi_trade_suite.json` (N=10)
**Tier:** basic (gpt-4o-mini end-to-end)

## TL;DR

Night-shift patches landed **confidence-band normalization** (the collapsed-0.75 failure mode is fixed — spread now spans 0.40-0.75) and the **defer override for high-conviction dissent**. The **jupiter-2025Q1-lst-rotation-msol** problem case from the prior run is now correctly called `pass` at confidence 0.40 instead of `pass` at 0.75.

But **3 of 4 metrics regressed or held a miss**. Per the founder's stop-rule, this is a structural gap, not a tune-it-tonight problem.

## 4-metric scorecard (post-patch, N=10)

| Metric | Target | Pre-patch | Post-patch | Status |
|---|---|---|---|---|
| brier_overall | ≤ 0.30 | 0.277 | **0.218** | PASS (improved) |
| act_30d_profit_rate | ≥ 0.58 | 0.75 (n=4) | **0.50 (n=2)** | FAIL (n shrinks; signal noisy) |
| pass_drawdown_avoid_rate | ≥ 0.60 | 0.333 | **0.333** | FAIL (unchanged) |
| defer_rate | < 0.30 | 0.30 | **0.50** | FAIL (regressed) |

Verdict distribution shifted from `act=4 pass=3 defer=3` → `act=2 pass=3 defer=5`. The panel got more cautious, not more accurate.

## Confidence-band actual distribution

`0.40, 0.40, 0.40, 0.40, 0.50, 0.65, 0.70, 0.70, 0.70, 0.75`

Pre-patch was `0.75 × 10` (collapsed). Code-level normalization works as designed.

## Per-fixture deltas vs. pre-patch

| Fixture | Pre-patch | Post-patch | Ground truth |
|---|---|---|---|
| kamino-2024Q3-jlpusdc-entry | act / 0.75 | defer / 0.75 | act-correct (+4.8%, -3.1% dd) — **REGRESSED** |
| kamino-2024Q4-sol-leverage-entry | defer / 0.75 | act / 0.50 | act-correct (+52.4%, -8.2% dd) — **IMPROVED** |
| kamino-2025Q1-jitosol-vault | act / 0.75 | pass / 0.40 | act-correct (+1.9%, marginal) — REGRESSED |
| kamino-2025Q3-usdc-pyusd-exit | pass / 0.75 | defer / 0.70 | pass-correct (+0.3% yield trap) — REGRESSED |
| drift-2024Q4-sol-perp-long | act / 0.75 | act / 0.40 | act-WRONG (-22.6%, -27% dd) — calibration improved |
| drift-2025Q2-jto-perp-short | act / 0.75 | pass / 0.40 | act-correct (+14.1%) — REGRESSED (flipped direction) |
| jupiter-2024Q3-jlp-vs-jitosol | pass / 0.75 | defer / 0.70 | act-correct (rotation won) — was wrong, now abstaining |
| **jupiter-2025Q1-lst-rotation-msol** | **pass / 0.75 dis=3** | **pass / 0.40 dis=2** | pass-correct (rotation lost) — **TARGET FIX LANDED** |
| jito-2025Q2-mev-tip-band | defer / 0.75 | defer / 0.65 | act-on-P75 was correct — defer is acceptable |
| sanctum-2025Q3-unstake-vs-hold | defer / 0.75 | defer / 0.70 | hold-won — defer acceptable |

## What the patches did

### 1. Strategist default-action matrix (prompt)

Added explicit table mapping `(technical, sentiment, fundamental, risk) → default action` to `_default_prompts.json:strategist`. Override-allowed but must be justified in body.

**Effect:** indirect; the coordinator still over-rode the strategist's directional intent toward `defer` in several cases.

### 2. Coordinator confidence anchors + penalties (prompt)

Added explicit anchoring at 0.70 baseline minus 0.10/dissent and 0.05/abstain. Said "do not default every run to 0.75".

**Effect:** combined with code-level enforcement, this is what produced the visible band spread. Working as designed.

### 3. Coordinator defer rule (iii) (prompt)

Added: "you are about to call 'pass' AND 3+ primary analysts point against it → defer".

**Effect:** the model interpreted this more broadly than intended — it now defers on **any** moderate disagreement, not just 3+ dissent. **Defer rate doubled from 0.30 to 0.50.** This is the regression vector.

### 4. Code-level normalization in `_build_verdict_from_coordinator`

- `_count_abstains` helper (counts mixed/neutral/stable/elevated across 4 primary analysts).
- Re-derives dissent from analyst turns; takes max(self-report, derived).
- Confidence ceiling = 0.70 − 0.10·dissent − 0.05·abstains, clamped [0.30, 0.85].
- Defer override fires only at `dissent_count >= 3` (symmetric act↔pass).
- Abstain-floor override fires at `derived_abstains >= 3`.

**Effect:** the confidence-band fix is here, not in the prompt. None of the post-hoc verdict overrides actually fired in this N=10 run — the coordinator volunteered `defer` on its own.

## Structural diagnosis (the honest part)

**The defer-rate regression is a prompt regression, not a code regression.** The new defer-(iii) clause in the coordinator prompt is too lenient — gpt-4o-mini interprets "high-conviction dissent" as "any analyst flagged a concern". The override in code is correctly gated at `dissent >= 3` and never fired, but the coordinator pre-emptively chose defer based on the prompt language.

**The pass_drawdown_avoid_rate miss is a different problem.** Only 1 of 3 `pass` verdicts correctly identified the worse-than-15% drawdown case. The pass calls in the post-patch run were on protocols where the corpus didn't surface enough downside signal — this is a **retrieval/corpus quality gap, not a coordinator gap**. The pass voices simply don't see the impending drawdown.

**N=10 is too small to make calibration claims.** With 2 acts and 3 passes, single-fixture flips swing the rate by 33-50%. The expansion to N=30 (deferred per stop-rule) would let us read these metrics with real confidence intervals.

## Recommended next structural fix (founder decision needed)

Three options, in increasing scope:

**Option A — Soften the prompt, keep the code.** Revise coordinator prompt defer-(iii) from "3 or more" being subjective to "3 or more analysts' closing-line tokens explicitly oppose the verdict". Make it mechanical, not interpretive. Keep the code-level overrides as the structural safety net. Cost: 0. Eval cost: ~$0.30 for a re-run.

**Option B — Add a "downside scout" voice.** The pass_drawdown_avoid_rate miss is a retrieval problem: no voice is dedicated to "what's the worst-case loss path here". Add an 8th voice or repurpose risk_manager to explicitly enumerate downside scenarios with magnitude estimates. This is a sprint, not a night-shift.

**Option C — Tighten the corpus, not the panel.** The N=10 acts that misfired all had thin or stale market_data chunks. Investing in retrieval-side quality (real-time Pyth windows, recent CL data) probably moves both `act_30d_profit_rate` and `pass_drawdown_avoid_rate` more than any further prompt tuning. Aligns with `project_retrieval_wedge_bug_2026_05_11`.

My recommendation is **A then C, skipping B**: cheapest revert first, then invest in retrieval. B is a v0.2 conversation.

## What I did NOT do per stop-rule

- Did **not** expand the suite from 10 → 30 fixtures. The "3-of-4 land green" precondition was not met.
- Did **not** run pro tier (cost discipline, not productive given N=10 noise).
- Did **not** revert the patches. They include genuine fixes (confidence-band spread, jupiter-2025Q1 verdict). Founder should choose Option A and re-run before deciding to revert.

## Files changed

- `packages/gecko-core/src/gecko_core/orchestration/trade_panel/_default_prompts.json` — strategist matrix, coordinator confidence anchors + defer rule (iii)
- `packages/gecko-core/src/gecko_core/orchestration/trade_panel/__init__.py` — `_count_abstains`, post-hoc dissent/confidence normalization, defer overrides
- `docs/eval/2026-05-13-s24-eval-honest-state.md` — this file

## Run artifacts

- Pre-patch: `tests/eval/live_runs/2026-05-12-s24-defi-trade-10.json`
- Post-patch: `tests/eval/live_runs/2026-05-12-s24-defi-trade-10-2.json`

Spend tonight: ~$0.30 (one 10-fixture basic-tier sweep). Within budget.

---

## Resolution attempt 2026-05-12 — Option B applied, FAILED, surgical next-fix below

**Action:** rewrote coordinator defer-(iii) from interpretive ("3+ analysts point against") to mechanical token-count rule (count exact closing-line opposition tokens; >=3 → defer; <2 → never defer from this clause). Added explicit non-trigger list ("elevated" doesn't count, "stable" doesn't count, "neutral" doesn't count, etc.). Code-level safety nets untouched.

**Side-finding (load-bearing, surface to founder):** `_default_prompts.json` at HEAD parses as INVALID JSON (trailing `,` after `"coordinator"` value + trailing `,` after `agents` object). `json.loads` rejects it cleanly. Yet the night-shift baseline `2026-05-12-s24-defi-trade-10-2.json` reports 10 successful panel runs. Two possibilities:
  (a) `GECKO_TRADE_PANEL_PROMPTS_PATH` was env-mounted to a different (valid) file at the night-shift run, and the bundled `_default_prompts.json` is structurally broken but never loaded in prod;
  (b) the baseline file is a leftover from a still-earlier run and the night-shift sweep actually crashed before writing.
Either way: the night-shift baseline cannot be trusted as a true post-patch state until reconciled. I removed the trailing commas as part of this turn's edit; JSON now parses.

### Post-Option-B 4-metric scorecard (N=10, basic, fresh sweep)

| Metric | Target | 10-2 (baseline) | 10-3 (Option B) | Status |
|---|---|---|---|---|
| brier_overall | <= 0.30 | 0.218 | **0.16** | pass (improved) |
| act_30d_profit_rate (n>=4) | >= 0.58 | 0.50 (n=2) | **null (n=0)** | FAIL (no acts at all) |
| pass_drawdown_avoid_rate | >= 0.60 | 0.333 | **0.0 (n=1)** | FAIL |
| defer_rate | < 0.30 | 0.50 | **0.90** | FAIL (regression) |

Verdict distribution: `act=0 pass=1 defer=9`. The panel collapsed almost entirely into defer. **1 of 4 metrics green. Worse than baseline.** Resolution call: **NO**, 3-of-4 not landed.

### Why Option B failed (honest diagnosis)

The mechanical rewrite is longer and structurally more salient than the prior interpretive clause. gpt-4o-mini now treats the closing-line enumeration as a *checklist of reasons to defer* rather than the *narrow guardrail* it was intended to be. The "EXPLICIT NON-TRIGGERS" subsection added bulk that reads as authority on when defer applies, not when it doesn't. We added more words to the defer clause; the model deferred more. Predictable in hindsight.

The token-count rule itself is sound, but **a mechanical count belongs in code, not in a prompt**. gpt-4o-mini cannot reliably count exact-match tokens across 6 prior turns; it pattern-matches "rule about defer → defer is the safe call".

### Surgical next-fix recommendation (Option A.2)

**Move defer-(iii) out of the prompt entirely. Implement it in `_build_verdict_from_coordinator` as a deterministic counter, symmetric for act/pass.**

1. Delete the entire defer-(iii) clause from the coordinator prompt. Keep (i) abstain-majority and (ii) balanced-debate as the only prompt-level defer triggers. The prompt then reads: "default to act or pass; defer only when the panel itself abstained or the debate is genuinely tied."
2. In code, after the coordinator returns its draft verdict, parse the 6 voice closing lines, count exact-match opposition tokens against the draft direction, and override draft → defer only when count >= 3. This is what the existing `_count_abstains` infrastructure already does for abstain-majority; extend it to opposition-majority.
3. The eval ask is then: does removing the prompt-side defer clause restore act/pass volume (target defer_rate <= 0.30), while the code-side counter retains the explicit-opposition safety net?

Cost: ~30 min code, $0.30 eval. Lower-risk than further prompt edits — every prompt iteration so far has *increased* defer rate because the model is biased toward caution when the prompt mentions defer at all.

**If A.2 also fails: the structural problem is gpt-4o-mini's caution bias on this domain, not the prompt or the code. Move to gpt-4o (mini → full) for the coordinator role only, leaving the 6 analyst roles on mini for cost. Expected ~3x coordinator-call cost; basic tier rises from ~$0.0015 to ~$0.005/idea. Still within tier economics.**

### Trailing-comma fix is in this commit

`_default_prompts.json` HEAD-state would not parse under strict JSON. This turn's edit also removed the two trailing commas that were blocking parse. Whatever the baseline-10-2 run was actually loading, the next run will deterministically load the bundled file.

### Files changed this turn

- `packages/gecko-core/src/gecko_core/orchestration/trade_panel/_default_prompts.json` — coordinator defer-(iii) rewritten to mechanical token-count rule; trailing commas removed; JSON now parses strict.
- `docs/eval/2026-05-13-s24-eval-honest-state.md` — this Resolution section.

### Run artifact

- `tests/eval/live_runs/2026-05-12-s24-defi-trade-10-3.json` (Option B sweep, defer_rate=0.90).

Spend this turn: ~$0.30. Cumulative: ~$0.60. Within $1 budget.
