# S20 Plan — Knowledge as Commodity (Build-Context Layer)

**Window:** 2026-05-10 → 2026-05-17 (proposed, ~6 working days solo)
**Predecessor:** S19 (Voyage/Mongo hardening — H1/H2/H4 shipped 2026-05-05)
**Author:** staff-engineer (synthesis), data-engineer (Track A), web3-engineer (Track B), ai-ml-engineer (Track C)
**Theme:** Build the persistent, categorized, build-aware context layer. Gecko sits between paid sources (pay.sh, Tavily, twit.sh, Bazaar) and the builder's own build agent — pre-loading full domain knowledge per *app vertical* (neobank, DEX, marketplace, …), indexed across 7 knowledge dimensions, served on every build-step query. The base compounds per vertical; later builders in the same vertical hit denser, faster, cheaper retrieval.

**Refined positioning (2026-05-06 brainstorm):** the moat is **NOT** the verdict shape alone. It's the combination of **(a) vertical-shaped index** (neobank, DEX, marketplace…), **(b) build-time consumption pattern** (the user's own build agent queries Gecko on every step, not just at pre-ideation), **(c) compounding density per vertical** (each builder's queries enrich the base), and **(d) switching cost on the agent integration** (build agents wired to Gecko's structured store). pay.sh sells the rail; Gecko sells the categorized organization. Different axes — orthogonal, not substitutes.

**Public-facing wedge sentence (revised 2026-05-06 post-dogfood `c05ab663`):**
> "Gecko is the categorized build-context layer for AI builders. We pre-load the full domain knowledge for your app vertical (neobank, DEX, marketplace, …), index it across 7 dimensions, and serve it to your build agent on every step — paid per call via x402. **The first 10 builders per (vertical, category) cell earn a revenue share on every subsequent retrieval.** Builders make the next one cheaper *and* get paid for seeding the cell."

**SUPERSEDED wedge sentence (2026-05-05 → 2026-05-06):** previously framed as "first builder pays a pioneer's surcharge." Dogfood judge rejected as a "gamified knowledge-sharing tax." See memory `project_pioneer_as_bounty_reframe` — pioneer flips from buyer-with-surcharge to contributor-with-bounty. Same compounding-base economics, different role framing.

**Reference docs:**
- Manifest sketch: `docs/strategy/2026-05-05-agent-skills-manifest-sketch.md`
- Pricing math: same doc (verified against live `bb research` smoke 2026-05-05–06)
- Manus first-touch sequence: `docs/demo/2026-05-06-manus-first-touch-sequence.md`
- Memory: `project_knowledge_as_commodity_pivot`, `reference_pay_sh`

**Verbosity-smoke verification (2026-05-06):** local gecko-api booted with `LOG_LEVEL=DEBUG`, two `bb research` runs at `tier=basic preset=budget` produced clean OpenRouter dispatch (`openai/gpt-4.1-mini-2025-04-14` via OpenRouter), Voyage embed (1024-dim Mongo), and zero errors. Total cost per session: ~$0.043. Proceed.

**Production smoke (2026-05-06, session `6afd55d7`):** full pipeline on v0.2.12 against `api.geckovision.tech` returned REFINE verdict with TAM=4 / WEDGE=5 / V1_FEAS=8. Surviving dissents (TAM unquantified, no named paying user, weak moat) directly motivate this S20 plan: the moat-trajectory thesis below converts each surviving dissent into a track.

---

## Three tracks

### Track A — Mongo consolidation + categorized-knowledge schema (data-engineer)

Spine of the sprint. Six tickets, ordered by dependency.

