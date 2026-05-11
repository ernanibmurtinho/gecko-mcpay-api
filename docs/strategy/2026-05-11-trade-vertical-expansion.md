# Trade Vertical Expansion — Design Spec (2026-05-11)

Three new surfaces composing on top of the shipped `gecko_trade_research` oracle: `gecko-wallet` (skill), `gecko-trade-agent` (skill + self-hosted runtime), and an investor-canon + hot-path ingestion delta. Coach (`gecko-trade-coach`) is already scaffolded at `gecko-claude/.claude/skills/gecko-trade-coach/` and is treated as fixed input.

## 1. Surface map

| Surface | Lives in | Layer | Talks to | Cadence |
|---|---|---|---|---|
| `gecko-trade-coach` (existing) | `gecko-claude` | CC skill | `gecko_trade_research` MCP, user | Interactive |
| `gecko-wallet` (new) | `gecko-claude` | CC skill | `okx-agentic-wallet`, `okx-dex-swap`, optionally `gecko_trade_research` | Interactive, one-shot |
| `gecko-trade-agent` skill (new) | `gecko-claude` | CC skill | Local agent runtime via shell + state file | Interactive |
| `gecko-trade-agent` runtime (new) | `gecko-mcpay-api/packages/gecko-core/src/gecko_core/trade_agent/` | Long-running Python process | Helius, Pyth, Mongo (own state), execution rail, `gecko_trade_research` (on schedule) | Continuous |
| `gecko_trade_research` (shipped) | `gecko-mcpay-api/packages/gecko-mcp/` | MCP tool | Full Mongo `chunks`, AG2 panel, x402 | Per-call |

Data lanes:

```
Coach ─────► gecko_trade_research ─► full corpus (Mongo chunks, all provider_kinds)
Wallet ────► onchainos CLI (peers) ─► chain RPC
              └─► gecko_trade_research (optional, only when user asks "should I")
Agent  ────► Helius/Pyth (hot)   ─► local state cache (short-TTL)
              └─► Mongo agent_* (own state only — positions, PnL, journal)
              └─► gecko_trade_research (scheduled + triggered only)
              └─► OKX/SendAI/Backpack execution adapter
```

## 2. `gecko-wallet` skill spec

Path: `gecko-claude/.claude/skills/gecko-wallet/SKILL.md`.

### Frontmatter

```yaml
name: gecko-wallet
description: Gecko's wallet co-pilot. Natural-language wallet ops grounded by Gecko's verdict oracle when value-judgment is needed. Routes to okx-agentic-wallet / okx-dex-swap / sendai-wallet for execution; never holds keys, never executes directly. Triggers — gecko wallet, fund my agent, get me sol, do i have enough, grounded swap, should i swap, gas top-up with research.
version: "0.1.0"
metadata:
  oracle_tool: gecko_trade_research
  peer_skills: [okx-agentic-wallet, okx-dex-swap, okx-dapp-discovery, okx-onchain-gateway, okx-security, okx-audit-log]
```

### Step 0 — Re-route check

Mirrors `okx-agentic-wallet/SKILL.md:17-54`. Three classes:

| Intent class | Action |
|---|---|
| Pure wallet read (balance, history, addresses) | Re-route to `okx-agentic-wallet` |
| Trade verb without judgment ("swap 10 USDC to SOL") | Re-route to `okx-dex-swap` |
| Trade verb WITH judgment ("should I top up", "do I need SOL", "is now a good time to bridge") | Stay; call `gecko_trade_research` then dispatch |
| Named DApp + action | Re-route to `okx-dapp-discovery` |

The wedge sits squarely on the third row — judgment-grounded wallet ops. Everything else delegates.

### Intent → action map

| User says | Skill does |
|---|---|
| "I need SOL to finish this operation" | Read balance via `okx-agentic-wallet`; if < threshold, decide swap source via internal heuristics; dispatch `okx-dex-swap` |
| "Should I bridge USDC to Base right now" | Call `gecko_trade_research` (`vertical=dex`, `idea=bridge USDC sol→base now`); surface verdict + dissent; ask user to confirm before re-routing to `okx-onchain-gateway` |
| "Fund my agent with $50 SOL" | Read agent wallet from `~/.gecko/trade-agent/agents.json`; dispatch `okx-agentic-wallet send` to that address |
| "What's my wallet worth" | Re-route to `okx-agentic-wallet` (pure read) |
| "Is it safe to approve X" | Re-route to `okx-security` |

### Composition

Installed alongside the OKX skill bundle. Never duplicates `onchainos` calls — every chain-touching op is a delegation. The skill's job is **intent classification + grounding**; execution is peer-owned. Wallet/facilitator neutrality preserved: the same skill works with SendAI or Backpack once their peer skills exist; today only OKX peers are wired.

