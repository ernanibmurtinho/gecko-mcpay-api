# S26 #19 — Lost-in-the-Middle Handoff

**Date:** 2026-05-13
**Branch:** `s26/lost-in-middle` (cut from `s26/cap-fix @ a6827c2`)
**Owner:** ai-ml-engineer
**Status:** PARTIAL FIX SHIPPED — V1-ship gate **NO-GO** on `citation_relevance` alone; **GO** on `verdict_accuracy`.

---

## TL;DR

- Shipping **Lever 3** (CITATION BREADTH directive in `_opening_prompt`).
- Reverted Lever 1 (top_k=7) and Lever 2 (middle-out reorder) — both failed or regressed.
- `verdict_accuracy` recovered **1.00** (was 0.67 in S26 final eval — kamino-2024Q4 flipped back to defer, the expected verdict).
- `citation_relevance` **did not move** (0.40 → 0.40) — the rubric judge measures provider-kind coverage, not marker breadth. The lost-in-the-middle fix was real but orthogonal to citation_relevance.

## What each lever did

### Lever 1 — `_DEFAULT_TRADE_TOP_K: 15 → 7` (REVERTED)

Canary: phantom 10/15 → **5/7 (71% same proportion)**. Personas still cited only [1] and [4]. Worse: in the rubric run, all 7 slots were filled with `protocol_native` chunks because #13's boost code preferentially elevates protocol-exact chunks. **Canon (Marks/Damodaran/Berkshire) was starved entirely** — `provider_kinds_present=['protocol_native']` only. This raised hallucination_score regressions in rubric output.

Lever 1 trades one pathology (low marker breadth) for another (corpus-mix collapse). Not a fix.

Live run: `tests/eval/live_runs/2026-05-13-canary-lever1.json`
Commit: `fba2ad1`

### Lever 2 — middle-out chunk reorder (REVERTED)

Reorder so highest-scored land at edges (positions 1, N, 2, N-1, ...). Canary: phantom **12/15** — **worse**, not better. Personas cited [2, 4, 12] only. Reframe: pathology is NOT positional attention drift; gpt-4o-mini cites a fixed budget of ~3–5 chunks regardless of input ordering. Edge-placement of strong chunks did not increase total citations.

Live run: `tests/eval/live_runs/2026-05-13-canary-lever2.json`
Commit: `0e22c3f`

### Lever 3 — CITATION BREADTH directive in `_opening_prompt` (SHIPPED)

Append an explicit instruction to the seed message:

> CITATION BREADTH (mandatory for every non-coordinator voice):
>   You have N numbered corpus chunks ([1] through [N]).
>   Your turn MUST cite at least 4 DISTINCT chunk indices when N >= 6,
>   drawn from across the full range — do NOT cite only edges. Walk
>   the middle. If a middle chunk is not directly relevant, name it
>   briefly and say why you set it aside; do not silently skip.
>   Phantom citation ... disqualifies the turn.

**At N=15 (default top_k):**
- Persona markers seen: `[1, 4, 5, 6, 7, 8, 10, 12]` — **8 distinct, full-range coverage**.
- Phantom citations: **7 of 15** (was 10 of 15 baseline) — better but above the strict ≤2 canary bar.

**At N=7 (Lever 1+3 combined):**
- Markers seen: `[1, 2, 4, 5, 6]` — 5 distinct.
- Phantom: **2 of 7** — meets the canary ≤2 bar.

Even though N=7 hits the canary bar, we ship N=15 + breadth because the rubric showed N=7 starves canon. Canary's phantom-marker test is **necessary but not sufficient** for grounded panels — it doesn't measure provider-kind diversity.

Live runs:
- L3-only N=15: `tests/eval/live_runs/2026-05-13-canary-lever3.json`
- L1+L3 N=7: `tests/eval/live_runs/2026-05-13-canary-lever1plus3.json`

## Rubric N=3 results (Lever 1+3 — the only rubric we could afford)

Suite: `defi_trade_rubric_suite.json`, first 3 fixtures (kamino-2024Q3, kamino-2024Q4, kamino-2025Q1).

| Dimension              | Score | Threshold | Pass |
|------------------------|-------|-----------|------|
| verdict_accuracy       | **1.00** | 1.00      | ✓    |
| citation_relevance     | 0.40  | 0.70      | ✗    |
| provider_kind_coverage | 0.33  | 1.00      | ✗    |
| hallucination_score    | 0.00  | 1.00      | ✗    |
| dissent_grounding      | 0.70  | 0.50      | ✓    |
| confidence_calibration | 0.65  | 0.60      | ✓    |

Live run: `tests/eval/live_runs/2026-05-13-s24-defi-rubric-s26-lever1plus3-3.json`

**Important caveat:** this rubric ran under Lever 1+3 (N=7) which corrupted provider-kind diversity. The N=15 + Lever 3 config that I'm shipping was NOT rubric-tested due to the $2 budget cap. Re-running rubric N=3 on `s26/lost-in-middle` HEAD is the recommended next step before any V1 ship decision.

## V1-ship gate call

Per Gate 1+2 in the autonomous-trade readiness plan: **NO-GO**.

- ✓ `verdict_accuracy ≥ 0.9` — met at 1.00.
- ✗ `citation_relevance ≥ 0.7` — **not met** (0.40, unchanged from baseline).

The rubric judge marks citation_relevance low because:
1. The shipped Lever 1+3 config starved canon — `provider_kinds_present=['protocol_native']` only on all 3 fixtures.
2. Even with N=15 + Lever 3 (untested rubric), the judge wants `must_cite_provider_kinds: [paysh_live, market_data, canon_marks, canon_damodaran, canon_berkshire]` — five distinct provider kinds. The fix to that is **corpus mix**, not citation breadth.

## What the next move should be (founder decision)

Two paths forward, neither I can execute solo:

**Path A — re-run rubric on the shipped (N=15 + L3) config first.** ~$1.50. Plausibly moves citation_relevance up because canon won't be starved; the rubric judge will see canon markers in persona prose. If still < 0.7, fall through to Path B.

**Path B — provider-kind-aware retrieval mix.** Cap protocol_native chunks at N/3 of top_k slots; force-reserve slots for canon and market_data even when their raw scores trail. This is a `retrieve_trade_corpus_chunks` change, not a panel-prompt change. Owner: data-engineer + ai-ml-engineer.

**Do not** iterate further on persona prompts to chase citation_relevance — the rubric is measuring corpus-mix grounding, not in-prompt behavior. Per the directive's hard constraint: "Don't iterate to chase the metric."

## Budget

- Canary lever 1: $0.05
- Canary lever 2: $0.05
- Canary lever 3: $0.05
- Canary lever 1+3: $0.05
- Rubric N=3 (lever 1+3): ~$1.50
- **Total: ~$1.70** (under $2.00 cap)

## Commits on `s26/lost-in-middle`

- `fba2ad1` — Lever 1 (FAILED, reverted)
- `0e22c3f` — Lever 2 (FAILED, reverted)
- (pending) Lever 3 — SHIPPED
