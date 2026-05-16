# S29-#32 — canon re-ingest + chunk-quality invariant handoff

**Date:** 2026-05-13
**Branch:** `s29/canon-reingest` (cut from `s28/rebaseline-thresholds`)
**Status:** Passes 1-3 landed as durable code. Passes 4-6 require live
infrastructure access (Mongo Atlas, embedding API keys, PDF downloads,
rubric eval spend) and are staged for founder execution.

---

## Founder principle re-stated

> Re-ingestion is cheap and iterative; garbage chunks are the real
> liability. Build the quality gate first, then re-ingest with the gate
> active.

The quality gate is the durable artifact. Pass 1 + Pass 3 close the
class of failure that produced 1,674 binary Berkshire chunks. Whether
those chunks get purged today or next week, the gate makes recurrence
structurally impossible at the insert boundary AND at the PDF-extraction
boundary.

---

## Pass 1 — chunk-quality invariant (DONE, committed)

**Commit:** `feat(s29-#32): chunk_quality invariant + insert_chunks_mongo gate`

- New module: `packages/gecko-core/src/gecko_core/ingestion/chunk_quality.py`
  - `ChunkQualityVerdict` frozen dataclass
  - `assess_chunk_quality(text, *, min_chars=200, max_repeat_ratio=0.50,
    min_printable_ratio=0.95) -> verdict`
  - Five reason classes: `ok` / `too_short` / `binary` / `repeated_chars`
    / `empty_json`. `duplicate` reserved for the audit script's counter
    stability.
- `insert_chunks_mongo` filters with the gate pre-write. Failed chunks
  log `mongo.insert_chunks.quality_skip` and are dropped from the batch.
  The batch as a whole is NOT failed (one binary page in a 200-page PDF
  should not waste the re-ingest).
- `quality_skips` counter surfaces via the existing structured-log
  pattern (alongside `pioneer_call`) — preserves the documented `int`
  return contract.
- 15 unit tests in `packages/gecko-core/tests/ingestion/test_chunk_quality.py`.

**Per CLAUDE.md "Single source of truth": `assess_chunk_quality` is THE
quality gate. No other quality check should be added elsewhere — extend
this module.**

## Pass 2 — audit script (DONE, committed)

**Commit:** `feat(s29-#32): audit_existing_chunks.py — report Mongo garbage`

- `scripts/data/audit_existing_chunks.py`
- Read-only. Iterates all chunks, scores each with `assess_chunk_quality`,
  emits per-(`provider_kind`, `reason`) breakdown + top-N samples per
  reason for inspection.
- Run with:
  ```bash
  set -a; source .env; set +a
  uv run python scripts/data/audit_existing_chunks.py --top-samples 10 \
      --out reports/2026-05-13-chunk-audit.json
  ```

**Founder MUST run this before authorizing any purge.** The script is
the Pattern E reachability probe — run it before AND after every corpus
mutation.

## Pass 3 — PDF extractor hardening (DONE, committed)

**Commit:** `feat(s29-#32): pdf extractor rejects binary-output parsers`

- `packages/gecko-core/src/gecko_core/sources/pdf.py` — the fallback
  chain now rejects parser output with printable-ratio < 0.95. A binary
  "success" falls through to the next parser instead of corrupting the
  corpus.
- This is the actual root-cause fix for the 1,674 binary Berkshire
  chunks. The pre-S29 chain accepted any non-empty parser return; one
  of pdfium/pdfminer/pypdf was returning `\x00\x01`-class garbage on
  Buffett's older scanned PDFs and the chunker happily embedded it.
- Test added: `test_binary_output_from_first_parser_falls_through`.

**`pdfplumber` deliberately NOT added.** The 3-parser fallback already
covers the cases pdfplumber would handle; adding a 4th parser without
measuring the marginal benefit is yak-shaving. If post-re-ingest the
audit still shows binary chunks for a specific URL set, that's the
signal to add pdfplumber — not before.

## Pass 4 — Berkshire re-ingest (DEFERRED — founder executes)

The ingest script already exists at
`scripts/canon/ingest_berkshire.py`. With Pass 1 + Pass 3 in place, a
re-run should now produce zero binary chunks. Procedure:

1. **Audit first** (Pass 2): confirm the 1,674 binary chunks the
   diagnostic flagged are still present and tagged `canon_berkshire`.
   Save the JSON report.
2. **Founder sign-off on purge.** Show the audit. If founder confirms
   delete:
   ```python
   # After verifying the audit shows binary in 1500+ of 1674 chunks:
   await chunks_collection().delete_many({
       "provider_kind": "canon_berkshire",
       # Optional safety: only delete chunks that FAIL the quality gate.
       # Computed client-side; see scripts/data/audit_existing_chunks.py
       # for the iteration shape.
   })
   ```
3. **Re-ingest:**
   ```bash
   set -a; source .env; set +a
   uv run python scripts/canon/ingest_berkshire.py
   ```
4. **Verify:** re-run audit. Expect `canon_berkshire.binary = 0`,
   `canon_berkshire.ok ≥ 500`. Per Pass 1 gate, `quality_skips` in the
   ingest logs should be tiny — non-zero only on truly scanned image
   pages.

**Hard stop:** if `quality_skips_total` is more than 10% of inbound
chunks, the extractor is still producing garbage. Stop the ingest and
re-diagnose before continuing.

## Pass 5 — mauboussin + macro initial ingest (DEFERRED — founder executes)

**`canon_mauboussin`** target 50-100 chunks:
- Source URL list: extend `packages/gecko-core/src/gecko_core/sources/canon_mauboussin.py`
  (currently a stub if it exists; otherwise mirror the `canon_marks.py`
  shape).