## 3. `gecko-trade-agent` skill + runtime spec

### Skill (`gecko-claude/.claude/skills/gecko-trade-agent/SKILL.md`)

Four commands, all thin wrappers over shelling out to a local Python runtime:

| User intent | Skill executes |
|---|---|
| "Deploy strategy <name>" | `bb trade-agent up --spec <path>` |
| "Show my agents" | `bb trade-agent ls` |
| "What's agent X doing" | `bb trade-agent inspect <id>` (tail journal + open positions) |
| "Stop agent X" | `bb trade-agent stop <id>` |
| "Pause new entries" | `bb trade-agent pause <id>` (existing positions managed, no new entries) |

Skill is offline-aware: if runtime not installed, instructs the install command (`uv tool install gecko-trade-agent` or equivalent shipped with `gecko-mcpay-api`).

### Runtime module layout

Path: `gecko-mcpay-api/packages/gecko-core/src/gecko_core/trade_agent/`

```
trade_agent/
  __init__.py
  cli.py                  # `bb trade-agent ...` entry; thin Click wrapper
  runtime.py              # async event loop; one instance per agent
  spec.py                 # loads + validates the coach-emitted JSON against schema.json
  modes/
    __init__.py
    advisor.py            # mode=advisor: surface opportunities, never sign
    trader.py             # mode=trader: gated execution
  scheduler.py            # APScheduler-style; owns re-verdict cadence + circuit-breaker timers
  hotpath/
    helius.py             # websocket subscription manager (account + program subs)
    pyth.py               # Hermes price feed websocket
    cache.py              # in-process TTL cache (no Mongo round-trip on hot path)
  state/
    mongo.py              # writes own collections only — agent_state, agent_positions, agent_journal
    models.py             # Pydantic models matching the Mongo schemas below
  oracle.py               # wrapper around gecko_trade_research MCP call; rate-limits + caches verdicts
  exec_adapters/
    __init__.py
    okx.py                # shells onchainos dex-swap
    sendai.py             # stub for v0.2
    backpack.py           # stub for v0.2
  primitives/             # entry/exit/sizing/filter/risk implementations matching schema.json enums
```

`runtime.py` is one asyncio loop: hotpath listeners produce events → mode handler decides → execution adapter dispatches → journal write. `scheduler.py` runs out-of-band re-verdict jobs on the same loop.

### State schema (Mongo, DB: `gecko_trade_agent`)

| Collection | Purpose | Indexes |
|---|---|---|
| `agent_state` | One doc per agent: spec snapshot, mode, status, last_verdict_id, daily_loss_pct, circuit_breaker | `{agent_id: 1}` unique, `{user_wallet: 1, status: 1}` |
| `agent_positions` | Open + recently-closed positions | `{agent_id: 1, status: 1}`, `{agent_id: 1, opened_at: -1}` |
| `agent_journal` | Append-only event log: entries, exits, verdicts called, circuit-breaker trips | `{agent_id: 1, ts: -1}`, TTL 90 days |
| `agent_verdict_cache` | Cached `gecko_trade_research` results keyed by (agent_id, idea_hash) | `{agent_id: 1, idea_hash: 1}`, TTL 24h |

**Hard rule:** runtime never reads from `chunks`, `sources`, or any corpus collection. The oracle call is the only path to corpus data.

### Modes (one shell)

```python
# runtime.py (sketch)
async def tick(self, event: HotpathEvent) -> None:
    if self.mode == "advisor":
        candidate = self.advisor.evaluate(event)
        if candidate:
            self.journal.write("opportunity", candidate)   # surface; no exec
    elif self.mode == "trader":
        candidate = self.trader.evaluate(event)
        if not candidate:
            return
        if self.risk.would_breach(candidate):
            return
        verdict = await self.oracle.get(candidate)         # cache-first
        if verdict.act:
            await self.exec_adapter.submit(candidate)
            self.journal.write("entry", candidate, verdict)
```

## 4. Data access matrix

| Component | `chunks` (full corpus) | `agent_*` Mongo | Helius / Pyth | x402 paid oracle | Notes |
|---|---|---|---|---|---|
| Coach | NO (via oracle only) | NO | NO | YES, per rule + final | Conversational |
| Wallet skill | NO | NO | Indirect (via oracle) | YES, only when intent is judgment-laden | One-shot |
| Agent — advisor mode | NO | RW (own only) | YES, websocket | YES, scheduled + triggered | Read-heavy |
| Agent — trader mode | NO | RW (own only) | YES, websocket | YES, scheduled + triggered + every entry gate (cache-first) | Cache hit is the common path |
| `gecko_trade_research` MCP | YES | NO | NO | n/a (it IS the oracle) | Unchanged |