| # | Ticket | Owner | Effort | Acceptance |
|---|---|---|---|---|
| **A1** | **S20-A-TAXONOMY-01** | data-eng | S | Single-source-of-truth module `packages/gecko-core/src/gecko_core/knowledge/taxonomy.py`. **TWO orthogonal axes** declared as Literals: `Category` (7 knowledge dimensions: market_intelligence, business_financial, investment_signals, product, technical_engineering, ai_ml, design_ux) AND `Vertical` (app domain: `neobank`, `dex`, `marketplace`, `prediction_market`, `indexer`, `b2b_saas`, `consumer_social`, `ai_agent_platform`, `gaming`, `infra_devtool`, `unknown`). `Subcategory` map per Category. `KnowledgeSource` Literal (`web` \| `tavily` \| `twit_sh` \| `bazaar` \| `pay_sh` \| `user_query` \| `enriched_output`). `ChunkMetadata` TypedDict (`confidence: float`, `usage_count: int`, `timestamp: datetime`, `pioneer: bool` — true for first chunk in a (vertical, category) cell). Schema-drift test mirrors `tests/test_payment_mode_consistency.py`. Pattern A enforced. **Vertical list is editable post-launch but the Literal must be the canonical source — no string-typed verticals anywhere.** |
| **A2** | **S20-A-CHUNK-SCHEMA-01** | data-eng | M | Extend `MongoChunkDoc` (`mongo_chunks.py:108-125`) with `category`, `subcategory`, `vertical`, `source`, `metadata.{confidence,usage_count,timestamp,pioneer}`. Alias `provider_kind` → `source` (deprecated read-only mirror for one sprint). New `gecko_core/knowledge/classifier.py` (`gpt-4o-mini`, `response_format=json_object`, ≤1.5k input tokens, batch=20) emits BOTH `category` and `vertical` per chunk. Reject writes missing `category`/`vertical`/`source`. Mongo validator JSON enforces both enums from A1. **Compound index on `(vertical, category, project_id, captured_at)` becomes the primary read path** — every retrieval filters by `(vertical, category)` first. |
| **A3** | **S20-A-LEGACY-REVOKE-01** | data-eng | S | Per `project_mongo_cutover_no_backfill`: stamp legacy chunks `category="legacy_uncategorized"`, `metadata.deprecated=true`. Default `rag_query` filter excludes legacy unless `include_legacy=True`. Build index on `(category, project_id, captured_at)`. |
| **A4** | **S20-A-PRECEDENT-PORT-01** | data-eng | M | Port `gecko_precedent` Postgres table → Mongo `precedents` collection (Voyage 1024). Eliminates the 1536-dim Postgres tail that produced S19's dimension churn. Spike eval: ≥0.7 Jaccard top-5 overlap vs current pgvector before merge. Postgres migration `revoke_writes_gecko_precedent.sql` keeps table for read-fallback one sprint. |
| **A5** | **S20-A-MEMORY-PORT-01** | data-eng | M | Same shape as A4 for `memory` table. After this lands, pgvector has zero active write paths. `embed_for_postgres_vector` is unreferenced (grep guard). |
| **A6** | **S20-A-USAGE-COUNT-01** | sw-eng | S | `usage_count` bumps on **citation in output** (not retrieval). Synth emits `cited_doc_ids`; async batch `$inc` per cited chunk. Structured log line `chunk_cited` with `chunk_id`, `category`, `vertical`, `confidence`. |
| **A7** | **S20-A-VERTICAL-PIONEER-01** | data-eng | S (0.5d) | New `gecko_core/knowledge/pioneer.py`. On every ingest, check Mongo for `(vertical, category)` cell density; if `count(chunks WHERE vertical=X AND category=Y) < PIONEER_THRESHOLD` (default 5), mark this chunk `metadata.pioneer = True` and surface `pioneer_call=true` in the response payload. **Pricing decision (whether pioneer pays premium, flat, or we eat cost) is OPEN — see §Open decisions before sleep.** This ticket only INSTRUMENTS the signal. |
| **A8** | **S20-A-VERTICAL-DETECT-01** | ai-ml | S (0.5d) | When a session opens, classify the user idea into one of the declared Verticals. Reuse `gecko_core/knowledge/classifier.py` from A2 with a vertical-specific prompt; default to `unknown` on low confidence (<0.6). User can override via explicit `vertical` param on `gecko_research` / `gecko_research_market` calls. **Open: if classifier returns `unknown`, do we (a) refuse the call, (b) fall back to cross-vertical retrieval, (c) prompt the user to pick? Recommended: (b) for now — graceful degradation.** |

**Track A risks:**
- Old-vs-new chunk boundary → fresh-start cutover (A3), reject classifier-on-read.
- Classifier latency on ingest → batch=20, gpt-4o-mini, +~8s per 100-chunk session is acceptable.
- `provider_kind` / `source` semantic drift → alias one sprint, drop in S21.
- Voyage-1024 retrieval quality on precedents → spike eval before A4 merge.
- Mongo Atlas index storage with new `(category, …)` compound → monitor; budget paid tier.

---

### Track B — Manifest publishing + per-skill x402 dispatch (web3-engineer)

Six tickets. Wire the publishing surface and the gate.

