# V1 Build Plan — External Sources (twit.sh + Flywheel + Free Corpora)

**Date:** 2026-04-28
**Status:** Build plan — follows from `docs/external-sources-survey.md`
**Author:** staff-engineer
**Supersedes:** Survey §4 (judge-posts angle) — twit.sh replaces static corpus
**Constraints:** ≤ $0.10 added COGS / Pro session; >85% margin at $0.75; no new infra beyond Supabase + MongoDB + ECS + ALB + Helius

---

## 1. Executive Summary

- **twit.sh is in for V1.** It's x402-native — same protocol Gecko already speaks. Direct HTTP calls from `gecko-core` using our existing x402 client; no Node runtime added to Fargate.
- **Static judge corpus is OUT.** Replaced by live twit.sh ingest of `@colosseum`, `@mattytay`, `@GuiBibeau` plus a category-keyed `searchTweets()` query. Costs $0.025-$0.05 per Pro session, well within the relaxed $0.10 added-COGS cap.
- **Six V1 sources, three cost classes.** Free always-on (HN, Reddit), free BYO-PAT (Colosseum, GitHub), internal flywheel ($0.001), and twit.sh paid ($0.05 ceiling).
- **Updated cost model:** worst-case added COGS per Pro session = $0.057. Pro at $0.75 retains 88% margin.
- **No frames.ag dependency in V1.** One-pager drafted; if they ship `/v1/user/context` in Sprint 3, we hot-swap to enriched persona prompts. Otherwise the path is twit.sh + Helius direct (premium tier territory).
- **Max tier deferred to Sprint 3.** Sized below: Max-S $2 / Max-M $5 / Max-L $15. All three keep margin >70%. Launching Max alongside V2 source rollout reduces SKU sprawl during the first paying-user signal window.
- **Internal flywheel locks in: no opt-out, deletion-on-request escape valve.** Privacy bounded to LLM-generated category summary; never verbatim idea text. CI guardrail enforced.

---

## 2. The 6 V1 Sources

| # | Source | Workflow position | Cost / session | Failure mode | Persisted on `ResearchResult` |
|---|---|---|---|---|---|
| 1 | **Colosseum Copilot** (BYO PAT, crypto-only, opt-in) | Parallel to Tavily during discovery; fires iff `classify(idea) ∋ crypto` AND user has PAT | $0 | Missing PAT or 5xx → log warn, skip | `result.colosseum_context` (cluster_id, top_5, winner_gap) |
| 2 | **Internal Gecko Flywheel** (no opt-out, all Pro) | Pre-discovery; embed → top-5 cosine > 0.78 retrieval | $0.001 | Empty result set is normal early; no-op | `result.gecko_precedent` (top-5 prior verdicts + summaries) |
| 3 | **HackerNews Algolia** (always on, free) | Parallel to Tavily | $0 | 5xx/429 → silent skip | `result.community_signal.hn` (top 5 threads) |
| 4 | **Reddit JSON unauth** (always on, free) | Parallel to Tavily | $0 | 5xx/429 → silent skip | `result.community_signal.reddit` (top 5) |
| 5 | **GitHub topic search** (BYO PAT, dev-tools/SaaS only, opt-in) | Parallel; fires iff `classify(idea) ∋ {saas, devtools}` AND user has PAT | $0 | Missing PAT/rate → skip | `result.github_signal` (repo_count, star_velocity, top_5) |
| 6 | **twit.sh live X search** (NEW; ~$0.05 ceiling; server-managed wallet) | Parallel; fires iff `classify(idea) ∋ {crypto, hackathon}` AND `TWITSH_ENABLED=true` AND wallet balance > $0.50 | $0.025-$0.05 | 5xx → skip; balance low → skip + alarm; cache hit → $0 | `result.x_signal` + `result.judge_threads` |

