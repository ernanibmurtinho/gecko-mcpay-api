# External Sources Survey — Gecko Pro Pre-Analysis Hooks
**Date:** 2026-04-28
**Status:** Decision doc — pick V1 sources, then plan build
**Author:** staff-engineer

---

## 1. Executive Summary

- **Ship in V1:** Colosseum Copilot (crypto-shape only), Internal Gecko Flywheel (always-on), HackerNews Algolia + Reddit JSON (SaaS/indie-shape only). All free or near-free, all under $0.02 added COGS.
- **Defer judge-posts to V2 — but seed a static judge corpus in `gecko-mcpay-skills` now.** X API is pay-per-use as of Feb 2026 ($0.005/read, no free tier, 7-day window); a live ingest blows our COGS cap and gives weak signal. A hand-curated markdown corpus of ~10 Solana judges' public rubric threads delivers 80% of the value at $0 per session.
- **Internal Gecko flywheel is the highest-leverage long-term source we have.** Already in Supabase. Cost to wire up: a single embedding pass + retrieval call. This is the moat.
- **frames.ag-as-data-broker is worth a coordination conversation, not a V1 dependency.** They have wallet history + social graph + spend pattern that nobody else can give us. Identify the asks; don't block on them.
- **MongoDB stays unused for now.** Supabase pgvector handles every corpus we'd add in V1. MongoDB earns its place only when we have a write-once-read-many cache that doesn't need to JOIN with sessions (e.g. a Tavily extract cache at scale, or a regulatory snapshot corpus).
- **Auto-detect by keyword sniff is the right UX.** Tier-gate the heavy ones (Colosseum + flywheel = Pro; regulatory + on-chain = a future Premium). No explicit `sources=` flag in V1 — it's surface area we don't need.
- **Per-session COGS budget for new sources: ≤ $0.02.** That keeps Pro at >70% margin and leaves headroom for an eventual Premium SKU.

---

## 2. Ranked Options Table

| # | Source | Signal it adds (vs. Tavily) | Cost | Auth | ICP | Effort | Lift | Verdict |
|---|---|---|---|---|---|---|---|---|
| 1 | **Colosseum Copilot** | 5,400 real Solana hackathon comparables, winner-gap ML clusters, Colosseum analytics | Free (BYO PAT, user-owned) | Per-user PAT | Crypto / hackathon team | S | High | **V1** |
| 2 | **Internal Gecko flywheel** (own past ResearchResults) | Verdict precedent on similar ideas — the only source competitors can't have | ~$0.001 embed/session | Service role (internal) | All | S | High | **V1** |
| 3 | **HackerNews Algolia search** | Show-HN history + comment sentiment by category — what real builders said when X launched | Free, no auth | None | Indie dev / SaaS | XS | Medium | **V1** |
| 4 | **Reddit JSON (unauth, read-only)** | Sub-specific demand signal (r/SaaS, r/SolanaDev, r/IndieHackers) | Free, ~10 req/min unauth | None (UA header) | Indie dev / SaaS / crypto | XS | Medium | **V1** |
| 5 | **GitHub topic + trending API** | "Is anyone actually building in this space?" — repo count, star velocity, recent commits | Free, 5k/hr authed | API key (single, ours) | Dev tools / infra | S | Medium | **V1** (cheap, high-fit) |
| 6 | **Static judge corpus in `gecko-mcpay-skills`** | ~10 Solana judges' rubric threads, hand-curated, embedded once | Free | None | Hackathon team | S | High | **V1** (replaces live X) |
| 7 | **Helius DAS / Solana RPC** | On-chain activity for crypto idea space (token holders, NFT mints, program calls) | Free tier 2 req/s DAS | API key | Crypto only | M | Medium | **V2** |
| 8 | **DefiLlama** | TVL, protocol category sizing — "is this DeFi vertical real" | Free, no auth | None | Crypto DeFi only | XS | Medium | **V2** (narrow ICP) |
| 9 | **Live X / Twitter via frames.ag proxy** | Real-time judge threads, trend signal | Conditional on frames.ag | OAuth via frames.ag | Hackathon team / crypto | M | High *if available* | **V2 — pending frames.ag** |
| 10 | **Wellfound / AngelList job postings** | Hiring as demand proxy — "5 startups hiring for X = market is real" | Free tier exists, throttled | API key | SaaS / B2B | M | Medium | **V2** |
| 11 | **Devpost** | Multi-ecosystem hackathon submissions | Scrape only — no public API | Scrape (against rules) | Hackathon team | L | Low | **Never** (no API, scraping banned) |
| 12 | **ETHGlobal** | Multi-chain hackathon comparables | Scrape only | Scrape | Crypto (non-Solana) | L | Low | **Never** |
| 13 | **ProductHunt** | Launch traction, vote distribution | Free GraphQL (rate-limited) | OAuth | SaaS launches | M | Low-Med | **V2** (nice-to-have, low signal density) |
| 14 | **npm download stats / Stack Overflow tags** | "Is this technology actually used?" | Free | None | Dev tools | S | Low-Med | **V2** |
| 15 | **USPTO patent search** | IP defensibility / prior-art signal | Free | None | Regulated / hardware | M | Medium (narrow) | **V2** (Premium tier) |
| 16 | **SEC EDGAR** | Public co. signal for fintech/finance ideas | Free | None | Fintech | M | Low | **V2** (rare ICP) |
| 17 | **FDA / FAA / DEA registries** | Regulatory feasibility for vet-rx / clinical / aviation | Free, varies | None | Regulated vertical | L | High *for narrow ICP* | **V2** (Premium tier) |
| 18 | **Crunchbase free tier** | Funded competitor list | Severely throttled | API key | All | M | Low | **Never** (free tier useless) |
| 19 | **Indie Hackers forums** | Solo-founder pain signal | Public API gone; archive only | Scrape | Indie dev | L | Low | **Never** |
| 20 | **Live X / Twitter direct (no proxy)** | Same as #9 but priced | $0.005/read, 7-day window | API key + $$ | Hackathon team | M | High | **Never** at our price point |
| 21 | **Internal `session_costs` cluster analysis** | "Which idea categories are users repeatedly paying to research?" | Free (already in DB) | Service role | Internal product signal | S | Med (PM tool, not pre-analysis) | **V2** |
| 22 | **Discord public servers (Solana SE etc.)** | Live community Q&A | High friction, ToS gray | Bot token | Crypto | L | Low | **Never** |