| # | Ticket | Owner | Effort | Acceptance |
|---|---|---|---|---|
| **B1** | **S20-B-SKILL-REGISTRY-01** | sw-eng | M (1.5d) | Canonical `Skill` dataclass + 12 entries in `gecko_core/skills/registry.py`. `manifest.py` builds the pay.sh v1.0 shape with `pricing` and `gecko_knowledge_category` extension fields. Schema-drift test against pay.sh required keys + price floors matching existing `/pricing`. |
| **B2** | **S20-B-MANIFEST-ENDPOINT-01** | sw-eng | S (0.5d) | Publish at `app.geckovision.tech/.well-known/agent-skills/index.json`. `Cache-Control: public, max-age=300`, ETag. **Description copy gate:** business-manager + product-designer must sign off on the 12 description strings before merge — per memory `project_output_layer_positioning`, must position as "structured insight layer" not "API at $X." |
| **B3** | **S20-B-X402-DISPATCH-01** | web3-eng | M (2d) | Single `POST /skills/{skill_name}` route — no 12 hard-coded paths. New `payments/gate.py` and `payments/dispatch.py` (facilitator-neutral). Reuses existing `X402Client`, `get_client(mode)`. Switching `X402_CHAIN` env requires zero code change. Skills declare `supported_chains`, never a facilitator. |
| **B4** | **S20-B-CREDIT-PACK-01** | web3-eng | M (2d) | `credit-pack` ($10 / 1.5M tokens) — Ed25519-signed JWT (`sub=wallet, jti=on_chain_tx_sig, tokens_remaining, exp`). Server-held key; `jti` is settlement tx signature (anti-replay). Mongo collection `credit_tokens(jti PK, ...)`. Atomic decrement with row-lock; double-spend test fails one cleanly. |
| **B5** | **S20-B-DISPATCH-HANDLERS-01** | sw-eng | M (1.5d) | Thin per-skill handlers in `gecko_core/skills/handlers.py`. `retrieve-*` → `rag_query`, `research-*` → existing pro orchestration, `research-full` → full pipeline. Bundled-token meter enforced (50K/100K/200K/500K caps → overage billed via x402 supplemental intent OR credit-token decrement). |
| **B6** | **S20-B-CONTRACT-TESTS-01** | web3-eng | S (1d) | (a) pay.sh-style discovery contract — fetch our manifest, resolve each skill URL, assert 402 shape. (b) Facilitator `/verify` contract tests with VCR-recorded fixtures (Pattern C) for frames.ag (Solana) + CDP (Base). `live_skills` pytest marker, gated on `RUN_LIVE_SKILLS=1`. |

**Track B risks:**
- **Bulk-credit token forgery** → Ed25519 JWT with `jti=settlement_sig`, server-held key. Document key-rotation procedure.
- **Facilitator hard-coding** → single dispatcher; schema-drift test asserts no skill hard-codes a facilitator URL.
- **gecko-claude / pay.sh discovery mispositioning** → B2 description copy gate. Use `gecko_knowledge_category` values that signal layer-position (`insight.retrieval`, `insight.debate`, `insight.pipeline`).

---

### Track C — Citation extraction + enrichment loop (ai-ml-engineer)

Seven tickets. Closes the compounding flywheel.

