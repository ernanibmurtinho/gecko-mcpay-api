# Partner Integration Shape — Gecko KaaS × Skills Marketplace + KYA

**Date:** 2026-05-08
**Owner:** ernanibmurtinho
**Status:** Working draft — codifies the integration map shared in chat

## 1. Two systems, one user-facing surface

| System | Owner | Primary surface | Unit of value sold | Settlement |
|---|---|---|---|---|
| **Gecko KaaS** (this repo + `gecko-claude` + `gecko-mcpay-app`) | Us | MCP server at `mcp.geckovision.tech` + CLI `bb` | **Grounded knowledge / verdicts** (vector retrieval + 7-agent debate) | x402 micropayments per call (stub today, live next) |
| **Partner marketplace** | Partner | Claude Code skills marketplace + KYA (Know-Your-Agent) identity layer | **Skills** (curated agent bundles) with verified author identity | Marketplace handles distribution + identity; per-call settlement is the agent's concern |

The two systems are orthogonal and complementary. Partner sells *who you are running* (skill + identity); Gecko sells *what knowledge that skill has access to* (KaaS).

## 2. System-level integration table

| Layer | Partner provides | Gecko provides | Where they meet |
|---|---|---|---|
| Distribution | Skill marketplace listing + install one-liner | `skill.md` + `install.sh` at `app.geckovision.tech/skill.md` | Partner can list `gecko-claude` skills (e.g. `examples/trading-oracle`) in their marketplace |
| Identity | KYA — verified agent author identity (signed manifest, reputation band) | `provider_kind` on every chunk + bucketed reputation per `feedback_no_raw_scores` | KYA proves *who built the skill*; `provider_kind` proves *where the answer's facts came from*. Neither replaces the other. |
| Runtime | Claude Code as the host | MCP server (20 tools) at `mcp.geckovision.tech` | Skill mounts `mcp.geckovision.tech` via `.mcp.json`; Claude Code routes tool calls through the partner-installed skill |
| Settlement | Marketplace fee (if any) on skill purchase | x402 per-call to `gecko_research` / `gecko_trade_research` / `gecko_advise` | Two independent meters. Skill purchase ≠ knowledge call. |
| Trust | KYA reputation band on the skill author | Bucketed `freshness_tier`, `provider_kind`, citation list per verdict | A KYA-verified skill calling Gecko inherits cite-level provenance for free |

## 3. Per-call flow (trading-oracle example)

```
User in Claude Code
  │
  │ 1. installs skill from partner marketplace (KYA-verified author)
  ▼
Skill (gecko-claude/examples/trading-oracle)
  │  manifest mounts mcp.geckovision.tech
  │
  │ 2. user types "is Jito healthy right now?"
  ▼
Claude Code → MCP tool call: gecko_trade_research
  │  (HTTPS to mcp.geckovision.tech/mcp/)
  ▼
gecko-api on ECS (us-east-2)
  │  3. retrieve chunks (Mongo Atlas Vector Search, Voyage 1024-dim)
  │  4. 7-agent AG2 debate (Strategist + 6 lenses)
  │  5. emit TradePanelVerdict with citations
  ▼
Returns to Claude Code → user sees verdict + citations
```

Money flow: marketplace fee at install time (partner-side). x402 micropayment at call time (Gecko-side, currently stub mode per `project_x402_stub_then_live`).

## 4. Boundary contracts (what each side must NOT do)

**Gecko must not:**
- Custody user funds. The skill's `example_call.py` prints intent payloads only; never signs.
- Issue trading verbs ("buy", "sell"). Output is advisory + bucketed confidence.
- Show users raw model names, token counts, or per-operation costs. Session pricing is the unit (CLAUDE.md security rule).

**Partner must not:**
- Modify Gecko's MCP tool contracts. The `tools_contract_*.json` fixture is the canonical schema; drift fails CI.
- Replicate Gecko's retrieval. Partner can list Gecko skills; partner does not run a parallel chunk store.
- Force a wallet/facilitator. Per CLAUDE.md "wallet/facilitator neutrality" — frames.ag for Solana, CDP for Base, awal as parallel; the product works above any x402-capable wallet.

## 5. What's shipped vs what's pending

**Shipped (as of 2026-05-08, v0.2.25):**
- 20-tool MCP server live at `mcp.geckovision.tech` (verified post-deploy: `gecko_trade_research` schema correct).
- `gecko-claude/examples/trading-oracle/` PR #3 with `skill.md` + `.mcp.json` + `example_call.py` + `README.md`.
- Live x402 buyer for paysh + bazaar (Phase 7) — 5 verified Solana settlements + Mongo ingest with `provider_kind ∈ {paysh_live, bazaar_live}`.
- 7-agent trade panel (Phase 8) + naive backtest scaffold (Phase 9, currently `unbacktestable=True` against Pyth).

**Pending (Phase 10A in flight, see this file's sibling agent run):**
- Backtest data swap → CoinGecko OHLCV (so Phase 9 actually returns realized PnL).
- `/trade_research` x402 gate + per-IP rate limit (currently free; trivial DoS surface).
- paysponge/perplexity → EVM-buyer fallback when 402 advertises Base only.

**Pending (cross-repo, partner-side):**
- Partner's marketplace listing for `examples/trading-oracle` once the demo is review-ready.
- KYA enrollment for the gecko-claude skill author.
- A documented "KYA-verified skill calling KaaS" reference flow on partner's docs site.

## 6. Falsifier

If, 4 weeks after the partner marketplace lists `examples/trading-oracle`, **0 of N skill installers issue a paid `gecko_trade_research` call**, the integration shape is wrong. Likely failures:

1. Skill install friction too high (fix: simpler `.mcp.json`).
2. Free `gecko_research` cannibalizes the paid trade tool (fix: gate trade tool harder; differentiate with backtest).
3. Partner's KYA + Gecko's `provider_kind` are seen as redundant (fix: collapse to one trust signal in skill UI).

Track via `mcp.geckovision.tech` access logs + ECS metrics + Mongo `chunk_audit` collection.

## 7. Cross-repo dispatches needed

- `gecko-claude` — confirm `examples/trading-oracle/skill.md` references Phase 10A's `tier=pro` backtest output once that PR lands.
- `gecko-mcpay-app` — out of scope this doc; web verdict viewer is V2 trust-primitive (deferred per `2026-05-08-trading-oracle-reference-skill-design.md` §2 out-of-scope).
- Partner-side — coordinate listing once Phase 10A merges + redeploys.
