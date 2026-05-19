# S36-#114 — Panel Reasoning Trace (7-voice turn-by-turn)

**Date:** 2026-05-18
**Branch:** `s36/prompt-grounding`
**Owner:** ai-ml-engineer (PANEL lane). Paired with #113 (data-engineer, retrieval lane) and complementary to #112 (eval-artifact/judge audit).
**Authorized spend:** ≤5 fresh panel runs via OpenRouter (`LLM_ROUTER=openrouter`), bounded well under $1. 2 fresh runs used.

## 1. Fixture selection

The #110 ship-gate is 5 artifacts (50 rows):
`tests/eval/live_runs/2026-05-18-s24-defi-rubric-s36-110-shipgate-r{1..5}-10.json`.

Ranked the 10 fixtures by **mean `hallucination_score` across the 5 runs**, ascending (worst first):

| mean hall_score | fixture | per-run hall_score |
|---|---|---|
| **0.000** | `kamino-jitosol-vault` | [0,0,0,0,0] |
| **0.000** | `jupiter-jlp-vs-jitosol` | [0,0,0,0,0] |
| **0.000** | `jupiter-lst-rotation-msol` | [0,0,0,0,0] |
| **0.000** | `jito-mev-tip-band` | [0,0,0,0,0] |
| **0.000** | `sanctum-unstake-vs-hold` | [0,0,0,0,0] |
| 0.500 | drift-jto-perp-short | [1,.5,0,0,1] |
| 0.600 | kamino-sol-leverage-entry | [0,1,1,1,0] |
| 0.600 | kamino-usdc-pyusd-roll | [0,1,0,1,1] |
| 0.700 | drift-sol-perp-long | [.5,1,1,.5,.5] |
| 1.000 | kamino-jlpusdc-entry | [1,1,1,1,1] |

**Bottom-5 (the trace set):** `kamino-jitosol-vault`, `jupiter-jlp-vs-jitosol`, `jupiter-lst-rotation-msol`, `jito-mev-tip-band`, `sanctum-unstake-vs-hold`. All five score a *deterministic* 0.000 in every one of the 5 runs — this is not small-N variance, it is a structural fail. #113 uses the identical rule, so the fixture sets match.

Note the spread: the score is bimodal per-fixture (a fixture is either 0.0 every run or rarely 0.0). The 0.34 aggregate is not noise around a mean — it is ~5 fixtures pinned at 0 dragging an otherwise-passing set.

## 2. The reasoning path & which voice hallucinates

Source (a): mined `turns[]` from all 50 #110 artifacts. Source (b): 2 fresh instrumented OpenRouter `pro`-tier runs (`jito-mev-tip-band`, `sanctum-unstake-vs-hold`) — reproduced the failure exactly.

**It is not one persona — it is systemic, and it cascades.** Trace of `jito-mev-tip-band` (r1 artifact, reproduced in fresh run):

1. `technical_analyst` — introduces `8.8785e-06 SOL as of May 16, 2026 [2]` and `5.04575e-06`. **Origin turn.** Attributes the figure to citation `[2]` (the Jito tip-floor `protocol_native` chunk).
2. `sentiment_analyst` — *repeats* `8.8785e-06`, *repeats* `5.04575e-06`, builds the "rising tip floor" narrative on them.
3. `fundamental_analyst` — *repeats* both figures verbatim, declares `protocol health: growing` on them.
4. `risk_manager` — does not add figures (qualitative).
5. `strategist` — *invents a new figure*: falsifier "P75 tip floor drops below `7e-06` SOL" — `7e-06` appears in no upstream turn or citation.
6. `bull_bear_debater` — *invents another*: decisive question "P75 tip floor remain above `8.5e-06` SOL" — also novel.
7. `coordinator` — carries `8.8785e-06` into `key_drivers` and the verdict prose.

This is the **cascade the founder asked about, confirmed**: an early voice's number propagates through every downstream voice and into the coordinator's `key_drivers`. Each fixture shows the same shape — the first analyst to touch a number seeds it; the rest amplify; the strategist/debater freelance *additional* round numbers (`30 days`, `1%`, `$50M`, `50%`) that have no source at all. `sanctum-unstake-vs-hold` is identical: `sentiment_analyst` seeds `26.12% APY [3]`, `risk_manager` + `bull_bear_debater` repeat it. `jupiter-jlp-vs-jitosol`: `technical_analyst` seeds `-2.33%`, `sentiment_analyst` repeats it plus `$173 million`.

