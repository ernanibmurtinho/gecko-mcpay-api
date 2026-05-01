# Sprint 15 — Profile registry seam + reputation ledger

**Status:** ready to fire (after Sprint 14 lands)
**Predecessor:** Sprint 14 (pulse v1 + Paragraph + publish.new + twit.sh×Colosseum + S12.5 test policy adoption)
**Driver:** `docs/strategy/profile-thesis-synthesis-2026-05-01.md` + `docs/strategy/profile-thesis-dogfood-2026-05-01.md`

**Done = profile-typed contributor machinery exists end-to-end (registry → identity ledger → RAG scoring → citation surface → founding-contributor program), but is dormant behind feature flags until Sprint 16 wires the routing layer. Stub-mode `bb research` byte-identical to pre-S15.**

---

## Why this sprint, why now

The user's commoditized-knowledge thesis was validated by 6 specialist lenses + a self-dogfood. Convergence: profiles are an **orthogonal axis** to SourceProvider + X402Client. Don't collapse them. Three-sprint arc S15-S17:

- **S15 (this sprint):** seam prep — registry, ledger, scoring, citation surface. All dormant.
- **S16:** wire routing — first 5 investors onboard via `gecko profile init`. Profiles light up.
- **S17:** chain mirror — EAS attestations on Base. Apex copy earns "profile-typed orchestration."

S15 is the cheap way to land it. Two-way reversibility on most tickets; one-way schema migration is the only commitment.

---

## Tracks

### Track A — ContributorRegistry Protocol (`S15-ARCH-01`) **CRITICAL**

Per `docs/strategy/profile-thesis-staff-engineer-2026-05-01.md`. The seam every other ticket builds on.

- **S15-ARCH-01 — `ContributorRegistry` Protocol + stub backend.**
  - Add `packages/gecko-core/src/gecko_core/contributors/{__init__.py,protocol.py,stub.py,factory.py}` mirroring `gecko_core.payments` shape (Sprint 13 S13-PAY-01)
  - Protocol: `name`, `mode: Literal["stub", "live", "cached"]`, `async resolve(address) -> ContributorProfile`, `async list_by_profile_type(...) -> list[ContributorProfile]`
  - Stub backend: in-memory dict seeded from a fixture of 5 known publish.new authors with hand-classified `profile_type` ∈ {judge, investor, pm, designer, security, founder, operator}
  - Wire into `gecko-api` `/openapi.json`: read-only `GET /contributors/{address}` returning `{address, profile_type, citation_count, sources[]}`
  - **No `X402Client` imports in `contributors/`** (boundary check)
  **Owner:** staff-engineer + software-engineer
  **Acceptance:** `gecko-mcp doctor` reports `contributor_mode=stub`; `bb research --idea "smoke"` byte-identical to pre-S15; OpenAPI contract change posted to gecko-mcpay-app.

### Track B — Identity ledger + outbox pattern (`S15-IDENTITY-*`) **CRITICAL**

Per `docs/strategy/profile-thesis-web3-engineer-2026-05-01.md` + dogfood CTO finding.

