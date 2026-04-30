# Gecko Quickstart — 10 minutes to first paid call

Stranger to first receipt in ten minutes. Each step has a command and the output you should expect.

---

## 1. What you're getting (10 sec)

Gecko is an AI co-founder you drive from Claude Code. `gecko_research` returns a verdict (`go` / `pivot` / `kill`), a `gap_classification` against precedents, and cited sources — billed per call over x402 on Solana. `gecko_plan` runs a five-voice advisor panel (CEO, CTO, PM, Staff, Designer) on top of that session.

## 2. Prerequisites (1 min)

- Claude Code installed — https://claude.com/claude-code
- A frames.ag account + email (used for OTP)
- For mainnet: ~$1 SOL + ~$5 USDC. For devnet: nothing — see step 5.

## 3. Install (1 min)

```bash
curl -fsSL https://app.geckovision.tech/install.sh | bash
```

What it does:

- writes the Gecko MCP server config to `~/.claude.json`
- registers the `app.geckovision.tech/skill.md` skill
- creates `~/.agentwallet/config.json` with an empty `apiToken` placeholder

If `app.geckovision.tech` is not reachable yet, point at the `gecko-claude` repo's `install.sh` directly.

## 4. Connect Claude Code to Gecko (1 min)

Open Claude Code in any directory. Paste:

```
Read https://app.geckovision.tech/skill.md and follow it.
```

The skill will:

1. Email an OTP to your frames.ag address.
2. Prompt you to paste it.
3. Write the resulting `apiToken` into `~/.agentwallet/config.json`.

Verify:

```bash
cat ~/.agentwallet/config.json | jq '.apiToken' | head -c 20
```

You should see a non-empty string.

## 5. Fund the wallet (2 min)

**Devnet (free):**

- Open the frames.ag deposit page → "Request devnet airdrop" (USDC).
- For SOL gas: https://faucet.solana.com — paste the wallet address shown in frames.

**Mainnet:**

- Send ~$5 USDC + ~$1 SOL to the wallet address shown in the frames deposit page.
- Confirm balances with `bb doctor` (looks for ≥ $1 SOL, ≥ $5 USDC when `X402_MODE=live`).

## 6. First paid call (2 min)

In Claude Code:

```
Use gecko_research with idea="AI tool that drafts SOC 2 evidence from Linear + GitHub"
```

Sequence you should see:

1. 402 challenge with quoted price ($0.10 USDC for `basic` tier).
2. Wallet sign prompt from frames.
3. Indexing progress: "Discovering sources", "Indexing N sources", "Generating documents".
4. Final output:

```
Verdict: pivot
gap_classification: partial-overlap
gap_summary: Drata and Vanta cover the control framework but pull
             evidence from cloud configs, not from issue trackers.
             The Linear/GitHub angle is unclaimed.
Sources:
  [1] https://www.drata.com/product/automated-evidence
  [2] https://www.vanta.com/products/integrations
  [3] https://news.ycombinator.com/item?id=...
  ...
session_id: 7a4c...e1
tx_signature: 5KQv...8mP   (real Solana sig in live mode)
```

Save the `session_id`. You need it for the panel call.

## 7. First panel call (2 min)

```
Use gecko_plan with session_id=<id from step 6>
```

Five voices, $0.05 USDC. Closing lines (real example from the post-Sprint-8 Gecko self-research panel):

```
CEO   — "Ship the Linear connector first. Drata won't notice for two quarters."
CTO   — "Webhook ingest + signed event log. Don't try to be a SIEM."
PM    — "ICP is Series A startups already on Drata who are tired of screenshot uploads."
Staff — "The compliance moat is integrations, not LLMs. Budget accordingly."
```

(Designer voice landed in Sprint 9 — your run will show all five.)

## 8. Verify the receipt (1 min)

```bash
bb economics <session_id>
bb economics <session_id> --verify
```

`--verify` (Sprint 6) hits the Solana RPC and confirms the on-chain transfer. Status should be `confirmed`. In stub mode the signature is `stub_*` and `--verify` short-circuits to `stub_ok`.

---

## Appendix — common errors + fixes

| Error                                          | Fix                                                                                       |
| ---------------------------------------------- | ----------------------------------------------------------------------------------------- |
| `LiveX402Client.charge: NetworkMismatch`       | Server expects mainnet, your wallet is on devnet (or vice versa). Check `X402_NETWORK`.   |
| `LiveX402Client.charge: InsufficientBalance`   | Top up USDC. `bb doctor` prints current balance.                                          |
| `LiveX402Client.charge: FramesAuth`            | OTP expired. Re-run the skill from step 4.                                                |
| OpenRouter `429`                               | Rate limited. Wait 30s and retry — the CLI will not auto-retry to keep billing honest.    |
| `invalid model ID`                             | Confirm `LLM_ROUTER=openrouter` in `.env`. `bb doctor` flags this as a warning.           |
| `bb research` hangs after "Discovering sources"| Tavily quota or network. Check `TAVILY_API_KEY` and try `bb doctor`.                      |
| Anything else                                  | Run `bb doctor`. It is the single source of truth for env health.                         |

---

*Builder Bootstrap Platform · app.geckovision.tech*
