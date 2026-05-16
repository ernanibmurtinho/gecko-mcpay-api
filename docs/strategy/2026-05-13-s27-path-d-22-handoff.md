# S27 Path D + #22 — Overnight V1 Ship-Gate Save Attempt — Handoff

**Date:** 2026-05-13
**Branch:** `s27/path-d-22` (cut from `s27/corpus-mix @ fa15a09`)
**Owner:** ai-ml-engineer (overnight, autonomous)
**Status:** **STOPPED at Path D per directive STOP rule.** Task #22 was
NOT attempted. V1 ship-gate **RED** — slip to S28 + Cohere Rerank.

---

## TL;DR

- Path D shipped: Atlas candidate pool widened `top_k*4 -> top_k*12`
  (plus proportional bump to `numCandidates` and vectorSearch `limit`).
- `provider_kind_coverage` **fully solved** (0.667 -> 1.000). The
  previously-stuck `kamino-2024Q3-jlpusdc` fixture went from 1 kind to
  5 kinds. Pattern F structural fix landed cleanly.
- `citation_relevance` did **NOT** improve (0.467 -> 0.450, within
  judge noise). The rubric judge now flags the broader citations as
  "boilerplate, tangential" — the interpretive-relevance ceiling the
  `s27/corpus-mix` handoff §"What didn't move" already named.
- citRel did not cross the 0.55 acceptance gate to chain task #22.
  Per directive: STOP, do not attempt #22, V1 slips to S28 + Cohere
  Rerank.
- Total spend: ~$1.60.

---

## Path D — what shipped

Single feature commit: `787d916`. One file:
`packages/gecko-core/src/gecko_core/orchestration/trade_panel/__init__.py`.

```
# vectorSearch stage
numCandidates: max(200, top_k * 20)  ->  max(400, top_k * 40)
limit:         top_k * 10            ->  top_k * 15

# post-$match $limit (the candidate pool the quota allocator sees)
$limit: top_k * 4                    ->  top_k * 12
```

`_apply_retrieval_boosts` and `_provider_quota_floor` untouched per
hard constraint. The allocator's logic was correct all along; it just
needed a wider pool to ration from.

## Results — rubric N=3 (Kamino fixtures)