---

## 3. Composite V1 Recommendation

**Three sources ship for V1: Colosseum Copilot, Internal Gecko Flywheel, and the HN/Reddit free-tier pair.** Plus the static judge corpus in `gecko-mcpay-skills` (which is technically a content-PR, not an integration).

The bar: *"would the user pay an extra $0.10 per session for THIS source's signal alone?"*

- **Colosseum Copilot — YES.** A hackathon team pitching a Solana DeFi idea would pay $0.10 to know there are 47 prior submissions in the cluster, what the winners did differently, and which problem statements repeatedly lose. This is the pitch.
- **Internal Gecko Flywheel — YES (compounding).** Worth nothing on day 1, worth a lot on day 90. The marginal cost is ~$0.001 (one embed + one similarity query), so we ship it free and the value accrues silently.
- **HN + Reddit — YES (in aggregate).** Either alone is "nice"; together they replace the user's manual "let me just search HN and r/SaaS for an hour" step. Combined cost: zero, both unauth.

### Integration sketch — Colosseum Copilot

- **Where:** Parallel to Tavily during discovery phase. Fire iff `idea_classifier(idea) == "crypto"`.
- **BYO key:** User PAT, stored encrypted on `users` row. Opt-in checkbox on Pro upgrade.
- **Failure mode:** 5xx or missing PAT → log warning, proceed without. Never block the session.
- **Persisted on ResearchResult:** Yes — `result.colosseum_context` (JSON: cluster_id, top_5_comparables, winner_gap_summary). Cited in PRD output.

### Integration sketch — Internal Gecko Flywheel

- **Where:** Pre-discovery. Embed the new idea, retrieve top-5 prior `(idea, verdict, comparables)` tuples by cosine similarity above threshold 0.78.
- **BYO key:** N/A — service role.
- **Failure mode:** Empty result set is the common path on day 1. No-op, proceed.
- **Persisted on ResearchResult:** Yes — `result.gecko_precedent` (top-5 prior verdicts on similar ideas, with anonymized idea hashes + verdict + summary). Also feeds Judge agent's `rag_context`.
- **Privacy:** Idea text NOT shared cross-user. We embed the idea + persist a short LLM-generated 1-sentence "category summary"; that summary is what gets retrieved. Verdict + comparables list are returned. Original idea text stays scoped to its owning user via RLS.

