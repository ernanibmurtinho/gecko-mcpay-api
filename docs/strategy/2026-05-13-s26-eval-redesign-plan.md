# S26 — Eval-First Test Strategy Redesign

**Date:** 2026-05-13
**Owner:** ai-ml-engineer
**Supersedes:** the implicit "pytest is the quality gate" framing carried through S20-S25.
**Companion docs (referenced but not gated on):**
- `docs/strategy/2026-05-13-s24-closeout-synthesis.md` — defer-rate 0.9, brier 0.16, honest-state framing
- `docs/strategy/2026-05-13-s25-retrieval-handoff.md` — retrieval bottleneck is data, not logic *(to be filed by data-engineer; not yet on disk at time of writing)*
- `docs/strategy/2026-05-13-s25-test-triage.md` — 25-test audit list *(to be filed; see "Annotation queued" section below)*

---

## Founder reframe (paraphrased)

> We don't need heavy unit tests. We need eval-style tests that check accuracy — is the model and agents answering back? Is the context not reaching max and returning null? Are the answers grounded? Should we load more data, or are there still errors to fix on our accuracy?

The load-bearing quality signal is the eval suite, not pytest. Pytest stays — for pure helpers (Pattern A drift tests, retrieval boost math, citation-builder shape, store-adapter contracts). It stops being the merge gate for anything that touches **panel behavior, retrieval quality, or verdict shape**.

Five operating questions, each maps to a check in the eval tiers below:

1. **"Did the agent answer back?"** — non-null, well-formed `TradePanelVerdict`. No timeout, no swallowed API error, no `verdict=None`. Deterministic.
2. **"Did context overflow?"** — `_format_chunks` silently truncates at `_RAG_CONTEXT_CHAR_CAP = 8000` chars today (line 133, `trade_panel/__init__.py`). Token count of full opening prompt under the model's hard limit. Deterministic.
3. **"Are the citations grounded?"** — every `Citation.chunk_id` in the verdict envelope matches an `id` of a chunk that was in the retrieval input. No phantom cites. Deterministic.
4. **"Are the answers making sense?"** — existing LLM-as-judge rubric (`score_defi_trade_rubric.py`). Keep as-is, run on cadence.
5. **"Should we load more data, or is there still an accuracy bug?"** — corpus-presence probe. For each fixture, assert that *some* chunk in Mongo carries the fixture's `must_cite_provider_kinds` for the protocol. If absent → ingest gap; if present but not retrieved → retrieval bug.

---

## Three-tier suite design

| Tier | Cadence | N | Cost | Wall | Dims | Owns |
|---|---|---|---|---|---|---|
| **1. Canary** | every commit (`make test-canary`) | 1 | ~$0.05 | <30s | 1, 2, 3 | ai-ml-engineer; software-engineer enforces via CI |
| **2. Sprint baseline** | weekly + pre-PR-merge on retrieval/panel changes | 3 | ~$0.50 | <5min | 1–5 | ai-ml-engineer |
| **3. Sprint-end full** | sprint close-out, pre-V1 ship, pre-pricing change | 10 | ~$3 | <30min | 1–5 + outcome eval (brier) | ai-ml-engineer; staff-engineer signs off |

Costs are upper-bound estimates: Tier 1 = 1 panel run (`gpt-4o-mini` basic ≈ $0.04) + zero judge calls. Tier 2 = 3 panel runs + 3 Sonnet 4.6 judge calls (≈ $0.10 each). Tier 3 = 10 panel runs (basic) + 10 judge calls + the outcome eval (no LLM, deterministic).

### Tier 1 — Canary (`tests/eval/scripts/canary_eval.py`)

**Input:** `tests/eval/suites/canary_suite.json` (N=1). Selected fixture: `kamino-2024Q3-jlpusdc-entry` (the cleanest from `defi_trade_rubric_suite.json` — expected verdict set is wide, must_cite list rich, dissent topics concrete).

**Production-path guarantee:** calls `run_trade_panel_with_retrieval` directly. This is the same entry point the MCP tool + REST endpoint use (`packages/gecko-core/src/gecko_core/orchestration/trade_panel/__init__.py:851`). Per `feedback_eval_harness_rag_gap`, no canned-context shortcut, no retrieval bypass.

**Assertions (deterministic, no LLM judge):**

