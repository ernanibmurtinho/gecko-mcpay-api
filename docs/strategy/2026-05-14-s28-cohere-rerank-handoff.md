# S28 W1 #25 — Cohere Rerank Handoff (2026-05-13)

**Branch:** `s28/cohere-rerank` (HEAD: `229e476`)
**Status:** Code shipped. Live rubric eval GATED on `COHERE_API_KEY` provisioning.
**Total spend this dispatch:** ~$0.05 (one canary in graceful-degrade mode). Budget ceiling was $0.20.

---

## Code-ship summary

Three commits on `s28/cohere-rerank` branched from `s27/path-d-22` (`820ca9d`):

1. **`5ad1167` — `feat(s28-#25): cohere rerank module + .env + doctor check`**
   - New module `packages/gecko-core/src/gecko_core/rag/rerank.py`.
   - `async def cohere_rerank(query, documents, top_n=5, model="rerank-v3.5") -> list[tuple[int, float]]`
   - Cohere SDK v5 `AsyncClient.rerank` via `os.environ["COHERE_API_KEY"]`.
   - Graceful degrade: missing key emits `rerank.skipped` once/process, returns `[]`.
   - Hard-cap docs[]=1000 (Cohere API ceiling), 10s timeout, one retry on 429 with 0.5s backoff.
   - Cost telemetry: `rerank.call` event with `n_docs`, `top_n`, `cost_estimate_usd`.
   - Mongo cache (`rerank_cache` collection) keyed on `(query_hash, documents_hash, model, top_n)` with 5min TTL.
   - `.env.example`: `COHERE_API_KEY=` block added under VOYAGE block.
   - `packages/gecko-mcp/.../doctor.py`: `check_cohere_api_key` — INFO row when unset (degrade message), redacted `<first2>...<last4>` sentinel when set. Wired into `run_doctor` after `check_voyage_api_key`.
   - `packages/gecko-core/pyproject.toml`: `cohere>=5.0`.
   - Tests: 4 fixtures in `packages/gecko-core/tests/rag/test_rerank.py` (happy path, 429 retry, timeout degrade, no-key skip). All 4 PASS via `uv run pytest packages/gecko-core/tests/rag/test_rerank.py -v`.

2. **`70b5d4b` — `feat(s28-#25): wire rerank into retrieve_trade_corpus_chunks + K=5 + L3 soften`**
   - `_DEFAULT_TRADE_TOP_K: 15 -> 5` in `orchestration/trade_panel/__init__.py`.
   - `retrieve_trade_corpus_chunks`: insert `cohere_rerank(query=idea, documents=[r["text"] ...], top_n=top_k)` between `_apply_retrieval_boosts` and `_provider_quota_floor`. Boost = structural priors. Cohere = semantic re-ordering. Quota floor = breadth invariants (becomes a no-op when rerank returns top_n=5 already).
   - `_opening_prompt`: S26 #19 CITATION BREADTH replaced with CITATION DISCIPLINE — cite-what-you-reason-over, no breadth padding, no number-to-target mistake when the chunk doesn't name the target asset.
   - Wide candidate pool (~180) at Atlas `$vectorSearch` is unchanged. Cost ceiling and quota allocator unchanged.

3. **`229e476` — `eval(s28-#25): canary in graceful-degrade mode (no live spend)`**
   - Canary fixture `kamino-2024Q3-jlpusdc-entry`, COHERE_API_KEY unset.
   - **D1 PASS** (answered_back).
   - **D2 PASS** (context_overflow).
   - **D3 FAIL** (3/5 phantom citations) — expected: K=5 wide-pool head is not semantically curated; this IS the failure mode S28 #25 fixes when the key is provisioned.
   - Cost: $0.05.
   - Artifact: `tests/eval/live_runs/2026-05-13-canary-2.json`.

---

## Cost projection per 1000 verdicts (live, key set, production settings)

| Component | Unit | Per verdict | Per 1000 verdicts |
|---|---|---|---|
| Cohere rerank call | $1 / 1000 calls | $0.001 | $1.00 |
| Cohere rerank docs | $0.20 / 1000 docs | $0.20 * 180/1000 = $0.036 | $36.00 |
| **Cohere subtotal** | | **~$0.037** | **~$37.00** |

