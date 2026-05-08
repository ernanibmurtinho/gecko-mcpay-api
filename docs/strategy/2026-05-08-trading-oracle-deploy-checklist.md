# Trading-Oracle Deploy Checklist (2026-05-08)

**Branch:** `feat/trading-oracle-reference-skill` — 28 commits ahead of `main`.
**Status:** Tests green (102/102 in trading-oracle/trade-panel/paysh scope), 5 verified Solana x402 settlements on chain, EVM x402 working too. Ready for prod when founder approves.

This is the operator runbook to ship Phase 6 + 7 + 7.5 + 8 + 9 to production. **Do not run blindly.** Each step has a falsifier — if a step fails, stop and investigate.

---

## TL;DR — Order of operations

1. **DB migrations** (Supabase + Mongo Atlas index) → before code.
2. **Code merge** — branch → main → ECS redeploy (gecko-api + gecko-mcp).
3. **gecko-claude** — push the sister-repo branch with the working `.mcp.json` schema fix.
4. **Smoke** — `gecko_research`, `gecko_trade_research`, paysh ingest probe.

---

## Step 1 — Schema (MongoDB Atlas only — Supabase chunks are gone)

**Important correction:** The two SQL files on this branch (`infra/supabase/migrations/20260508130000_*.sql`, `..._20260508140000_*.sql`) are **not actually applied at deploy time**. The `chunks` table no longer lives in Supabase — per `memory/project_s18_scope_mongo_migration.md` and `project_mongo_cutover_no_backfill.md`, chunks were cut over to MongoDB Atlas in Sprint 18 and the Supabase `chunks` table was revoked-write then dropped in Sprint 19.

The SQL files exist as **Pattern A schema contracts** — the drift test in `tests/test_provider_kind_consistency.py` reads them to verify the Python literal types stay in sync with the documented schema shape. They run zero ALTER TABLE statements against any live database.

So Step 1 is effectively MongoDB-only:

### MongoDB Atlas Search index update (the only DB action that matters)

The `chunks_vector` Atlas Search index needs new filter fields so retrieval queries scoped by these axes don't fall back to collection scans:

```jsonc
// Add to chunks_vector index definition.fields:
{ "type": "filter", "path": "freshness_tier" },
{ "type": "filter", "path": "protocol" },
{ "type": "filter", "path": "content_kind" },
{ "type": "filter", "path": "is_stale" }
```

Manual operator action via the Atlas UI or `mongosh` admin API. Docs reference: bottom comment of `20260508140000_chunk_protocol_content_kind.sql`.

**Falsifier**: if retrieval queries with `protocol=jupiter` are slow (>500ms p50) after deploy, the index update didn't land — re-do it.

### MongoDB chunks collection: NO schema migration needed

MongoDB is schemaless. `insert_chunks_mongo` just gains the new fields on insert; existing chunks lack them and get the default values (`protocol=[]`, `content_kind="unknown"`, `is_stale=false`, `freshness_tier="static"`) per Pydantic default coercion at read time. **No backfill required**, no down-migration risk.

---

## Step 2 — Code merge + ECS redeploy

### What changed

**`gecko-mcp` (MCP server):**
- New tool: `gecko_trade_research(idea, protocol, vertical='dex', tier='basic')`. Tools count **19 → 20**.
- `tools_contract_2026_05_08.json` regenerated.
- No env-var changes required.

**`gecko-api` (FastAPI):**
- New endpoint: `POST /trade_research` (free in v1 — eval phase, no x402 gate yet).
- `insert_chunks_mongo` accepts new kwargs (`protocol`, `content_kind`, `is_stale`, `freshness_tier`) with backward-compat defaults.
- No env-var changes required.

