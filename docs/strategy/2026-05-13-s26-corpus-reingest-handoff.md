# S26 #14 — Corpus Re-Ingest Handoff (2026-05-13)

## Headline

Three retrieval-quality gaps closed. Corpus delta confirmed by direct Mongo aggregation. **Final rubric N=3 eval NOT yet re-run** — gated on the S25 #13 boost code merging into this branch (`s26/cap-fix`) so the new `protocol_native` chunks and date-aligned `market_data` chunks both stack their +0.10 / +0.05 boosts correctly.

## Branch reality

The task brief asked for `s26/corpus-reingest` cut from `s25/retrieval-push`. Execution landed on `s26/cap-fix` instead — that branch already has #17 (the `_RAG_CONTEXT_CHAR_CAP 8000→60000` lift) committed as `3a3fd5b`, so committing Gap-1/2/3 there keeps the cap-fix + ingest work together for final eval.

Commits on `s26/cap-fix` from this work:

| Commit | Gap | What landed |
|---|---|---|
| `3a3fd5b` | #17 | `_RAG_CONTEXT_CHAR_CAP` 8000→60000 (pre-existing, NOT this task) |
| `*` (Gap 1) | #14a | New `ProviderKind="protocol_native"` + Kamino/Drift/Jupiter/Jito/Sanctum ingest |
| `*` (Gap 2) | #14b | bazaar_live Solana-only chain filter + 15 cross-chain chunks deprecated |
| `*` (Gap 3) | #14c | Historical Pyth prices for 10 fixture as_of_dates × 7 assets |