So: **systemic across all 7 voices, with a clear seed→amplify cascade.** No single persona to blame; the analyst personas seed, the strategist/debater fabricate fresh round numbers, the coordinator launders them into `key_drivers`.

## 3. The named failure mode — overflow / null-turn check

**Explicitly checked. Result: NO null, blank, truncated, or errored turns. NO context overflow.**

Evidence:
- All 50 #110 rows: `total_turns=7`, `degraded_turn_count=0`, `panel_retries=0` — every voice produced a turn, none degraded, no retries fired.
- Mined turn lengths: every turn is 1200–2600 chars of coherent prose. None empty, none cut mid-sentence.
- Fresh runs: `jito` total turn text **14,170 chars (~3.5K tokens)**; `sanctum` **11,870 chars (~3.0K tokens)**. Add the 15-chunk retrieval context (~6–8K tokens) and personas — total panel context is well under 20K tokens against a gpt-4o 128K window. **Not remotely close to overflow.**
- The earlier-incident cascade ("one hallucination → context overflow → null turn → compromised verdict") **is not what is happening here.** The verdicts are compromised, but the compromise is upstream of any context-size effect: every turn is healthy and complete; the turns are simply *confidently citing numbers that are not in the visible snippets*.

This is the important negative result for the founder: the #110 3/6 is **not** the overflow/null-turn failure mode. Re-running with more budget will not surface it. The cause is structural and reproducible at small context.

## 4. Does the grounding gate leave the figure in the text? — YES

This is the headline mechanism. The S36 grounding gate (`packages/gecko-core/src/gecko_core/orchestration/trade_panel/grounding_gate.py`) runs, fires, and **does exactly what it was coded to do — which is not enough to move `hallucination_score`.**

On the bottom-5, the gate fires and produces a blocker question, e.g. for `jito-mev-tip-band`:
> "Grounding gate: the following figures are not confirmed by any cited snippet and should be treated as unverified — 1%, 5.04575e-06, 7e-06, 8.5e-06, 8.8785e-06."

Fresh-run gate output confirms it: `claims=14, grounded=0, ungrounded=14, grounded_fraction=0.00, abstained=True` for `jito`; `10/0/10, 0.00, True` for `sanctum`. The gate correctly detects **every** numeric claim as ungrounded.

But `grounding_gate.py` lines 292–330 (`apply_grounding_gate`) are explicit by design:
> "the `verdict` literal ... is NOT flipped ... the gate only signals 'do not trust the numbers' via confidence + an explicit blocker."

The gate's two actions are: (1) floor `confidence` toward 0.0, (2) **append** a blocker question. It **never scrubs the figures** from `turns[]`, `key_drivers`, or the coordinator verdict prose. So `8.8785e-06 SOL`, `26.12% APY`, `$173 million` all remain sitting in the turn text and in `key_drivers`.

The rubric judge then reads that exact text, sees an unsourced specific figure matching a `must_not_hallucinate` pattern ("specific Jito tip-floor lamport figure cited without paysh_live or market_data"), and scores `hallucination_score = 0.0`. The judge notes for all five fixtures confirm this verbatim — several even quote the gate's own blocker back as proof the figure is unverified, then penalize anyway.

**The gate and the judge agree the numbers are ungrounded. The gate just leaves them on the page for the judge to penalize.** Worse, the appended blocker text itself contains the figures (`8.8785e-06`, `26.12%`), so the gate's own output surface re-introduces the very numbers into the text the judge scans (`grounding_gate.py` scans `blocker_questions` as a surface — see `_verdict_surfaces`, line 215).

## 5. Headline — what is actually going wrong, and what would move the score

**What is going wrong (two stacked causes):**