1. **D1 — "did the agent answer back?"**
   - `verdict_obj is not None` and is a `TradePanelVerdict`
   - `verdict_obj.verdict in {"act", "pass", "defer"}`
   - `len(verdict_obj.turns) == 7` (one per persona)
   - `verdict_obj.confidence` is a float in `[0.0, 1.0]`
   - No exception bubbled up from `run_trade_panel_with_retrieval`

2. **D2 — "did context overflow?"**
   - Measure char-len of `_format_chunks(retrieved_chunks)` BEFORE the cap is applied (read-through helper)
   - Estimate token count via `len(prompt) // 4` (good-enough heuristic at the canary tier; switch to `tiktoken` only if the assertion ever fires spuriously)
   - Assert: `n_chunks_input == n_chunks_rendered` (i.e. no chunk was dropped from the rendered block due to char-cap truncation)
   - Assert: estimated_tokens < 100_000 (well under `gpt-4o-mini`'s 128k)
   - Soft-warn (do not fail) if estimated_tokens > 50_000 — early signal that we're approaching the cap before any model crash

3. **D3 — "are the citations grounded?"**
   - Build the set `S = {chunk.id for chunk in retrieved_chunks}`
   - For each `cite in verdict_obj.citations`: assert `cite.chunk_id in S`
   - For each inline `[N]` marker found via regex in `turn.content` for each turn: assert `N <= len(retrieved_chunks)` (the index is in-range)
   - No assertion on cite *count* (zero cites is a separate failure mode handled in Tier 2)

**Exit code:** 0 on all-pass; non-zero on any deterministic check failure. Run output is appended to `tests/eval/live_runs/<date>-canary.json` (one row).

**Where it runs:**
- `make test-canary` locally before pushing any retrieval / panel / prompt change
- CI gate on PRs that touch `packages/gecko-core/src/gecko_core/orchestration/trade_panel/**` or `packages/gecko-core/src/gecko_core/rag/**` (wire-up tracked separately; not in this commit)

**Mocked Tier-1 failure transcript (worked example):**

```
$ make test-canary
S26 canary | n=1 | suite=canary_suite.json
  [kamino-2024Q3-jlpusdc-entry] protocol=kamino ...
    D1 (answered_back): PASS (verdict=defer, confidence=0.30, 7 turns)
    D2 (context_overflow):
      retrieved_chunks=15, rendered_block_chars=11403, char_cap=8000
      => FAIL: 12 of 15 chunks dropped by silent truncation at _format_chunks
      estimated_tokens=2851 (well under 100k; failure is the cap, not the model)
    D3 (citations_grounded):
      panel citations=3, cite.chunk_id all in retrieval set: PASS
      inline [N] markers: [1, 2, 4, 7, 18] — marker [18] exceeds n_chunks=15: FAIL
  RESULT: FAIL on D2 + D3
  TRIAGE:
    - D2 → DATA-INGEST / CHUNK-SHAPE failure. The chunks ARE retrieved but
      11 KB of them never reach the panel. File ticket equivalent to #14
      style: either tighten chunk sizing at ingest, or lift _RAG_CONTEXT_CHAR_CAP,
      or add a per-chunk-tier budget. Hand to data-engineer to confirm chunk
      bloat is real and not a single-row outlier.
    - D3 → PROMPT / MODEL failure. The model invented marker [18] from a
      truncated context. Likely downstream of D2 (cap fires → personas see
      less than they think they cited). Re-test after D2 fixed.
exit 1
```

That transcript is the canary doing its job: separating "the data was wrong" (D2 → data-engineer) from "the model fabricated a cite index" (D3 → ai-ml-engineer). Both are caught in <30 seconds. Before this tier existed, this failure mode required a Tier-3 rubric run to surface as `hallucination_score=0` with no clue where the bug actually lived.

**Real Tier-1 transcript — 2026-05-12 first run on `s26/eval-redesign` (BEFORE):**

```
$ make test-canary
S26 canary | n=1 | suite=canary_suite.json
  [kamino-2024Q3-jlpusdc-entry] protocol=kamino
    D1 (answered_back)    : PASS  (verdict=defer, confidence=0.7, 7 turns)
    D2 (context_overflow) : FAIL
      fail: chunk dropout: 10 of 15 chunks not reachable in rendered block
            (cap=8000 chars, block_len=8032)
    D3 (citations_ground) : PASS  (n_panel_cites=15, n_phantom_cites=0,
                                   n_inline_markers_out_of_range=0)
exit 1
```

Caught the silent truncation: the cap was 8000 chars, the rendered block was 8032 chars, chunks 6-15 vanished into the truncation tail. D3 falsely PASSED because the legacy check only verified `chunk_id` validity and inline-marker in-range — it didn't notice that `build_citations_from_chunks(chunks)` (line ~913) was decorating the verdict with citations the personas never actually wrote.

**Real Tier-1 transcript — 2026-05-12 third run on `s26/cap-fix` (AFTER):**

```
$ make test-canary
S26 canary | n=1 | suite=canary_suite.json
  [kamino-2024Q3-jlpusdc-entry] protocol=kamino
    D1 (answered_back)    : PASS  (verdict=defer, confidence=0.7, 7 turns)
    D2 (context_overflow) : PASS  (n_chunks_rendered=15 of 15,
                                   block_chars=29676, cap=60000,
                                   estimated_tokens=7530)
    D3 (citations_ground) : FAIL
      fail: phantom citations: 10 of 15 — markers [4][5][6][7][8][9][10][11] ...
            never appear in any persona content
      details: persona_markers_seen=[1, 2, 3, 14, 15]
               phantom_markers=[4, 5, 6, 7, 8, 9, 10, 11, 12, 13]
exit 1
```

D2 now PASSes — the cap bump from 8000 → 60000 puts the full 29.7k-char rendered block well inside the budget and well inside `gpt-4o-mini`'s 128k context (estimated ~7.5k prompt tokens vs. 128k limit, >100k headroom). D3 now FAILs, but **honestly** — the strengthened phantom-marker check surfaces that `build_citations_from_chunks` is post-hoc-decorating the envelope with 10 of 15 markers the personas never cited. The personas only wrote `[1][2][3][14][15]`; the other 10 citations are caller-visible grounding without model-visible content.

That second failure is a **separate panel-prompt bug** filed as a follow-up ticket: under v0.1 prompts the 7 personas don't reason across all retrieved chunks, just the first few + the tail. Cap-fix removed Bug 1 (truncation); the strengthened D3 made Bug 2 visible. Fixing the prompt to actually iterate over all retrieved chunks is a separate ai-ml-engineer ticket and out of scope for `s26/cap-fix`.

Artifact: `tests/eval/live_runs/2026-05-13-canary-postfix.json`.

### Tier 2 — Sprint baseline

**Input:** N=3 fixtures across distinct protocols. Re-uses the first three rows of `defi_trade_rubric_suite.json` (kamino-jlpusdc, kamino-sol-leverage, drift-sol-perp).

**Assertions:** D1 + D2 + D3 inherited from canary (run in-process — share the canary scoring module), PLUS:

4. **D4 — "are the answers making sense?"** — `score_defi_trade_rubric.py` already implements this. Keep its 6-dimension rubric + Sonnet 4.6 judge as-is. The Tier 2 wrapper invokes it with `--limit 3`.

5. **D5 — "should we load more data, or is there still an accuracy bug?"** — corpus-presence probe (NEW; not yet implemented in this commit). For each fixture:
   - Query the production Mongo `chunks` collection (same connection the live retrieval uses) for `{"protocol": fixture.protocol, "provider_kind": {"$in": fixture.must_cite_provider_kinds}}`
   - If the result is empty → **ingest gap**. Report as `corpus_missing` per provider_kind. Do NOT fail the fixture — this is diagnostic, not a gate.
   - If the result is non-empty but `provider_kind_coverage` (D4 axis) was zero → **retrieval bug**. Report as `corpus_present_but_unretrieved`. Cross-reference with data-engineer's retrieval-filter audit.

**TODO scaffold for D5 — not built in this commit. File-path target:** `tests/eval/scripts/corpus_presence_probe.py` (new), helper-imported by both the Tier 2 runner and a future direct CLI invocation (`uv run python -m tests.eval.scripts.corpus_presence_probe --fixture <id>`). Must use `gecko_core.db.mongo.get_chunks_collection()` (or the moral equivalent at S26 time) — not a side-channel client — so the probe sees what production sees.

**Gate:** when a #14-style data re-ingest is proposed, Tier 2 must run **before** and **after**. If D5 transitions from `corpus_missing` to `corpus_present` and citation_relevance lifts by ≥0.1 in mean, the re-ingest is validated. If D5 flips but citation_relevance does not, the failure is retrieval-side (data-engineer hands off back to ai-ml-engineer).

### Tier 3 — Sprint-end full

**Input:** N=10. `defi_trade_rubric_suite.json` in full (10 fixtures already) + the 10-fixture outcome suite (`defi_trade_suite.json`).

**Assertions:** D1–D5 from above + the outcome eval's brier + verdict-distribution + drawdown-avoid metrics (already implemented in `score_defi_trade_suite.py`).

**Gate:** required before:
- V1 ship
- Any pricing-tier change (basic/pro routing, model swap)
- Any prompt change to the panel personas or the coordinator's closing line
- Any retrieval-scoring change (e.g. tightening `$or` admittance to `$and` on protocol field)

**Reporting:** combines the two existing `*.json` outputs in `tests/eval/live_runs/` into a single sprint-close report (TODO: thin wrapper, not in this commit).

---

## What's NOT in this commit

- Tier 2 wrapper script (`tier2_eval.py`)
- Tier 3 wrapper script (`tier3_eval.py`)
- Corpus-presence probe (`corpus_presence_probe.py`) — D5
- CI wire-up for `make test-canary` on PRs that touch panel/retrieval paths
- The `tiktoken`-based exact token count (vs. `len // 4` heuristic) — switch only if heuristic fails spuriously

These ship in S26 Wave 2 / Wave 3 as separate commits, each independently testable. This commit ships the plan + the canary scaffold + one validated canary run.

---

## Re-test triage — what S25 left open

The S25 test-rebuild work (15 REWRITE candidates, 5 light siblings already drafted) was scoped under the prior framing where pytest was the merge gate. Under the new framing, many of those rewrites are testing behavior the eval tiers now own:

| S25 rewrite candidate (representative) | Was covering | Owned by under S26 framing |
|---|---|---|
| `test_trade_panel_returns_verdict_shape` | D1 (answered back) | Tier 1 canary |
| `test_format_chunks_truncates_at_cap` | D2 (context overflow) | Tier 1 canary |
| `test_citations_match_retrieved_chunks` | D3 (citations grounded) | Tier 1 canary |
| `test_panel_handles_empty_corpus` | edge case of D1 + D2 | Tier 1 canary (add fixture) |
| `test_rubric_judge_returns_valid_scores` | judge wiring | Tier 2 (already covered) |

**Recommendation: 5-of-15 of the S25 rewrite candidates are DELETE under this framing.** They test surface that the canary now covers, more honestly, against the production code path. Keeping them duplicates work and adds maintenance burden.

The 4 KEEP tests from the S25 triage are still keepers under S26: Pattern A drift, retrieval-boost math, citation-builder pydantic shape, store-adapter contract. Those are pure helpers; pytest is the right tool. The 1 DELETE candidate from S25 stays deleted.

**Annotation queued:** when `docs/strategy/2026-05-13-s25-test-triage.md` lands on disk, prepend the top-of-file note: *"5 of the 15 REWRITE candidates are DELETE under the S26 eval-first framing (see `2026-05-13-s26-eval-redesign-plan.md` §Re-test triage). The 4 KEEP tests stand."* This commit attempts that prepend; if the file is absent at commit time, the annotation is deferred to S26 W2 and tracked here.

---

## Cost & cadence guardrails

- **Tier 1 budget per sprint:** ≤ $1.50 (≤ 30 commits at $0.05 each — generous). If a sprint exceeds this, the canary fixture is too expensive; swap for a cheaper question (basic tier already; reduce N=1 cite count or chunk size).
- **Tier 2 budget per sprint:** ≤ $5 (weekly + 4-5 PR-gated runs).
- **Tier 3 budget per sprint:** ≤ $10 (3 runs: open-sprint baseline, mid-sprint check, close-out gate).

Total eval budget per sprint: ≤ $16.50. Compare to a single live pricing experiment ($50-200). The eval suite is cheap insurance.

---

## Success criteria for S26 (this work-stream)

1. Canary scaffold runs green on current `main`, exits 0 with all three deterministic dimensions passing (or, if it fails, the failure points at a real bug we then file).
2. Documentation of the three-tier design + the worked transcript example exists and is referenced from `CLAUDE.md`'s self-learning section under "Recurring patterns" (queued for S26 W2 — once D5 lands and the full loop is validated end-to-end).
3. The 5 redundant S25 rewrite candidates are deleted, not rewritten (recovers ~half a day of S25 work that we no longer owe).
4. Data-engineer can independently invoke the corpus-presence probe (D5) to validate a re-ingest, without coordinating with ai-ml-engineer for every check.