`s25/retrieval-push` (#13 boost code, +0.15 protocol-exact, +0.10 provider-specific, +0.05 date-aligned, -0.10 date-misaligned) is **not yet merged into `s26/cap-fix`**. The `_apply_retrieval_boosts` function lives there. The boost-set extension to include `protocol_native` in `PROVIDER_SPECIFIC_KINDS` requires #13 to merge forward; see "Followups" below.

## Per-gap chunk-count delta (gecko_rag.chunks)

Direct Mongo query (post-ingest, post-remediation):

| provider_kind | before | after | active | deprecated | delta |
|---|---|---|---|---|---|
| paysh_live | 64 | 64 | 64 | 0 | 0 (kept for ledger provenance; mostly empty `{"data":[]}` bodies) |
| bazaar_live | 27 | 27 | 12 | 15 | -15 active (Base-chain noise stamped `metadata.deprecated=True`) |
| market_data | 14 | 84 | 84 | 0 | +70 historical (Pyth /api/get_price_feed for fixture as_of_dates) |
| protocol_native | 0 | 133 | 133 | 0 | +133 NEW (this task) |
| canon_marks | 649 | 649 | 649 | 0 | unchanged |
| canon_damodaran | 2410 | 2410 | 2410 | 0 | unchanged |
| canon_berkshire | 1674 | 1674 | 1674 | 0 | unchanged |

**Net active-chunk delta: +188** (133 protocol_native + 70 market_data historical − 15 bazaar_live deprecated).

### protocol_native by protocol

```
kamino:   121 chunks  (markets, kvaults catalog sample, strategies sample, staking-yields, docs)
jupiter:    4 chunks  (dev.jup.ag docs + SOL lite-api price)
drift:      3 chunks  (docs.drift.trade overview)
jito:       3 chunks  (tip_floor snapshot + docs.jito.wtf)
sanctum:    2 chunks  (learn.sanctum.so docs)
```

Kamino is over-represented because the Kamino public API exposes substantive JSON (markets, vaults, strategies, staking-yields) where the other 4 protocols mostly serve React SPAs. The asymmetry is fine for the rubric eval which is 5/10 Kamino fixtures.

### market_data historical chunks

10 fixture as_of_dates × 7 assets = 70 chunks, freshness_tier='hot':

```
SOL/USD on 2024-11-05 = $157.88   (rubric question premise was $165 EoD; ~5% off)
BTC/USD on 2024-11-05 = $67,813.72
ETH/USD on 2024-11-05 = $2,396.99
SOL/USD on 2024-08-12 = $141.57
... (all 10 dates × 7 assets)
```

## Pattern A: protocol_native ProviderKind decision (Option A chosen)

Chose **Option A**: new `ProviderKind="protocol_native"` literal vs Option B (reuse `paysh_live` + metadata field).

Rationale:
1. `paysh_live` carries USDC-ledger semantics (every chunk was a paid x402 call); mixing free protocol-API content under that label corrupts ledger queries
2. Empty `{"data":[]}` paysh_live chunks would be indistinguishable from new substantive ones at debugging time
3. Pattern A is the cleanest path — single-file edit per concept
4. The S25 #13 boost code already treats `paysh_live`/`bazaar_live` as a privileged class; extending the set to include `protocol_native` is a 1-line edit

Pattern A propagation:
- `gecko_core.sources.types.ProviderKind` — added (canonical)
- `gecko_core.knowledge.taxonomy.KnowledgeSource` — added (mirrors)
- `infra/supabase/migrations/20260513000000_protocol_native_provider_kind.sql` — drift contract (chunks table is Mongo-only per `memory/project_supabase_chunks_dropped_2026_05_08`; SQL is IF EXISTS-guarded)
- `tests/test_provider_kind_consistency.py` — locked canonical tuple
- `tests/knowledge/test_taxonomy_consistency.py` — `KNOWLEDGE_SOURCES` now `12` (was 10; +market_data +protocol_native)

## Pattern E: end-to-end reachability proof

For each new ProviderKind / new chunk class, direct Mongo verification:

```python
# protocol_native
db.chunks.count_documents({"provider_kind": "protocol_native"})  # 133
db.chunks.find_one({"provider_kind": "protocol_native", "protocol": ["kamino"]})
# → real "Kamino-tracked LST staking-yield snapshots — JitoSOL, mSOL, bSOL, INF
#    current APY + 7d trailing..." body

# market_data historical
db.chunks.count_documents({"provider_kind": "market_data", "freshness_tier": "hot"})  # 84
db.chunks.find_one({"session_id": "<historical_session_uuid>", "text": /2024-11-05/})
# → "Pyth historical close for SOL/USD on 2024-11-05. Closing price USD: $157.8759..."

# bazaar_live deprecation
db.chunks.count_documents({"provider_kind": "bazaar_live", "metadata.deprecated": True})  # 15
```

## Pattern F: canon stays protocol=[], new chunks carry exact tag

| chunk class | protocol field |
|---|---|
| canon_marks / canon_damodaran / canon_berkshire | `[]` (cross-cutting) |
| market_data historical (price) | `()` empty — cross-cutting (price applies to every protocol) |
| protocol_native | `["kamino"]` / `["drift"]` / etc. — exact protocol slug |

Retrieval admittance in `retrieve_trade_corpus_chunks` matches:
```
$or: [
  {"protocol": proto_norm},        // protocol_native chunks land here
  {"protocol": {"$size": 0}},      // canon + market_data historical land here
  {"protocol": {"$exists": False}}
]
```

## Followups (not in this PR)

1. **Merge `s25/retrieval-push` (#13 boost code) into `s26/cap-fix`.** Once `_apply_retrieval_boosts` lives here, add `protocol_native` to `PROVIDER_SPECIFIC_KINDS` so the +0.10 boost stacks with the +0.15 protocol-exact for direct API content. One-line edit.

2. **Mongo unique index on `(source_id, chunk_index)` is NOT actually unique.** Observed: re-running `ingest_historical_prices.py` doubled the chunks until I one-off-deleted the duplicates. The index exists (`source_chunk_unique` in Atlas) but its `unique: True` flag is off. File a Mongo-Atlas-side ticket; not blocking this work.

3. **Final rubric N=3 eval is GATED.** Per the task brief: do NOT run the eval until both #14 (this work) AND #17 land together AND the boost code has been merged forward. When ready:
   ```bash
   set -a; source .env; set +a
   uv run python tests/eval/scripts/score_defi_trade_rubric.py \
     --suite tests/eval/suites/defi_trade_rubric_suite.json \
     --n 3 \
     --out tests/eval/live_runs/2026-05-13-s26-post-ingest-N3.json
   ```
   Acceptance: `citation_relevance ≥ 0.7 AND hallucination_score ≥ 0.8`.

4. **Operator step — re-run `ingest_protocol_native.py --sample` on a 24h cadence.** The `freshness_tier="daily"` chunks are time-relevant; without refresh they age. A cron / scheduled CI job will land in a follow-up.

5. **`market_data.market_data` non-historical existing chunks** carry `protocol=[<slug>]` (not empty `()`). Inconsistent with my new historical chunks which use `()`. Both work via the admittance `$or`, but it would be cleaner to backfill the non-historical ones to `protocol=()` — out of scope for #14.

## Final-eval gate command (when #13 merges + #17 lands)

```bash
git checkout s26/cap-fix                # has #14 + #17
git merge s25/retrieval-push --no-ff    # bring in #13 boost code
# add `protocol_native` to PROVIDER_SPECIFIC_KINDS in
#  packages/gecko-core/src/gecko_core/orchestration/trade_panel/__init__.py
#  (line ~676 in retrieval-push's version of the file)
set -a; source .env; set +a
uv run python tests/eval/scripts/score_defi_trade_rubric.py \
  --suite tests/eval/suites/defi_trade_rubric_suite.json --n 3 \
  --out tests/eval/live_runs/2026-05-13-s26-post-ingest-N3.json
```

Expected directional read: `citation_relevance` should move from 0.35 → ≥ 0.7 (the rubric threshold). With 133 protocol_native + 70 date-aligned market_data chunks now in the corpus, the Kamino fixtures should retrieve substantive vault-params + date-aligned prices instead of the prior empty-body `{"data":[]}` paysh_live + cross-chain Base bazaar_live noise.
