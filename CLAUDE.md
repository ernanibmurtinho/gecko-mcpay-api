# Gecko — Builder Bootstrap Platform (Python backend)

You are working on **gecko-mcpay-api**, the Python backend for the Builder Bootstrap Platform. Distribution is via Claude Code skills (frames.ag-style: `Read app.geckovision.tech/skill.md` → bootstrap → use).

## The three repos

| Repo | Stack | What lives there |
|---|---|---|
| `gecko-mcpay-api` (this) | Python uv workspace | SDK, MCP server, FastAPI, CLI |
| `gecko-mcpay-app` | Next.js | V2 frontend at `app.geckovision.tech` |
| `gecko-claude` | Markdown + Vercel | Public `skill.md` + `install.sh` + example apps; serves `app.geckovision.tech/skill.md` |

Cross-repo work goes through `staff-engineer`. Most changes are contained in one repo.

## Communication Style

- No filler ("Great question", "Awesome, here's what I'll do")
- Direct, code-first, brief
- Admit uncertainty rather than guess
- Recommendation first, justification second

## Repository shape (this repo)

uv workspace monorepo (single repo, multiple packages — that's normal Python project layout, not a polyrepo problem).

| Path | What lives there |
|---|---|
| `packages/gecko-core` | The SDK. Pure Python business logic. **All real work happens here.** |
| `packages/gecko-mcp` | MCP server wrapping `gecko-core`. The frames-style surface for Claude Code. |
| `packages/gecko-api` | FastAPI service. What `gecko-mcpay-app` calls. Deploys independently. |
| `apps/cli` | The `bb` / `gecko` CLI. Wraps `gecko-core`. |
| `skills/` | DOES NOT EXIST HERE — skill.md + install.sh live in the `gecko-claude` repo |
| `apps/web/` | DOES NOT EXIST HERE — web app lives in the `gecko-mcpay-app` repo |
| `infra/supabase/migrations` | DB schema, including pgvector setup |
| `docs/` | PRD.md, product-story.md, design specs |

**Rule:** business logic in `gecko-core`. CLI, MCP, API are thin transport layers — parse input, call core, format output. If you find logic in `gecko-mcp` or `apps/cli`, move it to `gecko-core`.

## Tech stack

- **Python 3.11+**, `uv` for env + workspace management
- **Supabase** (Postgres + pgvector) for sessions, sources, chunks, embeddings
- **OpenAI** `text-embedding-3-small` and `gpt-4o-mini`; Pro tier uses **AutoGen** GroupChat
- **Tavily** for source discovery; `httpx` + `youtube-transcript-api` for extraction
- **x402** on Solana for payments — modes: `stub`, `live`, `frames` (post-hackathon)
- **Click** for CLI, **Rich** for terminal output, **FastAPI** for the API
- **MCP** for the Claude Code surface

## Mandatory workflow

```bash
uv run ruff format
uv run ruff check --fix
uv run mypy packages/ apps/
uv run pytest
# if pipeline touched:
bb research --idea "smoke test"
# if env/config touched:
gecko-mcp doctor
```

## Security non-negotiables

**NEVER**:
- Commit secrets. `.env` files are gitignored; `.env.example` ships with empty values.
- Expose `SUPABASE_SERVICE_ROLE_KEY` to any client. CLI is OK; `gecko-mcpay-app` is NOT — it uses anon key + RLS via `gecko-api`.
- Log API keys, even in errors. Redact before raising.
- Show users model names, token counts, or per-operation costs. Session pricing is the unit.
- Call OpenAI without `response_format={"type": "json_object"}` when the caller expects JSON.

**ALWAYS**:
- Validate URLs before fetching (no SSRF — block private IP ranges, file://, etc.)
- Cap LLM input tokens per request; truncate context defensively.
- Persist sessions before any expensive work so a crash doesn't lose state.
- Pass `X402_MODE` through every payment-touching code path; default to `stub` if unset.

## Subagent team

Eight specialists. Seven own work in this repo; `frontend-engineer` is a cross-repo stub that surfaces coordination notes for their full counterpart in `gecko-mcpay-app`.

**Engineering (staff-engineer arbitrates across these lanes):**

| Agent | When |
|---|---|
| `staff-engineer` | Architectural decisions, cross-package or cross-repo refactors, "should we…" questions, arbitration when work crosses lanes |
| `ai-ml-engineer` | Prompt engineering, agent persona design, eval harness + thresholds, RAG quality, AG2 5-agent debate, advisor panel reliability, verdict synthesis, "why is the model doing X" |
| `software-engineer` | Feature implementation in `packages/` and `apps/` (Python) |
| `data-engineer` | Supabase schema, pgvector, ingestion pipeline, embeddings storage |
| `web3-engineer` | x402, Solana, Base/CDP, wallet flows, frames.ag integration, on-chain settlement |

**Product:**

| Agent | When |
|---|---|
| `business-manager` | PRD updates, pricing, GTM, success metrics |
| `product-designer` | Terminal output styling, UX flows, web app screen specs |
| `product-manager` | ICP definition, user journey mapping, outcome-driven prioritization |
| `trading-strategist` | Trade-vertical: financial data sources, investor-canon corpus, backtest design, execution-venue tradeoffs, competition diagnostics. Owns "does this trade make money." |
| `frontend-engineer` | Cross-repo stub. Invoke when API or model changes here affect `gecko-mcpay-app`. Real implementation agent lives in that repo. |

Default to `staff-engineer` for any change that touches more than one package, more than one repo, or crosses engineering lanes.

**Lane boundaries to keep clean:**

- `software-engineer` owns "the code runs correctly"
- `data-engineer` owns "the data is correctly stored"
- `ai-ml-engineer` owns "the model gives the right answer"
- `web3-engineer` owns "the payment settles correctly"

When a problem touches more than one of these, route to `staff-engineer` first.

## MCP servers (development)

Configured in `.mcp.json`. Keys go in `.env` (gitignored).

- **Supabase MCP** — schema introspection during dev
- **Context7** — up-to-date library docs lookup
- **Helius** (when `web3-engineer` is active) — Solana RPC + DAS

## Done checklist

Before merging:

- [ ] `uv sync` succeeds
- [ ] Lint + format clean: `uv run ruff check && uv run ruff format --check`
- [ ] Tests pass: `uv run pytest`
- [ ] If pipeline touched: `bb research --idea "smoke test"` runs in stub mode
- [ ] If env/schema touched: `gecko-mcp doctor` passes
- [ ] If API shape changed: notify `frontend-engineer` in `gecko-mcpay-app` (the OpenAPI spec at `/openapi.json` is the contract)
- [ ] Docs updated: README quickstart works; PRD reflects scope changes
- [ ] No secrets in diff

## Self-learning

**`CLAUDE.md`** (this file, tracked):
- Emphatic user preferences/corrections
- Patterns that repeat 2+ times
- Explicit "remember this"

**`CLAUDE.local.md`** (gitignored):
- Session notes, debugging context, scratch observations

### Project conventions

- **Single source of truth for shared `Literal` types.** Live in one canonical module (e.g. `gecko_core.payments.modes` for `PaymentMode`). Every consumer imports from there — never redeclare. SQL `CHECK` constraints reference a comment block naming the canonical Python enum. Adding a value = touch exactly one Python file + one migration. Schema-drift test in `tests/test_payment_mode_consistency.py` enforces this; replicate the pattern for new shared Literals.
- **Bucketed reputation/score bands, never raw floats public.** Per the profile-thesis synthesis (PD anti-gaming flag): public surfaces show `emerging` / `established` / `senior` style buckets. Raw scores stay internal. No public leaderboards.
- **Wallet/facilitator neutrality.** frames.ag for Solana, CDP for Base, Cloudflare for HTTP, awal as parallel. Never hard-code one in the product. The product works above any x402-capable wallet.

### Recurring patterns

- **Pattern A — Parallel `Literal` redeclarations of the same concept.** `PaymentMode` lived in 4 places before S12.5; each Sprint 12 fix flipped one and missed others. **Encoding:** when a new shared concept needs to live in Python and SQL, route everyone through `gecko_core.types` (or its domain-specific equivalent). Replicate the schema-drift test pattern from `test_payment_mode_consistency.py`.

- **Pattern B — Stubbed-but-shipped code.** Sprint 12 Track A's `_build_payment_payload` shipped as a stub and took ~10 fix iterations against live mainnet to get right. **Encoding:** for any new wire-protocol integration, the *first* deliverable is a free local simulation script (eth_call-style for EVM, RPC mock for Solana) that can falsify the implementation without spending money. Live smoke is the *final* verification, never the primary debug tool.

- **Pattern C — Tests that exercise stubs, not real wires.** Sprint 12 CDP tests passed in stub mode and on `/verify`; `/settle` failed because verify is a pure signature check that doesn't exercise the dispatch branch. **Encoding:** for payment-touching code, every `X402Client` conformer ships with a recorded-fixture contract test (vcr-style) against the real facilitator's relevant endpoints. Adding a new client is gated on the contract test passing. The `live_cdp` marker pattern in S12.5-TEST-04 is the template.

- **Pattern D — "Orchestration" claims need defensibility checks against Perplexity.** When a thesis adds "orchestration + context" as a wedge claim, ask the AI/ML lens whether that's actually defensible against Perplexity / ChatGPT / Bazaar's own discovery. **Orchestration is table stakes; the wedge is rarely orchestration.** Find the actual moat (verdict shape, dissent quality, settlement layer, contributor reputation, etc.) and lead with it. The 2026-05-01 profile-thesis synthesis hit this — AI/ML pushed back on the "orchestration + context" framing and reframed the wedge as the adversarial-debate verdict + grounded dissent.

- **Pattern E — "Wired" ≠ "reaches the model" — every retrieval claim needs an end-to-end probe.** 2026-05-11: the `gecko_trade_research` envelope shipped with structured `citations[]` (issue #15 fix). Per-layer unit tests passed. Live API responses returned `citations: []`. Root cause: TWO independent retrieval paths existed — `rag_query` (session-scoped filter) and `retrieve_trade_corpus_chunks` (protocol-equality `$match`). Both filtered out the 4,733-chunk permanent canon corpus before the panel saw it. Per-layer test reachability is necessary but not sufficient. **Encoding:** for any new "the corpus reaches the panel" claim, ship a direct end-to-end test that calls the actual production retrieval path with a question whose expected citations include the new corpus tag, and asserts ≥1 chunk per new `provider_kind`. The retrieval-wedge sprint doc at `docs/strategy/2026-05-11-retrieval-wedge-sprint.md` is the template.

- **Pattern F — Permanent corpus + session-scoped retrieval is structurally broken.** Same 2026-05-11 root cause. Session-scoped filters work for ephemeral per-request chunks; they silently exclude any chunk written under a *different* session_id. **Encoding:** when corpus has a permanent dimension (canon literature, vendor docs, anything reused across sessions), retrieval filters MUST tolerate "session_id != current_request" via global scoping or explicit `freshness_tier=static` bypass. session_id may stay as provenance metadata; it must NOT be a retrieval gate. See the Option C fix in `docs/strategy/2026-05-11-retrieval-wedge-sprint.md`.

### Trade-vertical conventions (2026-05-11)

- **Three-layer trade architecture: coach → oracle → execution.** Per `memory/project_three_layer_architecture_2026_05_11`: `gecko-trade-coach` (conversational strategy builder) emits a schema-validated spec; `gecko_trade_research` MCP tool produces grounded verdicts with surviving dissent + citations; `gecko-trade-agent` runtime executes the spec locally (advisor mode in v0.1, trader mode in v0.2); execution dispatches through neutral adapters (`okx`, `sendai`, `backpack`). Never collapse layers. The agent runtime never reads the corpus directly — it calls the oracle.

- **Cache-then-charge oracle invocation.** The trade-agent runtime invokes `gecko_trade_research` on cadence + triggers, not per-trade. Cache lookup on `idea_hash` precedes any network call. Stampede protection via per-key `asyncio.Lock`. Cost math: a naive per-trade oracle call would burn ~$12.50/day per agent; cache-then-charge brings it to ~$1.50/day. The cache pattern is non-negotiable for any high-frequency consumer of the oracle. See `packages/gecko-core/src/gecko_core/trade_agent/oracle.py`.

- **Hotpath isolation.** Anything that touches Helius / Pyth / live RPC lives under `packages/gecko-core/src/gecko_core/trade_agent/hotpath/`. The hotpath package depends ONLY on `httpx`, `websockets`, `pydantic` — it does NOT import `gecko_core.db`, `gecko_core.rag`, `gecko_core.orchestration`. This keeps the runtime's data layer testable + swappable + small. The retrieval / oracle / orchestration layers do not call the hotpath either; the runtime owns the bridge.

- **General-knowledge chunks (canon) carry `protocol=[]`.** Protocol-specific chunks (`paysh_live`, `bazaar_live`) carry exact protocol tags. Canon investor literature is cross-cutting and surfaces for ALL protocols. The trade-panel retrieval `$match` accepts `protocol == proto_norm` OR `protocol == []` OR `protocol` missing — never exact-match-only on the protocol field, or canon never reaches the panel. See `packages/gecko-core/src/gecko_core/orchestration/trade_panel/__init__.py` line ~516.

- **Investor-canon corpus: free + public-domain only (v0.1).** Marks (Oaktree memos), Damodaran (NYU Stern PDFs), Berkshire (shareholder letters 1977-2024) — all free + public-domain. Licensed sources (O'Reilly, Perlego) are deferred to v0.2. Founder cash constraint per `memory/feedback_okx_no_funding_pressure`. Ingest scripts live under `scripts/canon/`; the canonical URL list per source lives under `packages/gecko-core/src/gecko_core/sources/canon_*.py`. New canon source = new file + new `ProviderKind` value (Pattern A) + drift test update.

- **Re-verdict triggers, not per-trade verdicts.** The agent calls `gecko_trade_research` only on: (1) scheduled basic refresh every 24h, (2) startup pro-tier on agent boot, (3) circuit-breaker trip at ≤1x/h while halted, (4) manual user prompt rate-limited ≤1x/15min, (5) entry-gate where cached verdict is stale or idea_hash differs, (6) spec change mid-flight. Anything more frequent is a config bug. See `docs/strategy/2026-05-11-trade-vertical-expansion.md` §5.

- **Hot-swap ECS-style for spec updates.** Per founder decision: when a user edits a strategy spec mid-flight, the runtime supports new-version-runs-in-parallel → drain old → atomic cutover. NOT in-place mutation. The runtime exposes `hot_swap_to(new_spec)` which compare-and-swaps `agent_state.spec_version` under a Mongo transaction. Persistence wins races. Daemon-mode parallel cutover (separate processes) lands in v1.0.

- **`X402_MODE=stub` is intentional during user-testing.** Production endpoints accept stub-signature payments without settling real money — the buyer-side flag matches. **Do NOT flip to live without explicit founder go-ahead** per `memory/project_x402_stub_then_live`. Stub-mode smoke calls against `api.geckovision.tech` cost $0 and validate the full code path.

---

**Sister repos**: [`gecko-mcpay-app`](https://github.com/<owner>/gecko-mcpay-app) | [`gecko-claude`](https://github.com/ernanibmurtinho/gecko-claude) | **Domain**: `app.geckovision.tech`