- **S15-IDENTITY-01 — Postgres reputation ledger.**
  - New tables: `contributor_attestations` (issuer, subject, profile_type, weight, evidence_url, issued_at), `contributor_reputation` (computed view: subject → bucketed_band)
  - Bucketed bands: `emerging` / `established` / `senior` (per PD's anti-gaming flag — never expose raw score)
  - 180-day reputation half-life on attestations (web3 memo)
  - Three-tier verification: self → observed → peer-quorum (only Tier-1+ counted in routing weight; Sprint 16 enforces)
  - **No chain writes this sprint.** Mirror to EAS lands S17.

- **S15-IDENTITY-02 — Outbox pattern for atomic Postgres→EAS sync.**
  Dogfood CTO finding (`profile-thesis-dogfood-2026-05-01.md`). Without it, when chain mirror lights up in S17, Postgres ledger and EAS attestations can drift. Two-line schema add now (S15) prevents weeks of drift remediation.
  - New table `contributor_attestations_outbox` mirrors `contributor_attestations` schema + `synced_at TIMESTAMPTZ NULL` + `tx_hash TEXT NULL`
  - Insert into outbox on every attestation write (Postgres trigger or app-side)
  - **Sync worker is S17.** S15 just records the outbox so S17 has data to sync
  **Owner:** web3-engineer + data-engineer
  **Acceptance:** every attestation write produces an outbox row; outbox table empty otherwise; no chain RPC calls.

### Track C — Schema for the contributor index (`S15-DATA-*`) **CRITICAL**

Per `docs/strategy/profile-thesis-data-engineer-2026-05-01.md`.

- **S15-DATA-01 — Three new tables + windowed RPC.**
  - `contributors` — primary key on (address, chain). Columns: address, chain, display_name, created_at.
  - `contributor_attributions` — joins to `chunks`. Columns: chunk_id (FK), contributor_address, contribution_evidence_url, attributed_at.
  - `contributor_profile_tags` — multi-source tag table beats fixed enum. Columns: contributor_address, profile_type, source ('self' | 'derived' | 'verified'), confidence, tagged_at, tagged_by.
  - RPC `match_chunks_by_profile(query_embedding vector(1536), profile_type TEXT[], window_days INT)` for profile-filtered similarity search
  - Migration `infra/supabase/migrations/<date>_contributor_registry.sql`
  - Backfill from existing `gecko_precedent.user_id` (data-eng memo)
  **Owner:** data-engineer
  **Acceptance:** migration applies idempotently; RPC returns expected results on synthetic fixture data; existing `chunks` queries unchanged.

- **S15-DATA-02 — Citation count materialized view.**
  Refreshed on `gecko_precedent` insert (verdict-time), not on cite-time. Cite-time fires too often to be a trust signal.
  - MV `contributor_citation_counts`: address, citation_count, build_count, kill_count, refine_count, ship_outcome_rate
  - Trigger refreshes on `gecko_precedent.outcome` update (Sprint 9 S9-PRECEDENT-01 column)
  **Owner:** data-engineer
  **Acceptance:** MV exists, refreshes correctly, queryable at <100ms.

### Track D — Profile-aware RAG scoring (`S15-AIML-*`) **HIGH**

Per `docs/strategy/profile-thesis-ai-ml-engineer-2026-05-01.md` + dogfood adds.

- **S15-AIML-01 — Profile-aware RAG scoring + 50-fixture suite.**
  - Hybrid log-linear scorer: `score = α·cosine + β·log(cited_count) + γ·outcome_rate + δ·recency`
  - Two-stage retrieval: BM25 → dense rerank
  - Coefficients eval-tuned, NOT hand-set
  - 50 fixtures × 6 profile types in `tests/eval/fixtures/profile_rag/`
  - **Flag-gated** behind `PROFILE_RAG_ENABLED` (default off)
  **Owner:** ai-ml-engineer
  **Acceptance:** ≥0.70 top-3 profile-type match on the fixture suite; no holdout regression with the flag off; flag-on holdout ≥0.80.

- **S15-AIML-02 — `platform_anti_pattern` critique extension.** **(NEW from dogfood)**
  Neither basic tier nor 5-voice panel caught the reputation-gaming risk in the dogfood (`profile-thesis-dogfood-2026-05-01.md`). Critic agent's prompt library doesn't have "platform-design anti-pattern awareness."
  - Add a critic-agent prompt suffix that explicitly checks for: leaderboard gaming, reputation laundering, sybil farming, echo-chamber selection bias, network-effect lock-in, fee-extraction monopoly, content moderation moral hazard
  - 20-fixture `platform_anti_pattern` suite: each fixture proposes a platform design containing one anti-pattern; critic must name it
  - **Acceptance test:** re-run `bb research` on the profile-thesis from `2026-05-01-commoditized-knowledge-thesis.md` after S15-AIML-02 ships → critic must name "public leaderboard" as a gaming risk
  **Owner:** ai-ml-engineer

- **S15-AIML-03 — Retire gpt-4.1-nano from staff_manager voice.** **(NEW from dogfood)**
  Closing-line failure on staff_manager occurred in Sprints 9, 11, 12, AND the 2026-05-01 dogfood. Sprint 9 fix never fully covered nano-class models. Either swap to `gpt-4o-mini` or extend the strict-suffix retry pattern model-specifically.
  - Recommended: swap to `gpt-4o-mini` (small cost increase, big quality lift)
  - If swap rejected: implement nano-specific retry with stricter suffix template
  **Owner:** ai-ml-engineer
  **Acceptance:** 5 successive `bb plan` calls with the staff_manager voice produce closing lines on every run.

### Track E — Citation surface for profiles (`S15-PROFILE-CITE-*`) **MED**

Per `docs/strategy/profile-thesis-product-designer-2026-05-01.md`.

- **S15-PROFILE-CITE-01 — Extend `Citation` with `profile_type` + `reputation_band`.**
  - Add to `gecko_core.models.Citation`: `profile_type: Optional[str]`, `reputation_band: Optional[Literal["emerging", "established", "senior"]]`
  - Update `apps/cli/src/gecko_cli/render.py` `_citations_renderable` (lines 102-140 per PD memo): sub-line gains `profile_type · reputation_band`. Five-field ceiling on the sub-line. On wrap, drop wallet then payout, never profile_type or reputation.
  - "Weighed in ─── 3 judges · 2 investors · 1 PM" line above Sources in validation panel (PD memo)
  - Drill-down behind `gecko show --profiles <session_id>` (separate CLI subcommand, not in render.py)
  - **Bucketed bands only.** Never raw float. (PD anti-gaming flag.)
  **Owner:** product-designer + software-engineer
  **Acceptance:** byte-identical pre-S15 rendering when profile fields null; 80-col snapshot test; three-token band; absent "Weighed in" line when zero profiles cited.

### Track F — Founding Contributor Program spec (`S15-BIZ-*`) **HIGH**

Per `docs/strategy/profile-thesis-business-manager-2026-05-01.md`.

- **S15-BIZ-01 — Founding Contributor Program spec + 50-investor target list.**
  - Spec doc: target profile = pre-seed/seed-stage VC investors with public Substack or Mirror presence
  - $25k seed budget = 50 investors × $500 each as 6-month retainer
  - Revenue split: **80/15/5 (contributor/Gecko/operations)** for founding cohort; later cohorts move to **70/20/10**
  - $300/mo guaranteed minimum for 6 months → mitigates contributor churn before liquidity
  - Target list: 50 named investors with handles + outreach owner + status field
  - GTM sequence: cold-email outreach → onboarding flow → first cite within 30 days → publicly announced cohort
  **Owner:** business-manager
  **Acceptance:** spec lands at `docs/strategy/founding-contributor-program-spec.md`; 50-name list with non-public outreach status; legal review on the retainer agreement (separate ticket).

---

## Out of scope

- Wiring profile-typed routing into the orchestrator (S16)
- EAS chain mirror (S17)
- Bull-bear pair picker (Sprint 16 candidate per dogfood PM finding)
- Apex copy update for "profile-typed orchestration" sub-fold (S17 — earned by signal: 3+ profile types cited consistently over 7 days)
- Subscription pricing test on contributor side (S17+ — separate from per-call markup which lands earlier)

## Acceptance (sprint-level)

- [ ] `ContributorRegistry` Protocol + stub backend ship; OpenAPI contract change posted
- [ ] Three new tables + outbox + MV all migrated
- [ ] Profile-aware RAG scoring lands behind `PROFILE_RAG_ENABLED=false` (default off); holdout green
- [ ] `platform_anti_pattern` critique catches "public leaderboard" in the re-dogfood
- [ ] `staff_manager` voice produces closing lines on 5/5 runs
- [ ] Citation surface extends with profile fields; pre-S15 render byte-identical when null
- [ ] Founding Contributor Program spec + 50-name list committed
- [ ] **Stub `bb research` byte-identical to pre-S15 (smoke regression check)**

## Test plan

After all tracks land, dogfood the same way:
1. `bb research --idea "Gecko is the orchestrator of profile-typed paid expertise..."` (the same thesis from `2026-05-01-commoditized-knowledge-thesis.md`) under stub mode
2. Compare verdict + critic output to the 2026-05-01 dogfood baseline
3. **Acceptance:** critic now names "public leaderboard" as a gaming risk (S15-AIML-02 fix); profile_type fields visible in citations (S15-PROFILE-CITE-01); reputation_band absent because no real attestations exist yet (correct behavior — registry is dormant)

## Reference

- `docs/strategy/profile-thesis-synthesis-2026-05-01.md` — the synthesis driving this sprint
- `docs/strategy/profile-thesis-dogfood-2026-05-01.md` — dogfood adds (S15-AIML-02, S15-IDENTITY-02 outbox, S15-AIML-03 nano retire)
- 6 lens memos under `docs/strategy/profile-thesis-{role}-2026-05-01.md`
- Sprint 14 plan (`docs/build-plan-sprint-14.md`, commits `63442bd` + `ede509b`)
