# Trading-Oracle Quickstart (Stub Mode) — 2 minutes

For partners and skill authors who want to call `gecko_trade_research` against the hosted MCP **without** wallet provisioning. Stub mode means the x402 gate is structurally real (you'll see proper 402 challenges) but settlement passes with any signature — no funds move, no keypair required.

> Live-mode flip happens later, on explicit founder go-ahead. Until then, the prod MCP runs `X402_MODE=stub` and every call is free in practice.

**Familiar pattern.** This is the same shape as `pay --sandbox claude` in [pay.sh](https://pay.sh/docs/get-started/agent-quickstart) — Solana Foundation's Pay CLI ships a sandbox flag that swaps in an ephemeral funded wallet so first-call testing doesn't burn USDC. We do the same thing server-side via `X402_MODE=stub`, so partners don't need a CLI install or wallet at all to validate the integration.

---

## Prerequisites

- Claude Code installed — https://claude.com/claude-code
- An empty (or any) project directory

That's it. No frames.ag account, no `~/.agentwallet/config.json`, no `install.sh`.

---

## 1. Drop a `.mcp.json` in the project root

```json
{
  "mcpServers": {
    "gecko": {
      "type": "http",
      "url": "https://api.geckovision.tech/mcp/"
    }
  }
}
```

That single file mounts the hosted Gecko MCP. Same endpoint everyone shares — no per-partner deploy.

## 2. Open Claude Code in that directory

It auto-discovers `.mcp.json` and mounts the server. No auth prompt, no wallet config.

## 3. Verify the tools loaded

```
What gecko tools do you have?
```

Expect: **20 tools**, including `gecko_trade_research`. The tool description ends with `Pays $0.25 basic / $0.75 pro via x402` — that's the live-mode price; in stub mode it's a no-op.

## 4. Call the basic tier

```
Use gecko_trade_research on Jito with the question "is the protocol healthy"
```

What happens under the hood:

1. Claude Code calls `POST /trade_research` on the hosted API.
2. API returns **402** with an x402 v2 challenge in the `payment-required` header.
3. MCP client's stub handler attaches a stub signature.
4. API verifies (stub-passes) and runs the 7-agent debate panel.
5. You get back a `TradePanelVerdict` with verdict (`act` / `pass` / `defer`), confidence, key drivers, blocker questions, and per-agent transcript — citations point at paysh / bazaar corpus chunks.

Expected latency: ~30–60s (7 LLM agents in series).

## 5. Call the pro tier

```
Use gecko_trade_research on Jito with tier=pro
```

Same as above, plus a `backtest` field. Pro tier hits the CoinGecko OHLCV history source and returns realized PnL on the Strategist's intent payload (entry/exit/horizon). If CoinGecko has no data for the protocol, `unbacktestable=True` with a reason — verdict still returns.

---

## What this proves to a partner

Without spending $1, a partner skill can:

- Mount Gecko's hosted MCP (one JSON file).
- Get grounded, debate-form trade research with citations.
- See the exact x402 challenge shape that will charge in live mode.
- Hand off the Strategist intent payload to a separate execution agent (e.g. `solana-claude` `defi-engineer`) — Gecko never touches keys.

---

## Going live (later)

When founder flips `X402_MODE=live`, the same `.mcp.json` keeps working. The only change a partner makes is provisioning a buyer wallet that can sign the 402 challenge and hold ~$1 USDC for a handful of calls. No code change in the skill.

Two wallet paths a partner can use:
- **Bring-your-own** — any x402-capable Solana wallet (frames.ag, awal, etc.). Per our wallet/facilitator neutrality rule, Gecko works above whichever the partner picks.
- **pay.sh CLI** — `brew install pay && pay setup && pay topup` provisions a self-custody wallet stored in OS keystore (macOS Keychain / GNOME Keyring / Windows Hello / 1Password). See [pay.sh install docs](https://pay.sh/docs/get-started/install). The same wallet then signs Gecko's 402 challenges.

See `memory/project_x402_stub_then_live.md` for the flip checklist.

---

## Validation checklist

- [ ] `.mcp.json` present in the project root.
- [ ] Claude Code reports `gecko_trade_research` in its tool list.
- [ ] First call returns a verdict citing at least one URL from `agentic.market` or `paysh.sh` providers.
- [ ] `tier=pro` call returns a `backtest` field (even if `unbacktestable`).

---

## Where to go next

- Reference skill: `gecko-claude/examples/trading-oracle/`
- Architecture map: `docs/strategy/2026-05-08-partner-integration-shape.md`
- Design spec: `docs/superpowers/specs/2026-05-08-trading-oracle-reference-skill-design.md`