## 5. Re-verdict triggers

The agent calls `gecko_trade_research` in exactly these cases. Everything else uses the cached verdict.

| Trigger | Tier | Cadence ceiling | Stored as |
|---|---|---|---|
| Scheduled refresh | basic ($0.25) | Every 24h, per spec | `agent_verdict_cache` baseline |
| Strategy-level pre-check on agent start | pro ($0.75) | Once per `up` | `agent_state.startup_verdict_id` |
| Circuit-breaker trip (daily loss ≥ `risk.max_daily_loss_pct` or surviving dissent strength ≥ threshold from spec) | basic | At most 1x/h while halted | `agent_journal` entry |
| Manual user prompt ("re-verdict now") via skill | basic or pro | Rate-limited 1x/15 min | `agent_journal` entry |
| Entry gate when cached verdict is older than spec freshness window OR the candidate idea differs materially from cached idea_hash | basic | Per-entry, but cache hit on identical idea avoids it | `agent_verdict_cache` |
| Spec change (user edits strategy mid-flight) | pro | Once per change | `agent_state.last_verdict_id` |

Cost math: 1 startup-pro + 1 daily-basic + ~3 triggered/day average = ~$1.50/day per agent. Worst case (volatile day, multiple breaker trips, distinct ideas) capped at ~$3/day by the rate limits above.

## 6. Ingestion pipeline diff

### Investor-canon corpus

Goes to existing Mongo `chunks` collection. Adds new `ProviderKind` Literal values per Pattern A — single Python file (`packages/gecko-core/src/gecko_core/sources/types.py:39-60`) plus the SQL CHECK migration mirror (which still ships for the drift test even though chunks live in Mongo — see memory `project_supabase_chunks_dropped_2026_05_08.md`).

| New `ProviderKind` | Source family | Freshness tier | Content kind |
|---|---|---|---|
| `canon_marks` | Oaktree memos (HTML+PDF) | `static` | `mechanism` |
| `canon_damodaran` | NYU Stern PDFs + posts | `static` | `mechanism` |
| `canon_mauboussin` | Morgan Stanley investment papers (PDF) | `static` | `mechanism` |
| `canon_youtube` | Boyle / Felix / Mauboussin YouTube transcripts | `static` | `mechanism` |
| `canon_berkshire` | Berkshire shareholder letters | `static` | `mechanism` |
| `canon_macro` | Fed, BIS, IMF working papers | `daily` (release-driven) | `mechanism` |

Ingestion path reuses `gecko_core/ingestion/` — `youtube.py` for transcripts, `pdf.py` for PDFs, `web.py` for HTML. Per-provider `embed_adapter.py` translates internal tags to canonical `ProviderKind` (the existing translation contract in `sources/types.py:17-25`).

### Hot-path data (Helius + Pyth)

NOT chunks. Lives in-process under `trade_agent/hotpath/cache.py` with an optional cold mirror to a new short-TTL collection.

| Collection | Purpose | TTL |
|---|---|---|
| `agent_hotpath_snapshot` (new, DB `gecko_trade_agent`) | Last-seen price / account state per (agent_id, mint). Only written if process is restarting and needs warm-start. | 5 min TTL index |

Helius: websocket subscriptions per token mint in spec (account + transaction subs). Pyth: Hermes SSE per price feed. Polling fallback at 5s on websocket disconnect. The cache is the only read surface for trader-mode decisions; `chunks` is never on the entry-gate path.

### Migrations

1. `infra/supabase/migrations/20260511_canon_provider_kinds.sql` — extends `sources_provider_kind_check` (drift-test contract; not live DDL for chunks).
2. Mongo Atlas: add `gecko_trade_agent` DB; create collections + indexes via `scripts/mongo/bootstrap_trade_agent.py` (new).
3. No changes to `gecko_rag.chunks` shape — just new `provider_kind` enum values flowing in.

## 7. Lane split — tickets

Aim: 8–12 tickets, mix of S (≤1d), M (2–3d), L (4–5d).

### `data-engineer` (4)

1. **DE-1 (M)** — Ingestion runner for investor-canon (Oaktree, Damodaran, Mauboussin, Berkshire). PDF + HTML; resumable; idempotent on `chunk_id`. Deps: none.
2. **DE-2 (M)** — YouTube transcript ingestion for Boyle / Felix / Mauboussin channels with `provider_kind=canon_youtube`. Reuses `ingestion/youtube.py`. Deps: none.
3. **DE-3 (S)** — Add the 6 new `ProviderKind` values to `sources/types.py` + drift-test migration + freshness tier mapping. Deps: none.
4. **DE-4 (S)** — Bootstrap script for `gecko_trade_agent` Mongo DB (collections + indexes). Deps: none.