| # | Ticket | Owner | Effort | Acceptance |
|---|---|---|---|---|
| **C1** | **S20-C-CITATION-CONTRACT-01** | ai-ml + sw-eng | S | Extend synth output schema with `cited_doc_ids: list[str]` and `citations: list[Citation]`. `[N]` markers in prose map 1:1 to `citations[i].idx`. Post-validate; drop unmatched IDs; log drop rate. Touches `models.py`, `judges/synth.py`, `orchestration/pro/post_processors.py`, `orchestration/basic.py`. |
| **C2** | **S20-C-CONFIDENCE-PROMPT-01** | ai-ml | S | Per-team confidence self-rating prompt scoring 3 dimensions (evidence_floor, dissent_quality, citation_density), returning `min`. Per-section emission + document-level min aggregate. Calibration target: 0.9 = dense base + multi-citation; 0.3 = sparse + live-fetch dominant. 5-idea fixture suite, manually-graded ±0.15 on 4/5. |
| **C3** | **S20-C-CLASSIFIER-01** | ai-ml | S | Query-time category classifier (gpt-4o-mini, ≤$0.001/call) wired in `workflows.ask` and `orchestration/basic.py` entry. 50-query labeled fixture: top-1 ≥0.75, top-3 recall ≥0.92. Categories imported from canonical enum (Pattern A). |
| **C4** | **S20-C-ENRICHMENT-WRITER-01** | ai-ml + sw-eng | M | New `gecko_core/flywheel/enrichment.py`: concat `query+verdict.summary+cited_chunk_texts`, embed via Voyage, search top-1 in same `(category, subcategory)`. **De-dup threshold cosine ≥ 0.93** → bump existing chunk's `usage_count` + append `query` to `metadata.queries[]` (cap 20). Else insert with `source=enriched_output`. Threshold env-tunable (`GECKO_ENRICH_DEDUP_COSINE`). |
| **C5** | **S20-C-CONFIDENCE-GATE-01** | ai-ml | XS | Only enrich when `verdict.confidence ≥ 0.6`. Lower-confidence outputs pollute the base. Threshold env-tunable, ratchet up as base densifies. |
| **C6** | **S20-C-REACHABILITY-TEST-01** | ai-ml | S | `tests/integration/test_enrichment_loop.py`: run `bb research` twice. Asserts (a) confidence emitted, (b) `cited_doc_ids` propagated to `usage_count` bumps, (c) enriched chunk visible in run-2 retrieval. Per `feedback_wedge_reachability_check` — every "X is wired" claim demands end-to-end audit. |
| **C7** | **S20-C-EVAL-LIVE-01** | ai-ml | S | Parallel `runner_live.py --enrichment-loop` records confidence, cited_doc_ids, enriched_chunk_id into `tests/eval/live_runs/`. Track over 5 runs: base density (chunk count by category), live-fetch ratio, mean confidence drift. 2 baseline runs before any threshold tuning. Closes `feedback_eval_harness_rag_gap` for the new pipeline. |

**Track C risks:**
- Model hallucinates doc IDs not in retrieval → post-validate, drop unmatched, log rate.
- Confidence collapses to single value → forced-spread instruction or swap min→mean.
- 0.93 dedup threshold from intuition → calibrate with 100 real queries post-S20.
- Enrichment-loop confound with retrieval changes → pin embedder + retrieval params; only enrichment varies.

---

## Cross-track dependencies

```
A1 ─┬─ A2 ─┬─ A3 ─┬─ A4 ─┬─ A5
    │      │      │      │
    │      │      └─ A6 ─┘
    │      │             │
C3 ─┘      │             │
            └─ C1 ─ C2 ─ C4 ─ C5 ─ C6 ─ C7

B1 ─ B2 ─┬─ B3 ─┬─ B5 ─ B6
         │      │
         └─ B4 ─┘
```

**Critical path (5d):** A1 → A2 → C1 → C4 → C6 + B1 → B3 → B5. Everything else parallelizes.

**Soft order:**
- Day 1: A1, B1, C3 in parallel.
- Day 2: A2, B2, C1.
- Day 3: A3 + B3 starts + C2.
- Day 4: A4 + B3/B4 in parallel + C4.
- Day 5: A5 + B5 + C5.
- Day 6: A6 + B6 + C6 + C7.

---

## Sprint total

**19 tickets, ~13 dev-days of work compressed via parallel tracks into 6 wall-clock days.**

Effort mix: 1×XS (A1, C5), 8×S, 9×M, 0×L. No XL — Pattern C eats the long pole here (contract tests + reachability), and we deliberately avoided one big "ship the whole flywheel" ticket.

---

## Out of scope (S21+)

- Quality-tier multiplier or token-only billing for Claude Opus calls. Today's flat-fee floor handles budget/balanced; quality needs a separate pricing surface.
- Manifest version 1.1 with `bundled_tokens_consumed` reporting headers (so agents see remaining bundle).
- pay.sh-side discovery telemetry (do they actually crawl us? how often?). Wait until B2 ships.
- twit.sh broadening to non-X social via Apify — explicitly deferred (locked: X-only for now).
- Drop legacy Supabase pgvector tables — wait for ≥3 days of doctor-green post-A5 before A5-followup `S21-DROP-PGVECTOR`.
- Quality / quality-tier multipliers, tiered bulk packs ($25, $100), team-dedicated retrieval quotas.

---

## Verification checklist

End-of-sprint smoke (after all tracks merge):

```bash
# 1. Doctor — Voyage live ping + Mongo dim verify both green
uv run gecko-mcp doctor --live

# 2. Mongo schema test
uv run pytest packages/gecko-core/tests/knowledge/ tests/integration/test_enrichment_loop.py -q

# 3. Manifest endpoint
curl -s http://localhost:8000/.well-known/agent-skills/index.json | jq '.skills | length'   # expect 12

# 4. x402 dispatch (stub mode)
RUN_LIVE_SKILLS=0 uv run pytest packages/gecko-api/tests/contract/ -q

# 5. End-to-end research with enrichment
uv run bb research --idea "S20 dogfood smoke" --tier basic --tier-preset budget
# expect: ≥1 cited_doc_id, confidence emitted, follow-up `bb research` on similar idea retrieves the enriched chunk
```