### Integration sketch — HN + Reddit

- **Where:** Parallel to Tavily, always on. Two `httpx.AsyncClient` calls.
- **BYO key:** None (unauth). Add a cron-monitor for rate-limit health.
- **Failure mode:** 429 or 5xx → skip silently. Tavily covers the gap.
- **Persisted on ResearchResult:** Yes — `result.community_signal` (top 5 HN threads + top 5 Reddit threads, scored by relevance).

---

## 4. The Judge-Posts Angle — Load-Bearing

**Headline call: ship a curated static judge corpus in V1, defer live ingest to V2.**

### Why static, not live

X API as of Feb 2026: pay-per-use only, ~$0.005/read, 7-day search window, no free tier. A real judge-network crawl (10 seed judges × ~50 recent threads each × ~$0.005) is ~$2.50 per refresh. Even refreshing weekly that's fine — but per-session live search to find "what did judges say about ideas like THIS one" easily hits 20+ reads = $0.10+, blowing our COGS budget on a single source. Worse: judge posts are sparse and noisy; the 7-day window means we'd often find nothing.

### V1: Static judge corpus

- **Format:** Markdown files in `gecko-mcpay-skills` repo, one per judge. Schema:
  ```yaml
  ---
  judge: Mert (Helius)
  handle: '@0xMert_'
  sectors: [infra, RPC, devtools]
  active: true
  last_updated: 2026-04-28
  ---
  ## Stated rubric (in their words)
  - "I scored this kill because..." [link]
  - ...
  ## Notable kill calls
  ## Notable ship calls
  ## Pet peeves / dealbreakers
  ```
- **Seed list (~10):** Mert (Helius), Anatoly (Solana), Akshay BD, Austin Federa, Lily Liu, plus 3-4 Colosseum partner judges, plus 1-2 YC partners with public Solana threads. User curates initial list manually — staff-engineer call.
- **Refresh cadence:** Manual, monthly. Not a CI job. Markdown PRs.
- **Ingest:** On Gecko boot or skill-update, embed each judge file into Supabase `judge_insights` table. ~$0.001 one-time per refresh.

### How it feeds the system

- **Input to Judge agent:** Pre-debate, retrieve top-3 judge entries by cosine similarity to the idea's category vector. Inject into Judge agent's system prompt as `rag_context.judge_calibration`. Prompt becomes: *"Calibrate your scoring against these real judges' stated rubrics. Mert kills infra ideas that don't have a clear differentiation from existing RPC providers; Anatoly ships ideas that compress on-chain state. The current idea is X — apply this calibration."*
- **Output on ResearchResult:** `result.judge_landscape` — `[{judge, handle, relevance, what_they'd_likely_say}]`. User sees: *"3 real Solana judges have stated rubrics that overlap with your idea's category. Mert tends to kill ideas like this when the differentiation is `lower latency`; you should preempt that in your pitch."*

### Storage decision

**Supabase, not MongoDB.** Reason: this corpus needs to JOIN with embeddings + be retrieved alongside `gecko_precedent` and `colosseum_context` in a single retrieval pass. It's small (~50KB embedded), changes monthly, and slots into our existing pgvector setup with zero new infra.

### Cost: ~$0.001 per session

Just the retrieval + injection. The embedding cost amortizes across all sessions in the month.

### V2 upgrade path

If frames.ag exposes an authenticated X search proxy at zero per-call cost (see §5), wire live ingest to refresh the corpus daily. The schema above doesn't change; the ingest source does.

---

## 5. The frames.ag-as-Data-Broker Angle

frames.ag uniquely knows:

1. **User's verified wallet + on-chain history** — have they shipped programs on Solana? Deployed an NFT collection? Hold positions in DeFi?
2. **Connected social accounts** — X handle, GitHub, Farcaster. Whether they have public hackathon history.
3. **x402 spend pattern across the frames.ag ecosystem** — are they a serial agent-tool buyer (= sophisticated user) or first-time?

### Plausible asks (ranked by likelihood frames.ag says yes)