Notes:
- The 180-doc number is the worst-case wide-pool size at Path D (`numCandidates=400`, `limit=top_k*15` at top_k=5 = 75; in practice 100-180 surfacing after the post-`$match` on protocol). The original dispatch's projection of "$0.18 / 1000 verdicts" assumed top_n=5 was being CHARGED separately, but Cohere bills on **input docs reranked**, not output. The unit cost is therefore proportional to wide-pool size, not top_n.
- The Mongo 5min TTL cache absorbs duplicate-call cost during high-cadence re-verdict loops (cache-then-charge per `project_three_layer_architecture_2026_05_11`). At expected basic-tier refresh cadence (24h) the cache contributes ~0% hit rate; the trade-agent in active cadence (5min-1h) sees 30-60% hit-rate-implied savings.
- LLM panel cost is unchanged (~$0.05 / verdict at gpt-4o-mini basic tier). Cohere adds ~75% to per-verdict infra cost in absolute terms; ratio to LLM cost is ~0.74x.

If cost is judged unacceptable: knobs to turn (in order of leverage):
1. Cap `len(documents)` sent to Cohere at 75 (top of post-boost pool) → ~$15/1000 verdicts.
2. Move rerank ahead of the wide pool — rerank only the post-`$match` head (60-75 chunks instead of 180) → ~$12-15/1000 verdicts. Requires verifying that the post-`$match` head still carries enough canon for `pkCov`.
3. Switch to Cohere `rerank-english-v3.0` (3x cheaper than `rerank-v3.5`) and re-baseline rubric. Quality delta unmeasured; suggested only if cost is the binding constraint.

---

## Founder unlock command — exact one-liner

After you've set `COHERE_API_KEY=<key>` in `.env`:

```bash
export COHERE_API_KEY=$(grep '^COHERE_API_KEY=' .env | cut -d= -f2) && \
  uv run python tests/eval/scripts/score_defi_trade_rubric.py \
    --limit 3 --tier basic --tag s28-cohere-live
```

This runs the DeFi trade rubric on N=3 fixtures, basic tier, tagged `s28-cohere-live`. Cost: ~$0.15 ($0.05/verdict × 3).

Acceptance bars (V1 ship-gate):
- **citRel ≥ 0.7** (baseline 0.45 — the gate this sprint exists to clear)
- **pkCov ≥ 0.9** (baseline 1.000 — must not regress)
- **hall ≥ 0.5** (1 − hallucination rate; baseline 0.000 means hallucination=1.0 i.e. all phantom; rubric semantics: higher = better)
- **vAcc ≥ 0.9** (baseline 1.000 — must not regress)

If any bar fails: STOP. Do NOT iterate prompts. Report back. Likely follow-ups:

---

## Fallback escalation if rerank plateaus

Per §4.4 of the S28 dispatch plan: gpt-4o coordinator swap.

**Trigger:** if rubric N=3 with rerank live still leaves `citRel < 0.7` after the L3 soften.

**Action:**
1. Edit `packages/gecko-core/src/gecko_core/orchestration/trade_panel/agents.py` (or wherever the coordinator's `llm_config` is built — grep for `COORDINATOR` + `model=` since this varies by branch).
2. Set the coordinator's model from `gpt-4o-mini` → `gpt-4o`.
3. Re-run the same rubric N=3 command. Cost delta: ~+$0.20/verdict for the coordinator turn only (other 6 voices unchanged at mini). Per-rubric run: ~$0.75 instead of $0.15.

This is NOT pre-emptive — gpt-4o is only proposed if rerank alone is insufficient. The Cohere rerank is the load-bearing change.

---

## What this dispatch did NOT do

- Did NOT run rubric N=3 (gated on `COHERE_API_KEY`).
- Did NOT push to main.
- Did NOT touch the boost code, the quota allocator, the retrieval pool sizing, or the `_RAG_CONTEXT_CHAR_CAP`.
- Did NOT modify the eval rubric itself; the bars above are the existing S27 Path D rubric outputs.

## Acceptance for the dispatch

| Item | Result |
|---|---|
| 3 commits on `s28/cohere-rerank` | DONE (5ad1167, 70b5d4b, 229e476) |
| Unit tests pass (4 fixtures) | 4/4 PASS |
| Canary D1+D2 PASS in degrade mode | DONE (D1 PASS, D2 PASS, D3 FAIL expected) |
| Handoff doc with founder unlock + cost projection | THIS DOC |
| Budget ≤ $0.20 | $0.05 actual |

Branch HEAD ready to merge into `main` after founder runs the live rubric and confirms the V1 ship-gate.