Baseline = `s27/corpus-mix HEAD` (post-#21 quota allocator). Path D =
this branch HEAD.

| Dimension              | Baseline | Path D | Δ        | V1 threshold |
|------------------------|---------:|-------:|---------:|-------------:|
| verdict_accuracy       |    1.000 |  1.000 |        — |         0.90 |
| citation_relevance     |    0.467 |  0.450 |   -0.017 |         0.70 |
| provider_kind_coverage |    0.667 |  1.000 | **+0.33** |         0.90 |
| hallucination_score    |    0.000 |  0.000 |        — |         0.50 |
| dissent_grounding      |    0.633 |  0.700 |    +0.07 |         0.50 |
| confidence_calibration |    0.667 |  0.700 |    +0.03 |         0.60 |

Live run: `tests/eval/live_runs/2026-05-13-s24-defi-rubric-s27-pathD-3.json`
Canary:   `tests/eval/live_runs/2026-05-13-canary-s27-pathD.json` (D1+D2 PASS, D3 phantom-cite fail — pre-existing on s26/s27 lines, orthogonal)

### Per-fixture provider_kinds_present

| Fixture | s27/corpus-mix (quota only) | Path D | Required (must_cite) |
|---|---|---|---|
| kamino-2024Q3-jlpusdc | `[protocol_native]` (1/5) | `[bazaar_live, canon_damodaran, canon_marks, market_data, protocol_native]` (5/5) | paysh_live, market_data, canon_marks, canon_damodaran, canon_berkshire |
| kamino-2024Q4-sol-leverage | 5 kinds | 5 kinds | market_data, canon_marks, canon_damodaran, canon_berkshire, paysh_live |
| kamino-2025Q1-jitosol | 3 kinds | 5 kinds | paysh_live, market_data, canon_damodaran, canon_marks |

**All 3 fixtures now hit `provider_kind_coverage = 1.0`.** The
allocator quotas fill on every query, not just 2 of 3. The §4
escalation trigger from the corpus-mix handoff (Path D widens pool)
worked exactly as designed for the dimension it targeted.

## Path D acceptance gate — gating decision

Directive:
> **Acceptance for Path D alone:** rubric N=3 citation_relevance ≥ 0.55
> AND provider_kind_coverage ≥ 0.7. If both pass, proceed to #22. If
> either fails, STOP at Path D and report — V1 slips to S28 + Cohere
> Rerank.

- pkCov: 1.000 ≥ 0.7 — **PASS**
- citRel: 0.450 < 0.55 — **FAIL**

**Decision: STOP. Task #22 not attempted.**

The interpretive-relevance ceiling is real and is exactly what Cohere
Rerank is built to fix — rerank scores citations by interpretive
relevance to the query, not by vector cosine + provider-kind quotas.
No amount of L3 directive tuning will make the judge call a generic
Damodaran ERP paragraph "directly relevant to JLP-USDC vault risk."
The corpus needs reranking, not more prompt iteration.

## Why #22 wouldn't have helped V1 either

The directive's #22 acceptance was:
> hallucination_score ≥ 0.5 (recovering from 0.00) without regressing
> verdict_accuracy below 0.9 or dissent_grounding below 0.5.

Even if #22 had landed and hit those bars, the V1 combined ship-gate
required:
> citRel ≥ 0.7 AND pkCov ≥ 0.9 AND hall ≥ 0.5 AND vAcc ≥ 0.9

citRel = 0.45 makes V1 GREEN impossible without an interpretive-
relevance lever Path D + #22 do not contain. #22 targets hallucination,
not citRel; even a perfect #22 leaves V1 RED on citRel.

## V1 ship-gate color

**RED.** Definition:
> V1 RED (slip to S28 + Cohere Rerank): citRel<0.55 OR pkCov<0.7

citRel 0.45 < 0.55 → RED. Slip to S28 + Cohere Rerank.

Note: this is a *better* RED than the s27/corpus-mix handoff RED.
That handoff was RED on both pkCov (0.67) and citRel (0.47). Path D
solved pkCov outright. S28 has one job, not two.

## Cost

| Step | Cost |
|---|---:|
| Canary (Path D) | ~$0.05 |
| Rubric N=3 (Path D) | ~$1.55 |
| **Total** | **~$1.60** |

Under the $3.50 target and well under the $4.00 hard stop. #22 budget
unspent.

## Hard constraints (verified)

- Branch: `s27/path-d-22` cut from `s27/corpus-mix` ✓
- `_default_prompts.json` untouched ✓
- `_apply_retrieval_boosts` untouched ✓
- `_provider_quota_floor` untouched ✓
- `_opening_prompt` untouched (#22 not attempted, L3 directive
  unchanged) ✓
- Path D = JUST the multiplier (plus matched bumps to numCandidates
  and vectorSearch limit so the wider pool can actually be returned) ✓
- No `--no-verify`, no force push, no main push ✓
- Canary + rubric both invoke `run_trade_panel_with_retrieval`
  (production path, not canned context) ✓

## Commits

- `787d916` — feat(s27-path-d): widen Atlas candidate pool 4x → 12x
- `f0f9388` — eval(s27-path-d): canary + rubric N=3 artifacts

## Recommended next move

**Slip V1 ship-gate to S28. Build Cohere Rerank into
`retrieve_trade_corpus_chunks` between `_apply_retrieval_boosts` and
`_provider_quota_floor`.**

Specifically:
1. Add a rerank stage that takes the widened pool (now 180 candidates,
   diverse provider_kinds thanks to Path D) and rescores by
   interpretive relevance to the idea.
2. The quota allocator then operates on rerank scores instead of
   raw vectorSearch + boost scores. Canon chunks that are *actually*
   on-point for the specific vault get picked over generic ERP
   paragraphs.
3. Rerank cost: Cohere v3 is ~$1/1k searches. At cache-then-charge
   cadence this is negligible.

Path D is a strict prerequisite for Cohere Rerank working — Cohere
can only rerank what we give it. Without the 4x → 12x widening, even
a perfect reranker has zero canon to elevate for niche fixtures.
**Path D ships to main.** S28 layers rerank on top.

Do NOT iterate prompts further. Per
`feedback_prompt_iteration_plateau.md`, this exact "tune one more
prompt knob and citRel will move" thinking burned 4 iterations in
S24 with no signal. The corpus interpretive layer is the real
constraint. Fix it at the retrieval layer.

## Open questions for the founder

1. **Ship Path D to main now?** It's a strict improvement (pkCov
   +0.33, no regressions). Even if V1 slips, Path D is the right
   default forever. Recommendation: yes, merge to main when awake.
2. **S28 Cohere Rerank scope.** Just rerank, or also reranker + a
   sparse BM25 sidecar for canon (the option 2 from the corpus-mix
   handoff)? Reranker first, sidecar deferred is my call.
3. **Task #22 fate.** Not dissolved by this run. Hallucination
   discipline is still a real gap (hall=0.0 across all 3 fixtures).
   But it's a S28+ ticket now, not a V1 blocker — once citRel
   crosses 0.55 via rerank, #22 becomes the second-to-last knob
   before V1 GREEN.

---

## Report-back per directive

1. **Path D rubric N=3 before vs after:**
   - citRel 0.467 → 0.450 (−0.017, judge noise)
   - pkCov 0.667 → 1.000 (+0.33, **solved**)
   - hall 0.000 → 0.000 (—)
2. **Did Path D cross 0.55?** No. citRel 0.45 < 0.55. **Gating
   decision: STOP, do not attempt #22.**
3. **#22 result:** N/A (not attempted per STOP rule).
4. **Final V1 ship-gate color:** RED (citRel < 0.55).
5. **Total spend:** ~$1.60.
6. **Single recommended next move:** Merge Path D to main, slip V1
   to S28 + Cohere Rerank. Path D is a strict-improvement prerequisite
   for Rerank to have anything diverse to rerank from.