1. **Likely yes — `GET /v1/user/context`** with bearer token. Returns: connected handles (X, GH), wallet pubkey, ENS-style display name, account age. They probably already have this internally for their own dashboard.
2. **Maybe — `GET /v1/user/onchain-summary`** — abstracted "this user has deployed N programs / minted N collections / has $X TVL across protocols." They'd source from Helius themselves. They might gate this behind explicit user consent.
3. **Probably no in V1, worth asking — X search lane.** "Authenticated X search via your platform credentials." Big win if yes; we suspect they wouldn't subsidize this for free given X's pay-per-use model. **Decision-blocker: ask them.**
4. **Probably no — social-graph-of-graph.** "Show me what THIS user's followers are talking about." Privacy-fraught; they'd say no.

### What we'd do with #1 and #2

Inject into the **Researcher agent** as `user_context.builder_profile`. Prompt addition: *"This user has deployed 3 Solana programs, primarily in NFT tooling. Calibrate your competitive analysis to assume technical capability and existing audience in NFT space."* This shifts the Judge from "would a hypothetical builder ship this" to "would THIS builder ship this" — materially more accurate verdict.

### Coordination plan

Send a one-pager to frames.ag asking specifically for #1 and #2. Don't block V1 on it. If they ship by V2, it's a free upgrade.

---

## 6. The Internal Gecko Flywheel — Why It's the Moat

We already own it; we just haven't indexed it.

### Schema (lives in Supabase, no MongoDB)

```sql
-- new table; reuses existing pgvector setup
create table gecko_precedent (
  id uuid primary key default gen_random_uuid(),
  session_id uuid references sessions(id),
  idea_summary text not null,        -- 1-sentence LLM-generated category summary, NOT the user's verbatim idea
  idea_hash text not null,            -- sha256(normalized idea), for dedup
  category_tags text[] not null,      -- ['crypto','defi','infra']
  verdict text not null,              -- 'ship' | 'kill' | 'pivot'
  key_comparables jsonb not null,     -- top-5 comparables agents named
  embedding vector(1536) not null,    -- embed(idea_summary)
  created_at timestamptz default now()
);
create index on gecko_precedent using ivfflat (embedding vector_cosine_ops);
```

### Write path

After every Pro session completes:
1. LLM-generate a 1-sentence category summary from the original idea (stripped of identifying details — "an X tool for Y users in Z space").
2. Embed it.
3. Insert row with verdict + comparables-named.

### Read path

Pre-discovery on a new session:
1. Embed new idea.
2. `SELECT ... ORDER BY embedding <=> $1 LIMIT 5 WHERE 1 - (embedding <=> $1) > 0.78`.
3. Inject into AG2 group chat as `rag_context.gecko_precedent`.

### Privacy

- Original idea text never crosses user boundaries (RLS on sessions enforces).
- The category summary is what gets matched. By construction it's an abstracted description of the idea's *shape*, not its *content*. Two startups working on identical ideas would produce similar summaries; we accept that as fair competitive signal, the same way Y Combinator's anti-disclosure norms accept that "we both pitched a Stripe-for-X" is unavoidable.
- **Decision: ship without per-user opt-out in V1.** Make it a Pro tier feature. If a user objects, they can request deletion. Revisit at scale.

### Failed-then-succeeded session pairs

User flagged this as "calibration goldmine." Concrete plan: add `parent_session_id` column. When a user re-runs a similar idea (idea_hash similarity > 0.85 within 30 days), link the sessions. Build a periodic batch job that surfaces (kill→ship) deltas to a `calibration_set` table that the AG2 prompt-tuning script reads. **V2 work, not V1.** V1 just persists; V2 mines.

---

## 7. MongoDB vs Supabase — Per-Corpus Decision

**Primary vector store: Supabase pgvector. Confirmed. No migration.**

| Corpus | Where | Why |
|---|---|---|
| `gecko_precedent` (own past verdicts) | **Supabase** | Joins to `sessions` + `projects`, RLS-friendly, same access pattern as existing chunks |
| `judge_insights` (static judge corpus) | **Supabase** | Small, slots into pgvector, retrieved alongside other RAG sources |
| Colosseum response cache | **MongoDB** | Write-once-read-many, large opaque JSON blobs, no JOINs needed, TTL-based expiry. MongoDB's flexibility wins here |
| HN/Reddit response cache | **MongoDB** | Same — opaque cache, TTL, no JOIN |
| `colosseum_context` per session (final reference on result) | **Supabase** (`research_results` JSONB column) | Belongs to the result row, must JOIN |
| Future: regulatory snapshots (FDA/FAA/etc.) | **MongoDB** | Large, infrequently updated, document-shaped |

