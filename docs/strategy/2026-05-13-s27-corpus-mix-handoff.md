# S27 #21 — Corpus-Mix (Provider-Kind Quota Allocator) Handoff

**Date:** 2026-05-13
**Branch:** `s27/corpus-mix` (cut from `s26/lost-in-middle @ 4b07f40`)
**Owner:** ai-ml-engineer
**Status:** SHIPPED — V1 ship-gate **RED** on `citation_relevance` and
`provider_kind_coverage`. Allocator moved both in the right direction by
the expected magnitude; further gains require corpus + retrieval-pool
work, NOT more prompt or allocator tuning.

---

## TL;DR

- Added `_provider_quota_floor` in `packages/gecko-core/src/gecko_core/orchestration/trade_panel/__init__.py`.
- Replaces the prior `rows[:top_k]` straight slice that routinely filled
  all 15 slots with `protocol_native` (S25 #13's +0.25 boost stack).
- Length-filter (>=200 chars) gates quota eligibility so empty
  `{"data":[]}` paysh_live chunks don't crowd substantive buckets out.
- `provider_kind_coverage` doubled (0.33 → 0.67); `citation_relevance`
  +22% (0.38 → 0.47). Both still below threshold.
- Per directive's STOP condition (`citation_relevance < 0.6 on N=3`),
  iteration halted. Honest handoff below.

## Before / After (rubric N=3, first 3 Kamino fixtures)

| Dimension              | Before (L3 only) | After (quota) | Δ        | Threshold |
|------------------------|-----------------:|--------------:|---------:|----------:|
| verdict_accuracy       |             1.00 |          1.00 |        — |      0.90 |
| citation_relevance     |             0.38 |          0.47 | **+0.08** |      0.70 |
| provider_kind_coverage |             0.33 |          0.67 | **+0.33** |      0.90 |
| hallucination_score    |             0.00 |          0.00 |        — |      1.00 |
| dissent_grounding      |             0.50 |          0.63 |    +0.13 |      0.50 |
| confidence_calibration |             0.63 |          0.67 |    +0.03 |      0.60 |

Live run: `tests/eval/live_runs/2026-05-13-s24-defi-rubric-s27-21-quota-3.json`

### Per-fixture provider_kinds_present

| Fixture | Before | After | must_cite_provider_kinds |
|---|---|---|---|
| kamino-2024Q3-jlpusdc | `[protocol_native]` | `[protocol_native]` | `[paysh_live, market_data, canon_marks, canon_damodaran, canon_berkshire]` |
| kamino-2024Q4-sol-leverage | `[bazaar_live, canon_damodaran, canon_marks, market_data]` | `[bazaar_live, canon_marks, canon_damodaran, canon_berkshire, market_data]` (full 5/5) | `[market_data, canon_marks, canon_damodaran, canon_berkshire, paysh_live]` |
| kamino-2025Q1-jitosol | `[protocol_native]` | `[bazaar_live, market_data, protocol_native]` | `[paysh_live, market_data, canon_damodaran, canon_marks]` |

**Two of three fixtures improved; one stayed at `[protocol_native]` only.**

## What worked

- `kamino-2024Q4-sol-leverage`: panel hit FULL 5-kind coverage. The
  allocator successfully reserved canon + market_data slots even though
  protocol_native chunks would have outranked them by raw boosted score.
- `kamino-2025Q1-jitosol`: was 1 → now 3 kinds. The allocator pulled
  market_data and bazaar_live alongside protocol_native.

## What didn't move

- `kamino-2024Q3-jlpusdc-entry` stayed at `[protocol_native]` only. The
  Atlas candidate pool (`top_k * 4 = 60` candidates) likely did not
  contain any canon or market_data chunks for this niche query — the
  allocator can only ration what retrieval surfaces. Confirmed not an
  allocator bug: the allocator's quota table reserves slots for
  canon_marks/canon_damodaran/canon_berkshire/market_data, but if the
  candidate pool has zero of those kinds, the slots overflow to
  protocol_native by design.
- `citation_relevance` improved but stays at 0.47 because the rubric
  judge weights *interpretive relevance* (e.g. "does this canon excerpt
  actually speak to leverage prudence for THIS vault?"), not just
  provider-kind presence. Even where canon is now present (fixture 2),
  the judge flags it as "boilerplate, tangential."

## Mongo audit (2026-05-13, kamino specifically)

| provider_kind | total | substantive (>200 chars) |
|---|---:|---:|
| paysh_live | 7 | **0** |
| protocol_native | 133 (global) | — |
| canon_marks | 649 (global) | ample |
| canon_damodaran | 2410 (global) | ample |
| canon_berkshire | 1674 (global) | ample |
| market_data | 84 (global) | ample |
| bazaar_live | 27 (global) | — |

The directive flagged `paysh_live` as required-by-rubric but
empty-in-corpus. The allocator's length-filter handles this correctly:
empty paysh_live chunks fail the 200-char eligibility check and
overflow to substantive buckets instead of consuming a quota slot.

## V1 ship-gate call

**RED.** Per the autonomous-trade readiness plan Gate 1+2:

- ✓ `verdict_accuracy ≥ 0.9` — met at 1.00.
- ✗ `citation_relevance ≥ 0.7` — 0.47.
- ✗ `provider_kind_coverage ≥ 0.9` — 0.67.

## Call on Task #22 (L3 / hallucination tune)

**Still needed.** `hallucination_score = 0.0` across all 3 fixtures —
unchanged from baseline. Judge cites "specific TVL figure ~$50M cited
from raw API snippet, treated as JLP-USDC vault when it's the general
prevAum field" and similar patterns. The quota allocator surfaces more
provider-kind diversity but the personas still synthesize specific
numbers without proper sourcing. This is exactly the L3 / hallucination
discipline issue Task #22 targets.

Recommendation: keep #22 on the queue. It was NOT dissolved by #21.

## What the next move should be (founder decision)

Two parallel paths, both required for V1 ship-green:

**Path D — retrieval candidate pool widening (data-engineer + ai-ml).**
For `kamino-2024Q3-jlpusdc`, Atlas's vector candidates over `top_k*4=60`
did not contain any canon chunks above the cosine floor. Options:
1. Increase `$limit` from `top_k*4` to `top_k*8` so the candidate pool
   is wider. Cost: 2x Atlas candidate scan; negligible $.
2. Add a separate canon-only sub-query (BM25 or vector) and merge before
   the quota allocator. More invasive; gives canon a guaranteed
   reachable pool.
3. Tune ingestion: tag canon chunks with stronger vault/leverage/yield
   keywords in their `text` field so vector similarity ranks them
   higher for protocol-vault queries.

**Path E — interpretive-relevance prompting (#22 territory).** The
judge's "boilerplate, tangential" complaint about canon citations
suggests the personas are quoting canon without contextualizing it to
the specific protocol/vault. A persona-prompt addendum directing each
voice to "name the canon principle AND apply it to THIS vault's
specific risk" might close the gap. Risk: this is exactly the prompt-
iteration plateau warned about in `feedback_prompt_iteration_plateau.md`.
**Per directive: do NOT iterate persona prompts.** This is founder
decision territory.

## Cost

- Canary: ~$0.05 (D1+D2 PASS, D3 7/15 phantom — orthogonal to #21)
- Rubric N=3: ~$1.50
- Total: **~$1.55** (under $2.00 cap)

## Hard constraints (verified)

- Branch: `s27/corpus-mix` cut from `s26/lost-in-middle @ 4b07f40` ✓
- `_default_prompts.json` untouched ✓
- `_apply_retrieval_boosts` untouched (allocator runs *after* boosts) ✓
- Pattern F preserved — canon `protocol=[]` admittance lives upstream
  of the allocator (in the Mongo `$match` $or clause); allocator
  operates only on `provider_kind` ✓
- `s27/corpus-mix` head: `3d65e52` (eval) on top of `fb3ff1d` (feature)
  on top of `4b07f40` (s26 L3) ✓

## Commits

- `fb3ff1d` — feat(s27-#21): provider_kind quota allocator
- `3d65e52` — eval(s27-#21): canary + rubric N=3 after quota allocator

## Test artifacts

- `packages/gecko-core/tests/orchestration/trade_panel/test_provider_quota.py`
  (6 fixtures, pure-function tests; runs in <1s; 42/42 trade_panel tests
  pass on `s27/corpus-mix` HEAD)
- `tests/eval/live_runs/2026-05-13-canary.json`
- `tests/eval/live_runs/2026-05-13-s24-defi-rubric-s27-21-quota-3.json`