**Always-on baseline:** Tavily (existing), Flywheel (#2), HN (#3), Reddit (#4).
**Conditional, free:** Colosseum (#1), GitHub (#5).
**Conditional, paid:** twit.sh (#6).

### Worst-case Pro session cost ledger

| Item | Cost |
|---|---|
| Existing Tavily/embed/LLM (unchanged) | $0.10 baseline |
| Flywheel embed + query | $0.001 |
| Colosseum / HN / Reddit / GitHub | $0 |
| twit.sh (10 reads × $0.005 cache-miss avg) | $0.050 |
| Judge-thread distillation LLM call | $0.005 |
| **Total added by this plan** | **$0.057** |

Within the $0.10 cap. Pro margin at $0.75 = 88%.

---

## 3. twit.sh Integration — Load-Bearing Section

### 3.1 Architecture options

| Opt | Description | Pro | Con |
|---|---|---|---|
| (a) | `npx twitsh` invoked per session via subprocess | Simplest auth/discovery path; matches docs | Adds Node.js + npm to Fargate image (~150MB); subprocess latency 200-500ms; harder to instrument; CLI may prompt interactively |
| (b) | Direct HTTP to `https://x402.twit.sh/...` with our existing x402 client | Zero new runtime; reuses x402 client we already ship; clean async; instrument-friendly | Endpoint catalog discovery is CLI-only per current docs |
| (c) | **Hybrid: bake endpoint catalog at container build time (one-shot CLI in Dockerfile), runtime is direct HTTP** | Eliminates Node from runtime image; predictable; simple to test | Catalog goes stale between deploys (~1-2wk cadence); refresh on every deploy is acceptable |

**Recommendation: (c) Hybrid.**

### 3.2 How (c) works

**Build time (Dockerfile, multi-stage):**

```dockerfile
# Stage 1: discovery (Node)
FROM node:20-alpine AS twitsh-discovery
RUN npx --yes twitsh@latest endpoints --json > /tmp/twitsh_catalog.json || \
    echo '{"_fallback": true}' > /tmp/twitsh_catalog.json

# Stage 2: runtime (Python only)
FROM python:3.11-slim
COPY --from=twitsh-discovery /tmp/twitsh_catalog.json /app/twitsh_catalog.json
# ... rest unchanged
```

The catalog file ships into the image. If twit.sh changes endpoint URLs, next deploy refreshes. If `npx twitsh endpoints` fails at build, we fall back to a hardcoded minimal catalog (committed to repo as `packages/gecko-core/src/gecko_core/sources/twitsh_catalog_fallback.json`).

**Runtime (Python, in `gecko-core`):**

```python
# packages/gecko-core/src/gecko_core/sources/twitsh.py
from pathlib import Path
import json
from gecko_core.payments.x402 import X402Client  # existing

CATALOG = json.loads(Path("/app/twitsh_catalog.json").read_text())

class TwitshClient:
    def __init__(self, x402_client: X402Client, mode: str = "x402"):
        self._x = x402_client                                   # reuses Gecko's x402 lib
        self._base = "https://x402.twit.sh" if mode == "x402" else "https://mpp.twit.sh"

    async def search_tweets(self, query: str) -> dict:
        url = f"{self._base}{CATALOG['searchTweets']['path']}"
        return await self._x.fetch(url, params={"q": query})    # x402 retry-on-402 handled inside

    async def user_tweets(self, handle: str, limit: int = 20) -> dict:
        url = f"{self._base}{CATALOG['userTweets']['path']}"
        return await self._x.fetch(url, params={"username": handle, "limit": limit})
```

`X402Client.fetch()` already handles the 402 → sign payment → retry with `X-PAYMENT` header dance.

### 3.3 Wallet management

| Item | Spec |
|---|---|
| Wallet purpose | twit.sh payments only — separate from $20 USDC Pro recipient wallet |
| Network | Base mainnet (USDC; x402 mode) |
| Float target | $5 USDC initially; auto-alarm at $1 |
| SSM keys | `/gecko/prod/TWITSH_WALLET_PRIVATE_KEY`, `/gecko/prod/TWITSH_WALLET_ADDRESS` |
| Sentinel pattern | `__unset__` value in SSM disables the integration |
| Health check | CloudWatch custom metric `gecko/twitsh/wallet_balance_usdc`, alarm `< 1 USDC` → SNS `gecko-ops-alerts` |
| Refill | Manual for V1; document in `docs/runbooks/twitsh-refill.md` |

### 3.4 What we query per Pro session

5 reads cache-warm → 10 reads cache-cold. Cap enforced in `TwitshClient` with a session-scoped counter.

| # | Endpoint | Query | When |
|---|---|---|---|
| 1 | `searchTweets` | `<key terms from idea>` | Always (if twit.sh enabled) |
| 2 | `userTweets` | `@colosseum` (top 20 recent) | If `crypto` in classified categories |
| 3 | `userTweets` | `@mattytay` (top 20 recent) | If `crypto` |
| 4 | `userTweets` | `@GuiBibeau` (top 20 recent) | If `crypto` |
| 5 | `searchTweets` | `<idea_category> (kill OR ship OR moat OR wedge)` | If `crypto` or `hackathon` |

**Specific tweet seed:** `https://x.com/GuiBibeau/status/2045236475332092404` is included by URL in the judge-rubric distillation prompt as a pinned reference (fetched once at deploy time, committed to `gecko-mcpay-skills/judges/seed_pins.md`).

### 3.5 Persisted on `ResearchResult`

```jsonc
{
  "x_signal": {
    "queried_at": "2026-04-28T...",
    "top_tweets": [
      { "author": "@handle", "text": "...", "likes": 312, "retweets": 18, "url": "...", "inferred_sentiment": "bearish" }
    ],
    "cost_usdc": 0.025
  },
  "judge_threads": {
    "by_judge": {
      "@colosseum": { "tweets_analyzed": 18, "rubric_distillation": "ships ideas with..." },
      "@mattytay":  { "tweets_analyzed": 14, "rubric_distillation": "..." },
      "@GuiBibeau": { "tweets_analyzed": 11, "rubric_distillation": "..." }
    },
    "applied_to_judge_agent": true
  }
}
```

Stored in `research_results.payload` JSONB column (existing schema; no migration).

### 3.6 Failure modes (fail-closed table)

| Mode | Detection | Action |
|---|---|---|
| twit.sh 5xx | HTTP status | Log `WARN twitsh.unavailable`; skip; Tavily covers |
| Wallet balance < $0.50 | Pre-flight check in `TwitshClient.__aenter__` | Skip integration this session; emit `twitsh.wallet_low` metric; alarm fires |
| Endpoint catalog stale (404) | HTTP 404 on known catalog path | Log `ERROR twitsh.catalog_stale`; fall back to hardcoded catalog; if still 404, disable for the session |
| 402 retry storm | Retry budget exhausted (3) | Skip; emit metric |
| Per-session read cap exceeded (>10) | Counter in client | Hard stop, log; never bills more than the cap |
| Cache hit | MongoDB lookup before HTTP | Free; no x402 round-trip |

### 3.7 Persistence cache (MongoDB)

| Field | Value |
|---|---|
| Database | `gecko_cache` (new) |
| Collection | `twitsh_cache` |
| Key | `sha256(endpoint || normalized_query)` |
| Value | Full JSON response + retrieved-at timestamp |
| TTL | 12 hours (MongoDB TTL index on `retrieved_at`) |
| Hit ratio target | >40% within 1 week |

This is the survey's "MongoDB earns its place when we have a write-once-read-many cache that doesn't need to JOIN with sessions" trigger condition. Now active.

---

## 4. Max Tier — Small / Medium / Large

| Spec | **Pro** (V1, current) | **Max-S** | **Max-M** | **Max-L** |
|---|---|---|---|---|
| Price | $0.75 | $2.00 | $5.00 | $15.00 |
| AG2 max_turns | 4 | 6 | 10 | 16 |
| AG2 max_tokens / turn | 1.5k | 2.5k | 4k | 8k |
| Sources fired | 6 V1 | 6 V1 + Helius DAS | + DefiLlama, GH-trending, Wellfound | + FDA/FAA stubs, frames.ag user_context, on-chain history |
| Reruns included | 0 | 1 | 3 | 5 |
| Judge-rubric distillation | basic (3 judges) | basic (3) | full (8 judges) | full + per-judge agent simulation |
| frames.ag user-personalization | no | no | yes (if available) | yes (required, soft-degrade if not) |
| On-chain calibration (Helius) | no | yes | yes | yes + Helius enriched DAS |
| Latency budget | 90s | 90s | 180s | 300s |
| **COGS sketch** | $0.10 | $0.30 | $0.85 | $2.40 |
| **Margin** | 87% | 85% | 83% | 84% |

**Recommendation: Max launches with V2 sources, not V1 GA.** Pro is the only paying SKU at GA Monday. Max introduces SKU sprawl before we have signal on Pro conversion. Max requires V2 sources (Helius DAS, DefiLlama, frames.ag enrichment) to differentiate. Sequence: Sprint 3 lands V2 sources → Max launches alongside, Sprint 4. Pricing locked now, implementation deferred.

---

## 5. Internal Flywheel — Implementation

Locked decisions:
- **No opt-out in V1.** Pro flat-rate buys it.
- **Deletion-on-request escape valve.** `DELETE /v1/me/precedent/<session_id>` endpoint cascades to `gecko_precedent` row.
- **Storage:** Supabase. Migration `015_gecko_precedent.sql`.

### 5.1 Migration

```sql
-- infra/supabase/migrations/015_gecko_precedent.sql
create table gecko_precedent (
  id uuid primary key default gen_random_uuid(),
  session_id uuid references sessions(id) on delete cascade,
  user_id uuid references auth.users(id),
  idea_summary text not null,        -- 1-sentence LLM category summary; NEVER verbatim
  idea_hash text not null,
  category_tags text[] not null,
  verdict text not null check (verdict in ('ship','kill','pivot')),
  key_comparables jsonb not null,
  embedding vector(1536) not null,
  created_at timestamptz default now()
);
create index gecko_precedent_embedding_idx
  on gecko_precedent using ivfflat (embedding vector_cosine_ops);
create index gecko_precedent_user_idx on gecko_precedent (user_id);

alter table gecko_precedent enable row level security;
create policy "read all" on gecko_precedent for select using (true);
create policy "delete own" on gecko_precedent for delete using (auth.uid() = user_id);
```

### 5.2 Privacy guardrail (CI)

`packages/gecko-core/tests/test_precedent_privacy.py`:

```python
@pytest.mark.parametrize("idea", LOAD_TEST_CORPUS)  # 50 sample ideas
async def test_summary_does_not_leak_verbatim(idea):
    summary = await _llm_category_summary(idea)
    overlap = _verbatim_overlap_ratio(summary, idea)
    assert overlap <= 0.30, f"leak: {overlap:.2f} of original chars in summary"
```

CI fails if regression introduced.

---

## 6. Build Sequencing — 13 Tickets

Total effort: 7-9 person-days. ID prefix `S2X-`.

| ID | Title | Owner | Effort | Blocked-by | Acceptance test |
|---|---|---|---|---|---|
| S2X-01 | Migration `015_gecko_precedent` | data-engineer | S (3h) | — | `gecko-mcp doctor` reports table present; pgvector index built |
| S2X-02 | Source dispatcher module | software-engineer | M (6h) | — | Dispatcher fans out 6 sources concurrently, aggregates, swallows per-source failures |
| S2X-03 | Idea classifier (keyword-sniff) | software-engineer | XS (2h) | — | Classifies 20 fixture ideas with ≥85% accuracy |
| S2X-04 | Flywheel write-path hook | software-engineer | S (3h) | S2X-01, S2X-02 | Pro session completion writes a row |
| S2X-05 | Flywheel privacy guardrail test | data-engineer | S (3h) | S2X-04 | CI fails on leaky summary fixture |
| S2X-06 | Flywheel read-path retrieval | software-engineer | S (3h) | S2X-01, S2X-04 | Top-5 cosine query returns expected fixtures |
| S2X-07 | HN + Reddit source modules | software-engineer | XS (2h) | S2X-02 | Aggregates into `community_signal` |
| S2X-08 | **twit.sh client + catalog bake** | web3-engineer | M (6h) | S2X-02 | Mock x402 facilitator; client makes 5 reads, retries on 402, respects per-session cap |
| S2X-09 | twit.sh wallet provisioning + alarm | web3-engineer | S (4h) | — | Alarm fires when balance < $1; runbook reproducible |
| S2X-10 | MongoDB twitsh_cache | data-engineer | S (3h) | S2X-08 | Cache hit short-circuits HTTP; TTL 12h verified |
| S2X-11 | Pro tier prompt update | software-engineer | S (3h) | S2X-04, S2X-06, S2X-08 | Prompt diff snapshot test; smoke `bb research` runs e2e |
| S2X-12 | MCP/CLI surface for opt-in PATs | software-engineer | S (3h) | S2X-02 | `gecko_research(..., colosseum_pat=, github_pat=, disable_sources=[...])` |
| S2X-13 | Frames.ag one-pager + Max pricing decision doc | business-manager + staff-engineer | S (4h) | — | Doc reviewed; sent or scheduled |

**Critical path:** S2X-01 → S2X-04 → S2X-06 → S2X-11 (~16h sequential).

---

## 7. Decision Criteria for V1 GA

V1 source rollout is "done" when:

- [ ] All 6 V1 sources integrated behind `dispatcher.py`
- [ ] `classify(idea)` deployed and passes 20-idea labeled fixture set
- [ ] `ResearchResult` JSONB includes new blocks: `gecko_precedent`, `community_signal`, `colosseum_context`, `github_signal`, `x_signal`, `judge_threads`
- [ ] Pro tier prompts reference new context blocks (snapshot tests pass)
- [ ] Flywheel write fires on every Pro session
- [ ] twit.sh wallet funded ($5 USDC); CloudWatch alarm wired; runbook published
- [ ] Eval harness re-run on the 20-idea suite shows `verdict_accuracy > 0.85`
- [ ] `gecko-mcp doctor` passes with new env keys
- [ ] No secrets in diff; ruff + mypy + pytest clean

---

## 8. Open Questions (residual)

1. **frames.ag coordination timing.** Send the one-pager before Monday demo (signals seriousness, risks dilution) or Sprint 3 start (more controlled)?
2. **Max tier launch window.** Alongside Sprint 3 V2 source rollout (recommended), or wait for first paying-Pro signal (~2-4 weeks of GA)?
3. **Eval harness scope.** Re-run the existing 20-idea suite (same baseline, easy comparison) or expand to crypto-shape + saas-shape sub-suites of 15 each (better signal, 2x effort)?
4. **Idea classifier method.** Regex/keyword-sniff (XS effort, deterministic) vs. small embedding nearest-neighbor against a category seed set (S effort, more accurate)? I'd ship keyword for V1 and migrate if accuracy drops.
5. **twit.sh upstream continuity.** twit.sh has no published SLA / status page / team page. Are we comfortable shipping a paid V1 dependency on a hackathon-era service, or do we want a contractual conversation with twit.sh first?

---

## 9. Cost Model (final)

Per Pro session (all sources fire, cache cold):

| Line | Cost (USD) |
|---|---|
| Existing baseline (Tavily + LLM + embed) | $0.100 |
| Flywheel embed + retrieve | $0.001 |
| HN / Reddit / Colosseum / GitHub | $0.000 |
| twit.sh (10 reads × $0.005) | $0.050 |
| Judge-thread distillation LLM | $0.005 |
| **Total COGS** | **$0.156** |
| **Gross at $0.75 Pro** | **$0.594** |
| **Margin** | **79%** worst case; **88%** with cache warm |

With a 40% twit.sh cache hit ratio (achievable within first week), margin lifts to 84%.

---

**End of build plan.**
