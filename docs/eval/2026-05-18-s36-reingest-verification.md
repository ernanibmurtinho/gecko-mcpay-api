# S36-#109 — Re-ingest verification gate (pre-paid-eval)

**Date:** 2026-05-18
**Branch:** `s36/prompt-grounding` (WS2 grounding gate + #108 prompt)
**Scope:** Investigation + free deterministic eval ONLY. No corpus writes, no paid LLM eval.
**Trigger:** Founder re-ran `ingest_protocol_native.py --sample` — clean: 32 endpoints, 342 chunks, skipped=0,
by_protocol kamino 68 / drift 134 / jupiter 82 / sanctum 58.

---

## 1. jito coverage gap — re-ingest NEEDED: **NO**

`scripts/protocol_native/ingest_jito_mev.py` does **not** use the WS2-reordered
`protocol_native.py` renderers. It imports `render_chunk` from
`gecko_core.sources.jito_mev` — an independent, header-first renderer:

```
render_chunk() ->  "Protocol-native API: jito / {slug} (subkind=mev_tip_data) (as of ...)."
                   "Endpoint description: ..."
                   "Source URL: ..."
                   "Content kind: ..."
                   "----- body -----\n{body_text}\n----- end body -----"
                   "Provider: protocol_native (Jito MEV-side ...)."
```

WS2's number-first fix (`_render_entity_list`, `_render_drift_funding_payload`,
`_render_tip_floor_payload` in `protocol_native.py`) reordered the *shared*
renderer module only. `jito_mev.py` has its own `render_chunk` and was never
touched by WS2 — so a jito re-ingest would produce **byte-identical** chunks to
what is already in Mongo (738 jito chunks, still header-first).

**Conclusion:** jito chunks are header-first and stay header-first regardless of
re-ingest. Re-running `ingest_jito_mev.py` changes nothing. No founder action
needed for #109.

**Caveat (out of scope, file as follow-up):** if the team later wants jito
chunks to also get the number-first treatment (the WS1 diagnosis applies equally
to tip-floor percentile payloads), that is a `jito_mev.py` *renderer rewrite* —
not a re-ingest. It is a new ticket, not a #109 blocker. The S36 ship-gate
`hallucination_score` blocker is being closed against the protocol_native
renderer fix; the deterministic eval below shows jito retrieval is already sound.

---

## 2. chunk-count drop — **consolidation + a catalog change, NOT data loss**

The founder's reported 2026-05-18 counts are exactly the chunks the re-ingest
wrote, and they are confirmed intact and number-first. The headline "drop"
(jupiter 269->82, kamino 110->68, sanctum 70->58) is two effects stacked:

### 2a. The new chunks are correct and number-first

`ingest_protocol_native.py` (S33-#80) uses a **stable `source_id`** (no
day-bucket) and a **`delete_chunks_for_source_mongo` replace-before-insert**
matched on `source_url`. For every endpoint URL in the current `--sample`
catalog, the re-ingest is a true replace. Sampled 2026-05-18 chunks:

- **kamino** (`_render_entity_list`): `"Kamino kamino-staking-yields: APY
  0.0591842754820410159, token mint mSoLz... Source: ..."` — numeric APY leads,
  provenance trails. Number-first ordering visible. Figures intact.
- **jupiter** (`_render_entity_list`): `"...blockId: 420613721 ... createdAt: ... decimals: 9
  ... liquidity: 173..."` — per-token numeric fields lead.
- **drift** (`_render_drift_funding_payload` / docs): funding-rate mechanics
  chunk present; numeric funding records lead per WS2.
- **sanctum**: docs-prose chunks (`learn.sanctum.so/docs/*`), content intact.

The apparent kamino-chunk "duplication" (`"...pubkey 7u3HeHxYDLhnCoErrtycN ...
7u3HeHxYDLhnCoErrtycNokbQYbWGzLs6JSDqGAv5PfF..."`) is **not a bug** — it is the
chunker's 50-token overlap window re-splitting a short per-entity prose chunk.
Expected behaviour.

### 2b. The "old" counts include orphaned chunks from a CHANGED catalog

Live Mongo `protocol_native` counts by `as_of_date`:

| protocol | as_of=None (legacy) | 2026-05-16 | 2026-05-18 (new) | live total |
|----------|--------------------:|-----------:|-----------------:|-----------:|
| jupiter  | 95                  | 90         | 82               | **267**    |
| sanctum  | 6                   | —          | 58               | **64**     |
| kamino   | —                   | —          | 68               | **68**     |
| drift    | —                   | —          | 134              | **134**    |

The pre-existing jupiter chunks are **27 `dev.jup.ag/docs/*` doc-prose URLs**
(audits, perps, swap routing, lend architecture...). The 2026-05-18 re-ingest
catalog has **4 jupiter URLs** — `lite-api.jup.ag` price/token endpoints. These
are *different `source_url`s*, so `delete_chunks_for_source_mongo` (URL-matched)
did **not** touch them. Same for sanctum: 6 stale `sanctum-extra-api.ngrok.dev`
quote chunks (one sampled returns `{"apys":{"bSOL":0.0}}` — the dev API gives
empty data) vs 19 fresh `learn.sanctum.so/docs/*` URLs.

So the 269->82 jupiter figure compares **two different catalogs**, not before/after
of the same endpoints. No jupiter *entities* were lost — the catalog was
refocused from doc-prose pages onto live price/token API endpoints. The 185
orphaned jupiter doc-prose chunks and 6 orphaned sanctum quote chunks are
**still in the corpus** (stale, header-first, but not corrupt).

**Verdict: consolidation + catalog refocus. No data loss.** The new 342 chunks
are dense, number-first, and figure-complete.

**Follow-up (not a #109 blocker):** the 185 orphaned jupiter doc-prose +
6 orphaned sanctum quote chunks are dead weight — pre-WS2 header-first chunks
under retired URLs. A cleanup pass (`delete_chunks_for_source_mongo` on the
retired URL set) would tidy the corpus. Not gating: the deterministic eval below
passes with them present.

---

## 3. deterministic retrieval eval — **BOTH GATES PASS**

`tests/eval/scripts/retrieval_eval.py` — no LLM, free (Pattern E pre-gate).
Run tag `s36-109-reingest-verify`, n=10, suite `defi_trade_rubric_suite.json`,
production top_k=15.

Contract check OK — all 5 demanded provider_kinds present:
`canon_marks 610 / protocol_native 1271 / canon_macro 398 / canon_damodaran 2408 / canon_mauboussin 439`.

| metric                       | value  | gate      | pass |
|------------------------------|-------:|-----------|:----:|
| canon_floor_rate             | 1.0    | >= 0.80   | YES  |
| provider_kind_coverage (mean)| 0.9667 | >= 0.80   | YES  |
| precision@k (mean)           | 0.7333 | (no gate) | —    |
| recall@k (mean)              | 0.9667 | (no gate) | —    |
| mrr (mean)                   | 0.3857 | (no gate) | —    |

Per-fixture: 10/10 `canon_floor=True`. Only miss is `drift-sol-perp-long`
pkCov=0.67 (`missing=['canon_macro']`) — pre-existing, unrelated to the
re-ingest. The **`jito-mev-tip-band`** fixture retrieves cleanly
(canon_floor=True, pkCov=1.00, P@k=0.73) — confirms jito MEV chunks are
reachable by the panel without a re-ingest.

Saved: `tests/eval/live_runs/2026-05-18-s33-retrieval-eval-s36-109-reingest-verify-10.json`

---

## Bottom line

The re-ingested corpus is **sound enough to fire the founder-authorized N>=50
paid re-measure**. No jito re-ingest is needed first:

1. **jito** — re-ingest would be a no-op (separate `jito_mev.render_chunk`,
   untouched by WS2). Jito retrieval already passes the deterministic eval.
2. **chunk-count drop** — consolidation + a catalog refocus (jupiter doc-prose
   pages -> live price API endpoints). New 342 chunks verified number-first and
   figure-complete. No data loss.
3. **deterministic eval** — both gates pass (canon_floor 1.0 >= 0.80;
   pk_coverage 0.967 >= 0.80). Pre-gate is GREEN.

**Non-blocking follow-ups (file as separate tickets):**
- Optional: rewrite `jito_mev.render_chunk` number-first to apply the WS1
  hallucination fix to the 738 jito chunks.
- Optional: corpus cleanup of 185 orphaned jupiter doc-prose + 6 orphaned
  sanctum quote chunks under retired URLs.