---

## Vertical-shaped index (the moat thesis)

The base partitions on **two orthogonal axes**:
- **Category** (knowledge dimension): market_intelligence, business_financial, investment_signals, product, technical_engineering, ai_ml, design_ux. From the original slides.
- **Vertical** (app domain): neobank, dex, marketplace, prediction_market, indexer, b2b_saas, consumer_social, ai_agent_platform, gaming, infra_devtool. **NEW** — added 2026-05-06.

Every chunk lives in exactly one `(vertical, category)` cell. Retrieval filters by `(vertical, category)` first, then runs vector search inside the cell.

**Why this is the moat (not just verdict shape):**

1. **Vertical-shaped index ≠ pay.sh's catalog.** pay.sh's categories (compute, finance, messaging, …) are *what services do*. Gecko's verticals are *what apps the user is building*. Orthogonal. pay.sh has zero incentive to vertical-index; their economics are transaction-volume across horizontals.
2. **Build-time consumption pattern.** When a builder commits to "I'm building a neobank," their build agent queries Gecko on every step (schema design, regulatory check, pricing analog, BaaS integration). High call volume per builder, not a one-shot pre-idea call.
3. **Compounding density per (vertical, category) cell.** Every neobank builder either (a) hits cached chunks (cheap, fast) or (b) extends the base (their query enriches the cell). Replicators face cold starts in every vertical we've already filled.
4. **Switching cost on agent integration.** Build agents wired to Gecko's structured store can't trivially swap to "raw pay.sh + organize-yourself" without losing the persistent context. They're consuming the *organization*, not just the data.

### Pioneer's tax — the seed economics

The **first builder per (vertical, category) cell** triggers heavy live-fetch (Tavily + twit.sh + Bazaar + pay.sh) to seed the cell. Estimated cost per cell-seed: **$5–10** of upstream queries. Subsequent calls in that cell hit the cache: ~$0.001 retrieval cost.

**A7 instruments this signal** (`pioneer_call=true` in response). The **pricing choice is open** (see §Open decisions before sleep).

### Concrete example — neobank vertical (worked through with the user)

A neobank builder consumes this slice over a typical build:

| Cell | Example chunks |
|---|---|
| `neobank × business_financial` | BaaS providers (Synapse, Treasury Prime, Unit, Lithic), capital reqs by jurisdiction, unit economics (interchange split, deposit float yield), pricing structures, comparable cap tables (Nubank, Brex, Mercury) |
| `neobank × investment_signals` | Recent BR/LatAm fintech funding rounds, DD red flags, strategic acquirers + integration patterns |
| `neobank × technical_engineering` | Card-issuing API patterns, KYC/AML integration, ledger architectures, banking-core options, settlement/payment-rail patterns (PIX, ACH, SEPA, USDC) |
| `neobank × regulated` (open Q — should regulated be a Category or part of business_financial?) | BCB/CVM rulings, FinCEN MSB, EU Payment Institution rules |
| `neobank × product` | JTBD by segment, activation funnel benchmarks, onboarding friction patterns |
| `neobank × ai_ml` | Fraud-scoring patterns, transaction categorization models, fintech-specific support agents |
| `neobank × design_ux` | Compliance-friendly disclosure patterns, card design + BIN restrictions, mobile-first onboarding |

That's roughly **40–60 categorized chunks per vertical**, ingested once, queryable forever.

---

## Open decisions before sleep

Three blockers for Track A start. Two more I'd like answers on but can defer.

### Blocking (must answer before A1/A2 ships)

1. **Initial Vertical list.** I seeded 11 verticals in A1 (neobank, dex, marketplace, prediction_market, indexer, b2b_saas, consumer_social, ai_agent_platform, gaming, infra_devtool, unknown). **Are these the right v1 set?** Missing obvious candidates: `defi_lending`, `nft_marketplace`, `infra_rpc`, `wallet_tooling`, `chain_abstraction`, `data_analytics`, `social_token`. Recommendation: add `defi_lending`, `wallet_tooling`, `chain_abstraction` (these come up frequently in Solana ecosystem); rest can land via S21 expansion.