- Suggested PDFs: Morgan Stanley Counterpoint Global papers,
  Mauboussin's NYU Stern PDFs, his published posts.
- Add `scripts/canon/ingest_mauboussin.py` following the
  `ingest_berkshire.py` template (already in repo). Pass
  `provider_kind="canon_mauboussin"`, `category="investment_signals"`,
  `vertical="dex"`, `protocol=[]` (Pattern F — canon is cross-cutting).

**`canon_macro`** target 50-100 chunks:
- Source URL list: create `packages/gecko-core/src/gecko_core/sources/canon_macro.py`
  with FRB working papers, BIS quarterly reviews, IMF working papers.
- Add `scripts/canon/ingest_macro.py` analogous to above. Same
  category/vertical/protocol shape.

The two `ProviderKind` values (`canon_mauboussin`, `canon_macro`) ALREADY
exist in `packages/gecko-core/src/gecko_core/sources/types.py`. No
schema change needed — Pattern A is intact.

## Pass 6 — Jito MEV-tip targeted ingest (DEFERRED — founder executes)

The existing 344 `protocol_native` chunks tagged `jito` are
staking-endpoint content — wrong for the MEV-tip fixture but still
useful for staking questions. Strategy: **add, don't replace**.

- Source URLs: `https://docs.jito.wtf/` (Block Engine + tip distribution
  docs), `https://kobe.mainnet.jito.network/` (MEV stats API), Jito
  Foundation blog posts on MEV strategy.
- Use existing `scripts/protocol_native/ingest_protocol_native.py`
  pattern — extend its source list with the MEV-specific URLs.
- Chunk-doc shape:
  - `provider_kind="protocol_native"`
  - `protocol=["jito"]`
  - `metadata.subkind="mev_tip_data"` — additive metadata under Pattern A
    (no Literal change; subkind is free-form string within the metadata
    sub-doc).
- Target: ≥30 substantive MEV-specific chunks. With the Pass 1 gate
  active, garbage from the Kobe API (rate-limit error pages,
  `{"data":[]}` stubs) is automatically rejected.

## Pass 7 — rubric N=10 validation (BLOCKED — structural issue)

**The rubric harness cannot validate retrieval-layer changes against
live Mongo.** Per `memory/feedback_eval_harness_rag_gap`:

> Eval harness uses canned `rag_context`. `runner.py --live` bypasses
> `rag_query`; any retrieval-layer A/B at the runner level is a
> structural no-op.

Running rubric N=10 after the re-ingest would not actually exercise
the new corpus content — it would re-score against canned fixtures.

**Correct validation path:**
1. Direct retrieval probe (Pattern E test) — call
   `retrieve_trade_corpus_chunks` with the jito-MEV question and assert
   ≥1 chunk has `metadata.subkind="mev_tip_data"`.
2. End-to-end live API smoke against the deployed `gecko_trade_research`
   tool, with the jito-MEV question, citation count > 0 from the new
   `protocol_native` chunks.
3. Rubric only after #1 and #2 pass — and only with a harness fix that
   makes `--live` actually call `rag_query` on the real Mongo.

Budget for the smoke + rubric remains capped at $6.50; the smoke is
~$0.50 stub-mode and the rubric is the remainder.

---

## Open issues

- **Rubric harness fix is its own ticket.** The structural rag_context
  gap should be filed as a follow-up if not already tracked.
- **No `pdfplumber` added.** If post-re-ingest audit shows non-trivial
  remaining binary chunks for `canon_berkshire`, file a follow-up to
  add pdfplumber as a 4th parser with a measured A/B.
- **No `canon_macro` URL list yet.** Pass 5 includes a stub plan but
  the actual URL list lives in source files that need to be authored.
  Defer to founder + product-manager to curate.

## Report-back template (for founder after running Passes 4-6)

```
(1) Total new chunks per provider_kind:
    canon_berkshire (post-purge re-ingest): ___
    canon_mauboussin: ___
    canon_macro: ___
    protocol_native (jito MEV, additive): ___

(2) Audit findings — total garbage across Mongo (from Pass 2 report):
    by_reason_global:
      too_short:    ___
      binary:       ___    (canon_berkshire pre-purge: 1,674 expected)
      repeated_chars: ___
      empty_json:   ___    (paysh_live / bazaar_live stubs)
    Largest garbage class: ___

(3) jito-MEV citRel before/after: ___ → ___

(4) Rubric N=10 delta vs V1-gate baseline: hall_score ___ → ___,
    citRel ___ → ___. (BLOCKED until harness fix; see Pass 7.)

(5) Total spend: $___

(6) Largest "garbage class" finding for founder:
    ___
```

---

## Files touched on this branch

- `packages/gecko-core/src/gecko_core/ingestion/chunk_quality.py` (new)
- `packages/gecko-core/src/gecko_core/db/mongo_chunks.py` (modified — gate wired)
- `packages/gecko-core/src/gecko_core/sources/pdf.py` (modified — binary
  rejection)
- `packages/gecko-core/tests/ingestion/test_chunk_quality.py` (new)
- `packages/gecko-core/tests/ingestion/test_pdf_fallback.py` (modified)
- `packages/gecko-core/tests/db/test_chunk_protocol_content_kind.py` (modified)
- `packages/gecko-core/tests/db/test_insert_chunks_freshness.py` (modified)
- `scripts/data/audit_existing_chunks.py` (new)
- `scripts/data/__init__.py` (new)
- `docs/strategy/2026-05-14-s29-32-canon-handoff.md` (this file)