**`gecko-core` (shared library):**
- New package `orchestration/trade_panel/` (7-agent AG2 GroupChat).
- New package `orchestration/trade_panel/backtest/` (Phase 9 — Pyth Hermes adapter, gracefully degrades to `unbacktestable=True` since Pyth doesn't expose OHLCV history).
- New `scripts/trading_oracle/` CLI: probe, multi-method buyer, Solana buyer, per-protocol loop, validator. `scripts/` is dev/operator-tier — does NOT ship to ECS but referenced for runbooks.

### Action

```bash
# 1. Final review
git log feat/trading-oracle-reference-skill main..feat/trading-oracle-reference-skill --oneline | head -30

# 2. Merge
git checkout main
git merge --no-ff feat/trading-oracle-reference-skill -m "release: Phase 6+7+8 — paysh + bazaar x402 ingest + 7-agent trade panel + backtest"
git push origin main

# 3. ECS redeploy (per existing runbook — same as Sprint 22 deploy)
#    - bump gecko-mcp: cd packages/gecko-mcp && uv version --bump patch
#    - tag: git tag v0.2.25 && git push --tags
#    - aws ecs update-service --force-new-deployment --cluster gecko --service gecko-mcp
#    - aws ecs update-service --force-new-deployment --cluster gecko --service gecko-api
```

**Falsifier:** after redeploy:
- `curl -s https://api.geckovision.tech/health` → 200 with version bumped
- `curl -s -X POST https://api.geckovision.tech/mcp/ -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | jq '.result.tools | length'` → **20**
- `curl -s -X POST https://api.geckovision.tech/trade_research -H 'Content-Type: application/json' -d '{"idea":"smoke","protocol":"jupiter"}' | jq .verdict` → `act` | `pass` | `defer`

### Stub mode reminder

Per `memory/project_x402_stub_then_live.md`: **prod runs `X402_MODE=stub` during user testing.** Do NOT flip to live without explicit founder go-ahead. The trading-oracle CLI runs `live` in your shell only; that's separate from prod's `X402_MODE`.

---

## Step 3 — `gecko-claude` push (sister repo)

3 commits on `s23/examples-mcp-test-flow` not pushed:

```bash
cd ~/PycharmProjects/Gecko/gecko-claude
git log origin/s23/examples-mcp-test-flow..HEAD --oneline 2>/dev/null \
  || git log -3 --oneline   # if branch isn't tracked yet
git push -u origin s23/examples-mcp-test-flow
```

Includes the working `.mcp.json` schema fix (`type: http` not `transport: streamable-http`) — anyone using `examples/trading-oracle/` needs this.

---

## Step 4 — Smoke after deploy

In Claude Code with `gecko-claude/examples/trading-oracle/.mcp.json` mounted:

```
> Use gecko_research with idea="solana defi protocol research" and tier="basic"
   (existing founder advisory — should still work)

> Use gecko_trade_research with idea="Should I deposit $1 USDC into Kamino USDC reserve right now?" and protocol="kamino"
   (NEW 7-agent trade panel — should return verdict + confidence + dissent_count + per-agent turns)
```

If both return non-empty verdicts, ship is clean.

### Final corpus snapshot (pre-deploy)

```
trading-oracle corpus health (validate_corpus.py)
  vertical: dex
  total chunks: 62
  provider_kind mix: paysh_live=35, bazaar_live=27
  freshness_tier mix: daily=62
  embedding dim: 1024 (consistent), 100% present
  per protocol: drift/jito/jupiter/kamino/pyth = 7 paysh + 5 bazaar each
```

5 verified on-chain Solana x402 settlements ($0.01 each) on Solana mainnet TXs:
- Jupiter: `5LHuktU6kfRuyW...`
- Kamino: `2jabuqtvPaJRjb...`
- Pyth: `4PMY4DwtJWaqxa...`
- Drift: `49BEvYLLgVqJY1...`
- Jito: `2DbtE1uD9fmCVq...`

---

## Things you can defer

- **Phase 9.5 — backtest data source upgrade.** Pyth Hermes doesn't expose OHLCV history; backtest currently returns `unbacktestable=True` for every call. Phase 9.5 swaps in Birdeye / CoinGecko OHLCV. Track as TODO in `history_source.py:_PROTOCOL_TO_FEED`.
- **`/trade_research` x402 gate.** Currently free in v1 (eval phase). Add per-IP rate limit + x402 gate before opening to non-trusted partners. Reviewer flagged this in Phase 8b code-quality review.
- **Multi-Solana-entry handling in `pick_solana_accepts_entry`.** Today picks first Solana accepts entry (devnet vs mainnet undifferentiated). Paysh wire only advertises one entry per service today; flagged as monitor.
- **paysponge/perplexity routing.** Their `/v1/sonar` 402 advertises Base only, our Solana buyer correctly refuses. Phase 9.6 = route to EVM buyer when 402 says Base. Or wait for paysponge to add Solana.

---

**Branch state at deploy time:**
- 28 commits since spec
- 102 tests green in trading-oracle/trade-panel/paysh scope
- 0 known regressions
- 5 verified on-chain settlements
- $0.05 + cents in real spend
- twit.sh wallet (Base): ~$4.93
- gecko-buyer wallet (Solana): ~$4.95
