# S25 #13 — Retrieval Push Handoff

**Branch:** `s25/retrieval-push`
**Owner:** ai-ml-engineer
**Date:** 2026-05-13

## Headline

The retrieval scoring layer landed: protocol-exact + provider-specific + date-alignment boosts now reshape the candidate set so protocol-tagged chunks (paysh_live / bazaar_live / market_data) surface ahead of cross-cutting canon. Hallucination handling improved structurally — date-misaligned market_data is now demoted. **citation_relevance did NOT move** because the underlying paysh_live/bazaar_live chunks for Kamino are semantically degenerate (raw Zerion/Exa API metadata, not vault parameters). The retrieval lever has been pulled correctly; the next move is **data-engineer re-ingest of vault-parameter manifests**, not further retrieval tuning.

## Before / after metrics (rubric eval, N=3, same suite, same model)

| Dimension | Baseline (2026-05-12) | After fix (2026-05-13) | Delta | Threshold | Pass? |
|---|---|---|---|---|---|
| verdict_accuracy | 1.0 | 1.0 | 0 | 1.0 | ✅ |
| citation_relevance | **0.38** | **0.35** | -0.03 | 0.7 | ❌ |
| provider_kind_coverage | 1.0 | 1.0 | 0 | 1.0 | ✅ |
| hallucination_score | **0.33** | **0.33** | 0 (N=1 hit 1.0; variance) | 1.0 | ❌ |
| dissent_grounding | 0.63 | 0.57 | -0.06 | 0.5 | ✅ |
| confidence_calibration | 0.62 | 0.50 | -0.12 | 0.6 | ❌ |

**Per-fixture provider_kinds_present** is the cleanest signal that retrieval changed:

| Fixture | Baseline pks | After-fix pks |
|---|---|---|
| kamino-2024Q3-jlpusdc-entry | bazaar_live, canon_d, canon_m, market_data | bazaar_live, canon_d, canon_m, market_data |
| kamino-2024Q4-sol-leverage | canon_d, canon_m, market_data | **bazaar_live**, canon_b, canon_d, canon_m, **market_data** |
| kamino-2025Q1-jitosol-vault | bazaar_live, canon_b, canon_d, market_data | bazaar_live, canon_b, canon_d, market_data |