**Cause A — citation snippets contain no figures.** The `protocol_native` Jito chunk's emitted `snippet` is API *metadata* — "Endpoint description: Jito tip-floor percentiles (P25/P50/P75/P95/P99) ... Direct lamport figures that ground 'P75 vs P90' questions" — and contains **zero actual lamport values**. The figure `8.8785e-06` is nowhere in any snippet the gate or judge can read. So the panel either (i) read the figure from the *full chunk body* (which the snippet truncates away) and cited it honestly, or (ii) fabricated it. **From the snippet alone this is untestable — and that ambiguity is the bug.** This is the #112/#113 lane: snippet rendering for number-bearing `protocol_native` chunks is dropping the numbers. This is the S36-WS2 "number-first renderer" work — and it is **not landing for these chunk subkinds** (`mev_tip_data` etc.). #113 should confirm whether the figures live in the chunk body.

**Cause B (panel lane, mine) — the panel freelances round numbers with no source at all.** Independent of A: the `strategist` and `bull_bear_debater` invent `7e-06`, `8.5e-06`, `$50M`, `50%`, `30 days`, `1%` that appear in **no** upstream turn and **no** chunk. These are pure fabrication — gpt-4o/4o-mini reaching for plausible-looking precision. The S36 abstain prompt (`commit 526e149`, "say 'no figure in corpus'") does not stop this: per `memory/feedback_prompt_iteration_plateau`, these models do not obey instruction-style grounding prompts. The strategist persona in particular is *asked* to give a falsifier with a threshold, and it manufactures a number to fill the slot.

**What would actually move `hallucination_score` — honest assessment:**

1. **Real fix, panel lane (mine), high-confidence:** Make `apply_grounding_gate` *redact*, not just annotate. The gate already identifies every ungrounded span with exact offsets (`NumericClaim`). Replacing each ungrounded span in `turns[]` text and `key_drivers` with a neutral token (e.g. `[unverified figure]`) before the verdict is emitted **removes the text the judge penalizes**. The judge would then see no unsourced figure and `hallucination_score` rises. This is a real fix, not a guess — it directly severs the mechanism in §4. Caveat: redaction is verdict-shape-mutating; needs a `tests/eval` fixture run to confirm it doesn't tank `dissent_grounding`/`citation_relevance`, and the redaction must also clean the gate's own appended blocker (don't echo `8.8785e-06` back into a surface).

2. **Real fix, retrieval lane (#113, not mine):** Fix the `protocol_native` snippet renderer so number-bearing chunks (`mev_tip_data`, APY snapshots) emit the actual figures in the `snippet`. Then honestly-cited figures *ground* and the judge passes them. This fixes Cause A only — Cause B's pure fabrications still fail. Both lanes are needed.

3. **Weaker, do not rely on it:** Another prompt iteration on the abstain instruction. History says this plateaus (S24: 1.0→0.20→0.50→0.90 thrash; S29-#33 and the S36 526e149 addendum both already failed). A prompt change is a guess; do not spend N=50 on it.

**Recommendation:** ship fix #1 (gate redaction) — it is code-side, deterministic, prompt-free, and structurally severs the documented mechanism — paired with #113's snippet fix for Cause A. Validate with a `tests/eval` fixture run on the bottom-5 *before* any N=50 re-measure. Do **not** burn another N=50 ship-gate on a prompt tweak.

---

### Appendix — per-fixture summary (r1 artifact)

| fixture | verdict | seed voice / seed figure | gate fired | figures left in text |
|---|---|---|---|---|
| kamino-jitosol-vault | defer | bull_bear_debater misreads `0.0594%` | no explicit gate blocker | yes (`0.0594%` in turn) |
| jupiter-jlp-vs-jitosol | defer | technical_analyst `-2.33%`; sentiment `$173M` | yes — flags `$173M,1%,100%,2.33%` | yes (turns + key_drivers) |
| jupiter-lst-rotation-msol | act | coordinator yield-narrative claim on `[4]` | yes — flags `$50M,1%,50%` | yes |
| jito-mev-tip-band | act | technical_analyst `8.8785e-06 SOL [2]` | yes — flags 5 figures | yes (turns + key_drivers) |
| sanctum-unstake-vs-hold | defer | sentiment_analyst `26.12% APY [3]` | yes — flags `1%,10%,26.12%` | yes (3 turns + key_drivers) |

All five: gate detects correctly, leaves the figure in the judged text, judge scores 0.0.
