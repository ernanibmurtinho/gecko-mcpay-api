# S28 #26 — Corpus Depth Handoff (2026-05-13)

## Headline

Corpus-depth pass executed for all four non-kamino protocols. **+884 protocol_native chunks landed in Mongo**. Pattern E reachability verified end-to-end via the production retrieval path. **Final Tier 3 N=10 eval was BLOCKED at fixture 4 of 10 by Anthropic credit-balance exhaustion** — only the first three kamino fixtures scored. The four non-kamino protocols (drift / jupiter / jito / sanctum) were not exercised by the rubric judge. **V1 ship-gate is UNDETERMINED pending billing refill and re-run.**

## Branch reality

- Branch: `s28/corpus-depth` cut from `s28/cohere-rerank`
- Carries Cohere rerank wiring + cap-fix + corpus + quota allocator + boost code (per ticket pre-conditions)

Commits:

| Commit | What |
|---|---|
| `98b67fc` | feat(s28-#26): drift corpus depth — 32 endpoints, +124 chunks (also carries the full file with catalog edits for jupiter/jito/sanctum) |
| `49a4493` | feat(s28-#26): jupiter corpus depth — 31 endpoints, +292 chunks (ingest milestone) |
| `068a3bf` | feat(s28-#26): jito corpus depth — 23 endpoints, +341 chunks (ingest milestone) |
| `89bf4e8` | feat(s28-#26): sanctum corpus depth — 25 endpoints, +68 chunks (ingest milestone) |
| (this commit) | eval(s28-#26): post-corpus-depth Tier 3 partial run + handoff |

## (1) Per-protocol chunk counts

Direct Mongo `count_documents({provider_kind:"protocol_native", protocol:"<p>"})`:

| Protocol | Before (#14) | After (#26) | Delta | Target (≥30) | Status |
|---|---:|---:|---:|---:|:---:|
| drift | 3 | 127 | +124 | 30 | PASS |
| jupiter | 4 | 296 | +292 | 30 | PASS |
| jito | 3 | 344 | +341 | 30 | PASS |
| sanctum | 2 | 70 | +68 | 30 | PASS |
| kamino (control) | 121 | 121 | 0 | 30 | PASS (unchanged) |

All four targets met. No protocol below 30; no protocol below 15 (the "flag-it" threshold).

### Composition notes (substantive vs catalog noise)

- **drift (124)** — almost entirely mechanism docs from `docs.drift.trade`: perpetuals trading, funding rates, auction params, liquidations engine + liquidators, oracles, risk parameters + safety module, insurance fund, margin model + account health + per-market leverage, JIT auctions, vAMM, DLOB matching engine, borrow-lend (interest rate, isolated pools, Amplify), market specs, trading fees, PnL accounting, revenue pool, glossary, account model, GitHub README. 1 endpoint (dlob-markets) returned 503 at ingest time.

- **jupiter (292)** — 95 chunks of substantive Swap/Perps/Lend/Tokens/Trigger/Recurring mechanism docs from `dev.jup.ag/docs`. ~197 chunks from the `tokens/v2/tag?query=verified` (109) and `tokens/v2/tag?query=lst` (88) catalog JSONs. The catalog chunks are low-signal but harmless under retrieval — they tag `protocol=["jupiter"]` and score below mechanism docs for trade questions.

- **jito (341)** — 22 chunks from the canonical `docs.jito.wtf/lowlatencytxnsend/` (full MEV bundle API spec including tip percentiles, sendBundle, atomicity), 6 from the ShredStream proxy doc, 10+ from jito.wtf product pages (searchers, stakers, validators, blog) and GitHub READMEs (stakenet/restaking/jito-solana/mev-protos/RPC SDKs). **281 of the 341 chunks come from the live validator-set telemetry JSON** (kobe.mainnet.jito.network/validators, ~266KB). High volume / lower individual signal. Substantive citation surface ≈ 40 chunks; the rest is validator-pool metadata.

- **sanctum (68)** — full coverage of `learn.sanctum.so/docs`: mission, LST intro (PoW vs PoS, native→liquid), Infinity (technical + non-technical), Router, Reserve, LSTs, Gateway, creator-LST flow (understanding, package, setup, mint, post-deployment), deployed-programs, sanctum-api. Plus 6 live quote endpoints (sol-value INF + jitoSOL, APY for INF/jitoSOL/mSOL/bSOL). Sanctum docs are concise per page → modest chunk yield, but every page is on-thesis.

## (2) Pattern E reachability — end-to-end retrieval probe

Direct call to `retrieve_trade_corpus_chunks` (production retrieval path) with the actual fixture questions and `top_k=10`:

| Fixture question | Protocol-tagged chunks in top-10 | protocol_native in top-10 | Top-1 source |
|---|---:|---:|---|
| Drift SOL-PERP 2x long pre-FOMC | 9/10 | 7/10 | docs.drift.trade/.../perpetuals-trading |
| Jupiter JLP vs JitoSOL rotation | 10/10 | 10/10 | lite-api.jup.ag/tokens/.../lst |
| Jito MEV P75 tip band | 10/10 | 10/10 | bundles.jito.wtf/.../tip_floor |
| Sanctum INF unstake vs hold | 10/10 | 10/10 | learn.sanctum.so/.../infinity-non-technical |

**Pattern E gate: PASS.** New chunks are reachable through the production retrieval path. Atlas Search vector-index picked them up immediately (no manual reindex needed).

## (3) Tier 3 N=10 eval — PARTIAL

Run: `uv run python tests/eval/scripts/score_defi_trade_rubric.py --limit 10 --tier basic --tag s28-corpus-depth-cohere`

Output: `tests/eval/live_runs/2026-05-13-s24-defi-rubric-s28-corpus-depth-cohere-3.json`

### Anthropic billing exhaustion

Fixtures 4-10 failed with:

```
BadRequestError: Error code: 400 — Your credit balance is too low to access the Anthropic API.
```

Fixture order:
- 1-3: kamino (SCORED)
- 4: kamino-2025Q3-usdc-pyusd-exit (FAILED — 500 internal error, likely right at credit exhaustion)
- 5: drift-2024Q4-sol-perp-long (FAILED — credit balance too low)
- 6: drift-2025Q2-jto-perp-short (FAILED)
- 7: jupiter-2024Q3-jlp-vs-jitosol (FAILED)
- 8: jupiter-2025Q1-lst-rotation-msol (FAILED)
- 9: jito-2025Q2-mev-tip-band (FAILED)
- 10: sanctum-2025Q3-unstake-vs-hold (FAILED)

The Cohere-rerank live N=3 run earlier today (`req_011Cb12...`) already drained the bulk of Anthropic credits. This run's first 3 fixtures consumed what remained.

### What did score (n=3, kamino-only)

| Dimension | Mean | Threshold | Per-fixture pass-rate |
|---|---:|---:|---:|
| verdict_accuracy | 1.000 | 1.0 | 1.000 |
| citation_relevance | 0.483 | 0.7 | 0.000 |
| provider_kind_coverage | 0.333 | 1.0 | 0.333 |
| hallucination_score | 0.667 | 1.0 | 0.667 |
| dissent_grounding | 0.567 | 0.5 | 1.000 |
| confidence_calibration | 0.533 | 0.6 | 0.333 |

Per-fixture (kamino only):

| Fixture | citRel | pkCov | hall | dissent | confCal | verdictAcc |
|---|---:|---:|---:|---:|---:|---:|
| kamino-2024Q3-jlpusdc-entry | 0.40 | 0 | 1.00 | 0.70 | 0.65 | 1.0 |
| kamino-2024Q4-sol-leverage-entry | 0.55 | 1.00 | 0.00 | 0.50 | 0.55 | 1.0 |
| kamino-2025Q1-jitosol-vault | 0.50 | 0 | 1.00 | 0.50 | 0.40 | 1.0 |

## (4) V1 ship-gate verdict

| Metric | Target | Observed (n=3 kamino) | Required for V1 | Status |
|---|---:|---:|---|---|
| Mean citation_relevance ≥ 0.60 | 0.60 | 0.48 (kamino) | sample is wrong — non-kamino missing | UNDETERMINED |
| Per-fixture non-kamino citRel ≥ 0.40 | 0.40 | (no data) | needs re-run | UNDETERMINED |
| Mean hallucination_score ≥ 0.50 | 0.50 | 0.67 (kamino) | YES on kamino sample | (proxy) GREEN |
| Mean provider_kind_coverage ≥ 0.7 | 0.7 | 0.33 (kamino) | NO — kamino sample below target | (proxy) RED |
| Mean verdict_accuracy ≥ 0.90 | 0.90 | 1.00 | YES | GREEN |

**Color: UNDETERMINED** (not RED — the four non-kamino fixtures, which are the WHOLE point of this ticket, never reached the judge). The kamino sample alone is uninformative for the corpus-depth thesis — kamino was the control group with 121 baseline chunks. The thesis to test is whether drift/jupiter/jito/sanctum citation_relevance jumps from the 0.15-0.30 baseline to ≥0.40.

## (5) Spend

| Item | Cost (estimated) |
|---|---:|
| Voyage AI embeddings (884 new chunks ≈ 450K tokens × $0.06/1M) | ~$0.03 |
| Mongo writes | $0 (within free tier) |
| Cohere rerank (live in retrieval path; 10 fixtures attempted) | ~$0.05 |
| OpenAI panel + judge (Anthropic Sonnet 4.6) — 3 fixtures completed | unknown (Anthropic balance unmeasurable) |
| **Total to my side (ingest + writes + Cohere)** | **~$0.08** |

Anthropic-side cost cannot be measured by me (no secret access). The eval consumed whatever was left of the Anthropic balance before failing.

Well under the $7 budget on the ingest side. The unbudgeted variable is the Anthropic refill needed to actually finish the eval.

## (6) Recommended next move

**Founder action required: refill Anthropic API credits**, then re-run the same command:

```bash
set -a && source .env && set +a
uv run python tests/eval/scripts/score_defi_trade_rubric.py \
  --limit 10 --tier basic --tag s28-corpus-depth-cohere-v2
```

That run will exercise all 10 fixtures and produce the actual V1 ship-gate verdict. Estimated Anthropic spend: $3-5 for the panel + judge across 10 Tier-3 fixtures (per the cost projection in `2026-05-14-s28-cohere-rerank-handoff.md`).

### Three-way contingency for after the re-run

- **If non-kamino mean citRel ≥ 0.60 (GREEN):** ship V1, close S28. Corpus depth was the missing piece; Cohere rerank + quota allocator + boosts + depth all stacked. The retrieval-wedge thesis is verified.

- **If non-kamino mean citRel 0.40-0.59 (YELLOW):** the corpus depth helped but not enough. Likely next move: drop Cohere (per the cohere-rerank handoff: "marginal value of rerank is small when retrieval pool is already good" — implies marginal NEGATIVE value when corpus is now well-balanced), reduce K from 5 back to 7-10, and re-test. Corpus depth alone may be the win.

- **If non-kamino mean citRel < 0.40 (RED):** the bottleneck is not corpus depth. Investigate (a) whether the panel synth is actually citing protocol_native chunks vs only citing canon, (b) whether the quota allocator is starving protocol_native in favor of canon, (c) whether the Cohere reranker is downranking protocol_native chunks because their prose is light on macro-investor language. The probe I'd run first: scan the panel's `cited_doc_ids` across the 7 failed fixtures and check the protocol_native share.

### My single-recommendation

**Founder: refill Anthropic credits, then re-run.** The retrieval-layer probe in (2) already proves the chunks reach the panel input. The unknown is whether the panel synth cites them. We can't know without the judge. Don't ship V1 on n=3 kamino-only data.

## Hard constraints — checklist

- [x] Branch `s28/corpus-depth` cut from `s28/cohere-rerank`
- [x] Did not touch retrieval / rerank / quota / boost / prompt code
- [x] No bare pytest invocations
- [x] Did not push to main or other shared branches
- [x] No SSM / secret access
- [x] No new Pattern A literals (protocol_native already existed from #14)
- [x] Pattern F: all new chunks carry `protocol=["<slug>"]` (not `[]`)
- [x] Pattern E: end-to-end retrieval reachability verified before claiming "shipped"
- [x] No protocol below 15 chunks (all four well above)
- [x] Idempotency: `uuid5(NAMESPACE_URL, "<endpoint>#<day_bucket>")` reused from #14 — re-runs same day are no-ops
- [x] Spend < $7 hard budget on ingest side

## What did NOT happen this session

- Final V1 ship-gate determination (blocked on Anthropic credits)
- Per-fixture citRel delta for the 6 non-kamino fixtures (never reached judge)
- Decision on whether to drop Cohere — needs the post-refill data