2. **Pioneer's tax pricing.** When a call seeds a `(vertical, category)` cell that costs us $5–10 in upstream live-fetch, three options:
   - **(a) Eat the cost.** Loss-leader for the first builder per cell. Margin compounds on subsequent builders. Bear case: someone seeds 50 verticals overnight to drain us.
   - **(b) Charge transparent premium.** First builder per cell pays $5 flat (vs $0.10 normal) with a header `X-Pioneer-Surcharge: true`. Manus-class agents will read this and decide.
   - **(c) Token-meter overage.** Bundled tokens are tighter for pioneer calls (5K vs 100K), so live-fetch consumption ticks the overage meter; user's bulk credit absorbs it transparently.
   - **My recommendation: (c).** Cleanest UX, no surprise charges, the credit-pack consumer doesn't care; the pay-per-call user sees a slightly higher effective rate on cold cells but that maps to actual cost. (a) is too easy to drain, (b) is hostile UX.

3. **Vertical detection on `unknown`.** A8 ticket asks: when classifier returns `vertical=unknown` (low confidence), do we refuse / fall back / prompt? Recommendation: **fall back to cross-vertical retrieval** for V1, prompt the user via response payload (`detected_vertical: "unknown", suggest_override: ["neobank", "marketplace"]`). User's next call passes explicit `vertical=neobank` and we cache it.

### Defer-able (nice to have, not blocking)

4. **Is `regulated` a Category or a slice of `business_financial`?** The neobank example surfaced regulated content (BCB, FinCEN, EU). Currently the 7 categories don't include `regulated`. Two options: (a) add as 8th Category, (b) put under `business_financial` subcategory. Recommendation: **(b)** — keeps the canonical 7 stable. Regulated lives in `business_financial.regulatory`.

5. **First-class build-context endpoint.** Today's MCP tools (`gecko_research`, `gecko_ask`) are oriented around pre-ideation. The build-time consumption pattern needs `gecko_build_context(vertical, category, query)` or similar. Recommendation: **add to S21**, not S20 — we need first to validate the vertical-shaped index works before exposing a new tool surface.

---

## Decision log

- **2026-05-05** — pivot from "judgment as commodity" to "knowledge as commodity" (memory `project_knowledge_as_commodity_pivot`).
- **2026-05-05** — pricing model locked: flat per-call + token bundled + overage + bulk credit pack. 12 skills total.
- **2026-05-06** — twit.sh = X-specific (Apify deferred).
- **2026-05-06** — confidence = LLM self-rating (citation-count deferred until judge corpus is mature).
- **2026-05-06** — `usage_count` bumps on **citation in output**, not retrieval.
- **2026-05-06** — no voice-prompt refactor; new teams are additive, current 5-voice advisor stays.
- **2026-05-06** — verbosity smoke against local gecko-api passed clean. Proceed.
- **2026-05-06** — production smoke session `6afd55d7` verified end-to-end on v0.2.12. Surviving dissents motivate this S20 plan.
- **2026-05-06** — repositioned to "build-context layer" (NOT "judgment as commodity" or "knowledge as commodity" externally). Wedge sentence above. Subscription pricing explicitly rejected (x402 is per-call by protocol; bulk credit pack is the only prepay shape).
- **2026-05-06** — moat thesis sharpened: vertical-shaped index + build-time consumption + compounding per cell + switching cost on agent integration. Categorized base alone is NOT the moat; it's all four together.
- **2026-05-06 (late)** — answers from owner brainstorm:
  - Verticals: ship A1 with 11 seeded; expand incrementally. No upfront lock.
  - Pioneer's tax: **explicit surcharge** on first call per `(vertical, category)` cell (option b). Transparent — calibrated from real first-run costs, not silent overage. Reasoning: "pay more on the first, as if the user were using pay.sh which charges every call."
  - Vertical detection: predefined set + classifier on top. If `unknown`, surface candidate set and user picks. **NO cross-vertical fallback.**
  - `regulated`: subcategory of `business_financial`. Canonical 7 stable.
  - `gecko_build_context(vertical, category, query)` MCP tool: **promoted to S20** (was S21). Add as Track D-or-extend Track C.
  - State-of-the-art RAG architecture: data-engineer + staff-engineer (data-architect lens) commissioned to design hybrid search + chunking strategy + pre-filtering + reranking + memory layer using MongoDB Atlas Vector Search + Voyage AI. Reference material: MongoDB Atlas RAG tutorial, MongoDB-AI training tracks (RAG with MongoDB, Memory for AI Applications, Voyage AI with MongoDB, Vector Search Performance).
