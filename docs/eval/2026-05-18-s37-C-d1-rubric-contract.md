# S37 Phase-1-C — D1 rubric-contract analysis

**Date:** 2026-05-18 · **Owner:** staff-engineer · **Status:** read-only analysis

## The finding (from #113)

Every defi-trade fixture's `must_not_hallucinate` rule demands the panel
ground figures in `market_data` / `paysh_live` provider_kinds — but zero such
chunks reach the panel on any fixture. Part of the `hallucination_score`
penalty is, as written, structurally unwinnable.

## Root cause — a corpus/ingestion gap, NOT a retrieval-filter bug

The retrieval `$match` in `retrieve_trade_corpus_chunks`
(`orchestration/trade_panel/__init__.py:1278-1290`) already admits
`market_data` unconditionally and `paysh_live` via the vertical pre-filter.
The filter is correct. The corpus is simply empty for both:

- `market_data` — `scripts/market_data/ingest_market_data.py` (free Pyth +
  DefiLlama, no payment) was built but never run against the live corpus.
- `paysh_live` — needs a funded x402 buyer wallet; 0 chunks since 2026-05-14.
  `protocol_native` (S26-#14) was built to replace the empty paysh chunks.
- Evidence: the retrieval-eval artifact shows the entire raw ANN pool is
  `protocol_native` — zero `market_data`/`paysh_live`.

## Three options

| # | Option | Blast radius | Effort |
|---|---|---|---|
| 1 | Run `ingest_market_data.py` (free; kamino/drift/jupiter/sanctum/marinade, **no jito**) | corpus write only, no code | low — but marginal lift: no slate quota, competes with protocol_native, which already carries the figures |
| 2 | Rewrite each `must_not_hallucinate` to demand only `protocol_native` | `tests/eval/suites/defi_trade_rubric_suite.json` only — never read by production code, pure judge ground-truth | low — ~10 string edits |
| 3 | Relax the judge so an ungrounded figure traceable to any retrievable kind isn't penalized | `score_defi_trade_rubric.py` `_JUDGE_SYSTEM` | low — but weakens the hallucination signal globally |

## Recommendation — Option 2

The #113/#114 traces proved the flagged figures (`$173M`, `8.8785e-06`,
`26.12%`) all already live in `protocol_native` chunks the panel received.
Demanding `market_data`/`paysh_live` specifically is a fixture-spec bug, not a
real grounding requirement. Option 2 makes the rubric demand what the corpus
can supply, is contained to one test file, and is honest. Pair it with a
deferred Option 1 (`ingest_market_data.py`) later as genuine corpus
enrichment — not as a gate-fix.

## Scope of distortion

- **Distorts:** `hallucination_score` directly — ~5 fixtures pinned at 0.0
  deterministically, dragging the N=50 aggregate to 0.34 against the 0.30 bar.
- **Does NOT distort:** `provider_kind_coverage` (reads `required_provider_kinds`
  = protocol_native + canon only) or `citation_relevance` (judged over
  `evidence_citations`, not contract-coupled to D1).

## Sequencing — D1 before the re-measure

D1 must be resolved **before** the N≥50 re-measure. With the broken clause,
any S37 panel-grounding fix is judged against an unsatisfiable rule — the
result is uninformative. Option 2 is a ~10-line fixture edit with zero code
blast radius; it lands before the founder-authorized paid re-measure.