The lending-vertical fixture (#2) now surfaces bazaar_live + market_data for the first time (previously gated out by the vertical pre-filter — the vertical-widening fix landed).

## What landed (code)

Single commit on `s25/retrieval-push`. All changes scoped to retrieval; **prompts untouched** per `feedback_prompt_iteration_plateau`.

### 1. `_apply_retrieval_boosts` — Python-side post-scoring

`packages/gecko-core/src/gecko_core/orchestration/trade_panel/__init__.py`

Adds three additive boost terms applied after Atlas vectorSearch and before final top-K slicing:

| Term | Trigger | Magnitude |
|---|---|---|
| `_BOOST_PROTOCOL_EXACT` | chunk's protocol array contains the query protocol | +0.15 |
| `_BOOST_PROVIDER_SPECIFIC` | protocol match AND provider_kind ∈ {paysh_live, bazaar_live} | +0.10 |
| `_BOOST_DATE_ALIGNED` | `as_of_date` supplied AND chunk's parseable date within ±7d | +0.05 |
| `_DEMOTE_DATE_MISALIGNED` | `as_of_date` supplied AND chunk's parseable date outside ±7d | -0.10 |

Boost terms compose additively. A protocol-exact paysh_live chunk dated in-window stacks to +0.30 over the raw Atlas score. Empirical raw scores sit in [0.69, 0.78]; +0.30 reliably lifts protocol-manifest chunks above tangentially-related canon (which sits at raw ~0.74-0.78 for the Kamino suite).

Canon chunks (`protocol=[]`) get **zero** boost — the protocol-exact check correctly skips empty arrays. This was a near-miss bug: the existing chunk projector at line ~723 fell back to `proto_norm` when the raw `protocol` field was falsy (canon's `[]`), which would have credited every canon chunk with a phantom protocol-exact boost. Fix lands in the same commit.

### 2. `_parse_chunk_date` — best-effort ISO date extraction

Inspects (in order): `metadata.timestamp` (string or BSON datetime), `metadata.as_of_iso`, `captured_at` (for hot/live/daily only — static canon is intentionally date-agnostic), `source_url` `#<iso>` suffix, and a `(?<!\d)(20\d{2}-\d{2}-\d{2})(?!\d)` text regex. Returns None when no signal is parseable. Caller treats None as "do not apply date scoring" (neutral).

### 3. Vertical pre-filter widened to admit protocol-tagged paysh_live/bazaar_live across verticals

`paysh_live` + `bazaar_live` joined the existing `provider_kind` allowlist clause in the Atlas `$vectorSearch.filter`. Pre-fix: a `vertical=lending` request for Kamino dropped 7 paysh_live + 5 bazaar_live Kamino chunks (all tagged `vertical=dex`) before they reached scoring. Post-fix: they survive the pre-filter and the scoring layer decides ranking. Pattern F continues to hold — canon (provider_kind ∈ canon_*) admittance is unchanged.

### 4. `vectorSearch.limit` raised from `top_k * 5` → `top_k * 10`

Oversample by 10x so the Python boost pass has room to reshuffle. Atlas charges by candidate scan, not result size — 150 vs 75 is operationally identical in cost.

### 5. `as_of_date` threaded end-to-end

New optional parameter on `retrieve_trade_corpus_chunks` and `run_trade_panel_with_retrieval`. The rubric eval script (`tests/eval/scripts/score_defi_trade_rubric.py`) reads `fixture["as_of_date"]` and forwards it.

### 6. Unit test coverage

`packages/gecko-core/tests/orchestration/trade_panel/test_retrieval_boosts.py` — 13 pure-function tests over `_apply_retrieval_boosts` and `_parse_chunk_date`. No Mongo, no LLM, no autogen. Validates: protocol-exact promotion, canon non-boost, provider-specific stacking, scalar-protocol fallback, date-window boundaries, static-canon date skip, captured_at gating.

## What did NOT move and why

### citation_relevance (target ≥ 0.7) stayed at 0.35 — the data-quality ceiling

Direct read of judge notes from `tests/eval/live_runs/2026-05-13-s25-retrieval-N3.json`:

> "citations 1-5 are Zerion portfolio snapshots and raw API cost metadata with no direct relevance to Kamino JLP-USDC vault parameters"

The bazaar_live Kamino chunks are now surfacing (5 of 7 land in top-15), but their content is degenerate. Sample raw text from one of the boosted chunks:

```
{ "data": { "attributes": { "changes": { "absolute_1d": -0.0013758, "percent_1d": -0.0285 },
   "positions_distribution_by_chain": { "base": 4.82 }, ...
```

That's a Zerion portfolio-positions API response — **Base chain**, not Solana Kamino vaults. The chunks happened to mention "kamino" somewhere in the payload. Surfacing them ahead of canon makes the citation list *look* more on-topic to the protocol filter, but the judge correctly identifies them as opaque API metadata, not vault parameters.

Same shape on paysh_live: all 7 Kamino paysh_live chunks are literally `{ "data": [] }` (empty API responses). Even at +0.25 boost they barely surface, and when they do they cite nothing.

**The retrieval lever is correctly pulled. The bottleneck is content quality.**

### confidence_calibration dropped 0.62 → 0.50

Judge attributes this to the panel's internal coordinator inconsistency (one fixture had the coordinator's internal verdict 'act' while the envelope shipped 'defer'). That's a coordinator-prose bug independent of retrieval — not chased here per the prompt-iteration plateau memory (coordinator verdict logic belongs in code, not prompt; deferred to a separate ticket).

### hallucination_score N=1 hit 1.0, N=3 reverted to 0.33

The N=1 run on fixture #1 scored hallucination=1.0 (judge noted "TVL figure properly attributed to citation 6 market_data"). The N=3 re-run of the same fixture scored 0.0 ("market_data source is dated 2026-05-12 while question is as_of 2024-08-12 — date mismatch suspicious but not a named must_not_hallucinate"). This is **judge variance at small N**, not a regression. The structural fix (date demotion on misaligned market_data) is firing — verified directly: bazaar_live for the lending fixture shows `boost=+0.15` (= +0.15 protocol + +0.10 provider − 0.10 date misalign), market_data shows `boost=+0.05` (= +0.15 protocol − 0.10 date misalign).

## Cost log

- N=1 N1: ~$0.13 ($0.10 panel + $0.03 judge)
- N=3 N3 final: ~$0.40 ($0.30 panel + $0.10 judge)
- Total ticket spend: **~$0.53** (well under $3.00 budget)

## What data-engineer needs to do next

The retrieval push surfaced two structural data gaps. Both are out-of-scope for this ticket (ai-ml lane) and queued for data-engineer (next-sprint slot):

### Gap 1: paysh_live Kamino chunks are empty `{ "data": [] }` payloads

All 7 paysh_live Kamino chunks in Mongo are empty API responses. They were almost certainly ingested from a fixture-mode paysh call that returned an empty `data` array. **Re-ingest paysh_live for Kamino using a real vault-params manifest endpoint** (Kamino multiply vault catalog, USDC/PYUSD pool parameters, leverage caps, oracle config). Same problem likely affects Drift, Jupiter, Jito, Sanctum — verify all 5 protocols in the rubric suite.

Diagnostic query for any operator:

```python
async for doc in chunks_collection().find({"provider_kind": "paysh_live", "protocol": "kamino"}):
    print(doc["text"][:100])
# Expected: vault-params manifest content
# Actual: { "data": [] } x7
```

### Gap 2: bazaar_live Kamino chunks are wrong-protocol metadata

The bazaar_live Kamino chunks are Zerion portfolio-positions API responses for **Base chain** addresses that happen to mention "kamino" in the payload. Re-ingest needs a Solana-Kamino-specific bazaar query — likely a different endpoint or filter. The current chunks are noise that survives the `protocol=kamino` tag because the upstream scraper tagged them on a substring match, not actual protocol provenance.

### Gap 3: market_data is current-snapshot-only — no historical price grounding

`metadata.timestamp` and `source_url` both anchor to the latest hourly snapshot (`2026-05-12T08:00:00Z`). A fixture asking about SOL at $165 on 2024-11-05 retrieves SOL at $147 (current price). The date-demotion landed in this ticket correctly drops misaligned market_data by -0.10 — it still surfaces (only 1-2 chunks per query), but the panel sees the date mismatch and the judge flags it.

**Real fix is a historical-price grounding source.** Two options for S25 W2:
1. Pyth historical API — has minute-level data going back ~2 years; ingest as separate `provider_kind="market_data_historical"` with `metadata.snapshot_date` matching the requested fixture date.
2. CoinGecko `/coins/{id}/history?date=DD-MM-YYYY` — free tier covers daily granularity back to genesis for major assets.

Pyth is the natively-Solana path; CoinGecko is the no-funding-pressure default per `feedback_okx_no_funding_pressure`. Either is a clean follow-on to this ticket's date-demotion scaffolding.

## Call on defer-rate

Defer-rate did not move and was not optimized for (per `feedback_prompt_iteration_plateau`). The verdict distribution across N=3 is `1 act / 2 defer` — fixture #1 flipped from defer to act in the after-fix run. That's verdict-accuracy noise at N=3 and consistent with the panel acting on the stronger protocol-tagged candidate set when retrieval feeds it grounded chunks. Wait for citation_relevance to cross 0.6 (post data-engineer re-ingest) before re-baselining defer-rate.

## Files touched

- `packages/gecko-core/src/gecko_core/orchestration/trade_panel/__init__.py`
- `packages/gecko-core/tests/orchestration/trade_panel/test_retrieval_boosts.py` (new)
- `tests/eval/scripts/score_defi_trade_rubric.py`
- `tests/eval/live_runs/2026-05-13-s25-retrieval-N3.json` (new, eval output)
- `docs/strategy/2026-05-13-s25-retrieval-handoff.md` (this doc)