### `software-engineer` (4)

5. **SE-1 (L)** — `trade_agent/runtime.py` event loop + `spec.py` schema loader + `state/mongo.py` writers. Deps: DE-4.
6. **SE-2 (M)** — `trade_agent/cli.py` (`bb trade-agent up/ls/inspect/stop/pause`) + `gecko-trade-agent` skill in `gecko-claude`. Deps: SE-1.
7. **SE-3 (M)** — `gecko-wallet` skill — frontmatter, Step 0 router, intent→action map, peer dispatch. Deps: none (peer skills already installed).
8. **SE-4 (S)** — `exec_adapters/okx.py` — shell wrapper over `onchainos dex-swap` with retry + audit-log write. Deps: SE-1.

### `ai-ml-engineer` (2)

9. **AIML-1 (M)** — `trade_agent/oracle.py`: verdict cache, idea-hash strategy (so semantically-equivalent ideas hit cache), trigger logic from §5. Deps: SE-1.
10. **AIML-2 (M)** — `modes/advisor.py` + `modes/trader.py` — primitive→evaluator wiring per `schema.json` enums, plus opportunity-surfacing prompts. Deps: SE-1, AIML-1.

### `web3-engineer` (2)

11. **W3-1 (M)** — `hotpath/helius.py` websocket manager — account + program subs, reconnect, backoff, drop-event metrics. Deps: none.
12. **W3-2 (S)** — `hotpath/pyth.py` Hermes SSE client + `hotpath/cache.py` in-process TTL store. Deps: none.

Total: 12 tickets, ~22–28 dev-days. Critical path: DE-4 → SE-1 → (SE-2, SE-4, AIML-1) → AIML-2.

## 8. Risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| **Re-verdict cadence too slow → strategy drifts mid-day** | M | H | Circuit-breaker on dissent-strength threshold from spec; freshness window on cache hit forces fresh verdict if idea_hash drifts; surface "stale verdict" warning in `inspect` |
| **Self-hosted runtime brittle on user laptops (sleep, network drops, OS quirks)** | H | M | Runtime writes journal + checkpoint every event so cold restart resumes; advisor mode default for v0.1 (no exec risk on crash); document `systemd --user` / `launchd` recipes; ship `bb trade-agent doctor` parallel to `gecko-mcp doctor` |
| **Hot-path Helius/Pyth latency eats into trade execution budget** | M | M | In-process cache means decisions read RAM, not network; websocket prewarm; degrade-to-polling fallback at 5s; circuit-breaker if RPC p95 > 2s for 60s. Latency budget published per-event in journal so users see slippage attribution |

Secondary risks (track but don't gate): canon-corpus IP review for "free + public-domain only" claim (legal-lite check before publishing); X402 cost overrun if cache-hit ratio < 50% (alert at 30%).

## 9. Open questions for founder

1. **Advisor mode default vs trader mode default for v0.1?** If laptop reliability is a real worry, shipping advisor-only first lets users dogfood without exec risk. Trader-mode flips on after a sprint of advisor telemetry.
2. **Should the agent's startup pro-tier verdict be free for the first run after coach hands off the spec, or always paid?** Coach already paid for per-rule verdicts; charging again at agent boot may feel double-dip.
3. **Backpack/SendAI execution adapters: stub in v0.1 or skip until a user asks?** Stub-but-shipped is Pattern B — we lose more than we gain. Recommend skip until requested.
4. **Helius credit budget per agent — cap, or pass-through to user wallet?** Per-agent websocket subs scale with positions × tokens; at 10 agents this is a meaningful Helius bill.
5. **Strategy spec versioning when user edits mid-flight: hot-swap, restart agent, or refuse and require `stop` + `up` cycle?** Cleanest is the restart cycle; hot-swap invites state corruption. Confirm before SE-1 design freezes.

---

Files referenced:
- `/home/nan/PycharmProjects/Gecko/gecko-claude/.claude/skills/gecko-trade-coach/SKILL.md`
- `/home/nan/PycharmProjects/Gecko/gecko-claude/.claude/skills/gecko-trade-coach/schema.json`
- `/home/nan/PycharmProjects/Gecko/gecko-mcpay-api/packages/gecko-core/src/gecko_core/sources/types.py` (lines 39-60, 67-80)
- `/home/nan/PycharmProjects/Gecko/gecko-mcpay-api/packages/gecko-core/src/gecko_core/db/chunk_store.py`
- `/home/nan/PycharmProjects/Gecko/gecko-mcpay-api/.agents/skills/okx-agentic-wallet/SKILL.md` (Step 0 pattern, lines 17-54)