**Rule of thumb encoded:** *"If it JOINs to a session/user, Supabase. If it's a fat opaque cache, MongoDB."*

---

## 8. UX — How Users Opt In

**Recommendation: Auto-detect by keyword sniff, tier-gated.**

```python
# pseudocode in gecko-core
def select_sources(idea: str, tier: Tier) -> list[Source]:
    sources = [TAVILY]                       # always
    cats = classify(idea)                    # {'crypto', 'defi', 'saas', 'regulated', ...}

    if tier >= Tier.PRO:
        sources.append(GECKO_PRECEDENT)      # always for Pro
        sources.extend([HN, REDDIT])         # always for Pro
        if 'crypto' in cats:
            sources.append(COLOSSEUM)         # if user has PAT
            sources.append(JUDGE_CORPUS)
        if 'saas' in cats or 'devtools' in cats:
            sources.append(GITHUB_TOPICS)

    if tier >= Tier.PREMIUM:                 # future
        if 'regulated' in cats:
            sources.extend([FDA, FAA])
        if 'crypto' in cats:
            sources.extend([HELIUS_DAS, DEFILLAMA])

    return sources
```

**Why not explicit `sources=[...]` flags in V1:** Users don't know what to pick. The whole pitch is "pay $0.75 and we figure out what context to pull." Explicit flags are V3 power-user surface area.

**Why not pure tier-gating:** Pulling Colosseum for a vet-tele-rx idea wastes a request and adds latency for zero signal. Keyword sniff prevents that.

---

## 9. The "User Wouldn't Refuse to Try" Test

For each V1 source — the line that closes the sale:

- **Colosseum:** *"We checked the 5,400 Solana hackathon projects against your idea. 12 teams have shipped close variants — here's what 3 of them did wrong, and what the 1 winner did differently. Pitch with this in mind."*
- **Internal Gecko flywheel:** *"You're the 47th person to pitch a [category] idea to Gecko. Of the previous 46, 38 got killed and 8 got ship — here's what separated them. Your idea looks more like the kill cluster; here's why."*
- **HN + Reddit:** *"r/SaaS has 14 threads in the last 30 days complaining about [your problem]. r/IndieHackers has 6. The pain is real and it's getting louder, not quieter."*
- **Judge corpus (static, V1):** *"Mert from Helius and 2 other Solana judges have publicly stated rubrics that bear directly on your idea. Here's exactly what they'd say in a Demo Day Q&A — preempt these in your pitch."*

If the user reads the lines above and shrugs, the source doesn't ship. All four pass.

---

## 10. Open Questions for the User

1. **frames.ag coordination — go or no-go?** Should staff-engineer + web3-engineer draft the one-pager asking for `/v1/user/context` and `/v1/user/onchain-summary`? Timeline — before or after Monday demo?
2. **Judge seed list — who curates?** Hand-curated markdown corpus needs an initial list of ~10 Solana judges. Should `business-manager` produce the list, or do you want to seed it personally given your network?
3. **Internal flywheel privacy stance — opt-out by default, or no opt-out?** I recommend no opt-out in V1 (Pro tier flat-rate buys this), with deletion-on-request as the escape valve. Confirm.
4. **Premium tier exists or is fictional?** Section 8 references a hypothetical `$1.50` Premium SKU for regulatory + on-chain layers. Is that a real near-term plan or just notation? (Affects which V2 sources we wire stubs for.)
5. **GitHub topic search — single shared key or BYO?** Easier to ship with one Gecko-owned PAT (5k req/hr authed); easier to scale and impossible to abuse with BYO. I lean shared-key for V1, switch to BYO at scale. Confirm.

---

## Appendix — Cost Model for V1

Per Pro session (worst case, all sources fire):

| Source | Cost |
|---|---|
| Colosseum (BYO PAT) | $0 |
| Gecko flywheel (1 embed + 1 query) | $0.001 |
| HN Algolia | $0 |
| Reddit JSON | $0 |
| GitHub topics | $0 |
| Judge corpus retrieval | $0.001 |
| **Total added** | **~$0.002** |

Within COGS cap of $0.20. Session economics unchanged.
