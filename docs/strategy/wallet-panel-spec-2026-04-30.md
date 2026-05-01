# `bb wallet` panel — design spec

**Date:** 2026-04-30
**Sprint:** input for Sprint 14/15 implementation (spec only — no code in Sprint 12)
**Owner (spec):** product-designer
**Owner (implementation, future):** software-engineer + web3-engineer
**Predecessor:** `docs/runbooks/wallet-options.md` (S12-DOCS-01), `docs/research/cdp-bazaar-2026-04-30.md`

---

## Why this spec exists

By the end of Sprint 14, a power user may have **five wallets configured at once**:

1. **frames.ag** — Solana mainnet (default for Claude Code skill installs; pays for `bb research`)
2. **TWITSH** — Base mainnet (alternative payer-side wallet)
3. **Coinbase Agentic Wallet (`awal`)** — Base + Solana (CDP Bazaar buyer-side)
4. **publish.new** — Solana (auto-generated; receives artifact sale payouts)
5. **Paragraph creator wallet** — Solana (writer royalties / creator attribution surface)

Without a single command that surfaces all five, the UX fragments: balances live in five places, funding instructions live in five runbooks, failure modes are diagnosed by reading five config files. `bb doctor` (readiness check) and ad-hoc `cat ~/.gecko/wallet.json` is not a coherent answer for a user with five wallets.

This spec defines the `bb wallet` command — a single inspection surface that matches the visual style of `bb doctor` and lives next to it in the CLI surface area.

**Spec only.** No implementation in Sprint 12. This is design input for Sprint 14/15.

---

## Command surface

```
bb wallet                       # alias for `bb wallet show`
bb wallet show                  # full panel: all configured wallets, balances, health
bb wallet show --kind frames    # narrow to one wallet
bb wallet add <kind>            # interactive setup for a new wallet kind
bb wallet remove <kind>         # remove wallet config (confirms first)
bb wallet fund <kind>           # print funding paths (faucet links / onramp URLs)
bb wallet test <kind>           # send a 1-cent stub probe; report verify-only result
```

`<kind>` values: `frames` | `twitsh` | `awal` | `publish-new` | `paragraph` | `custom`

All subcommands respect `--json` for machine-readable output (parity with `bb doctor --json`).

---

## `bb wallet show` — terminal output

**Visual style:** Rich `Panel` containing a Rich `Table`. Match `bb doctor`'s table layout (column widths, dim metadata, color = meaning). Width-aware; render at 80, 120, 200 cols.

### Layout (120-col reference)

```
╭──────────────────────────────── Wallets ────────────────────────────────╮
│                                                                         │
│  Kind         Network          Address              Balance     Health  │
│  ─────────────────────────────────────────────────────────────────────  │
│  frames       solana:mainnet   9xKp…aF2Q  (default) 4.21 USDC   ok      │
│  twitsh       eip155:8453      0x7a3e…D91c          0.00 USDC   low     │
│  awal         eip155:8453      0x9b1f…E044          12.50 USDC  ok      │
│  publish-new  solana:mainnet   3pMr…uV8X  (receive) 0.85 USDC   ok      │
│  paragraph    solana:mainnet   not configured       —           —       │
│                                                                         │
│  Active payer for `bb research`: frames (solana:mainnet)                │
│  Active receiver for artifact sales: publish-new                        │
│                                                                         │
│  Run `bb wallet fund twitsh` to see funding paths.                      │
│  Run `bb wallet add paragraph` to wire creator attribution.             │
│                                                                         │
╰─────────────────────────────────────────────────────────────────────────╯
```

### Column rules

- **Kind** — fixed label set; lowercase.
- **Network** — CAIP-2 form (`solana:mainnet`, `eip155:8453`). Matches `X402_NETWORK` env values exactly so users can copy/paste.
- **Address** — first 4 + last 4 chars of the wallet address; truncate middle with `…`. Tag `(default)` for the active payer; `(receive)` for receive-only wallets.
- **Balance** — USDC balance fetched live from chain RPC. Cache 30s. If RPC unreachable, render `?` and surface the failure in Health.
- **Health** — `ok` (green), `low` (yellow, balance < $0.10), `unreachable` (yellow, RPC error), `misconfigured` (red, missing apiToken / signer), `—` (dim, not configured).

### Footer hints

Below the table, render two footer lines:
- "Active payer for `bb research`: <kind> (<network>)" — resolved from `X402_NETWORK` + factory choice.
- "Active receiver for artifact sales: <kind>" — resolved from publish.new config (Sprint 14).

Then up to **two** actionable hints. Drawn from this priority order:
1. Any wallet in `low` health → "Run `bb wallet fund <kind>` to see funding paths."
2. Any unconfigured wallet kind that the active flow needs → "Run `bb wallet add <kind>` to wire <feature>."
3. Any wallet in `misconfigured` → "Run `bb wallet show --kind <kind>` for diagnostics."

Never more than two hints. Never zero — if everything is fine, render "All wallets healthy. `bb research --idea \"...\"` to start." in dim.

### Color rules

- `ok` → green
- `low`, `unreachable` → yellow
- `misconfigured` → red
- `—` (not configured) → dim
- All metadata (addresses, network names) → dim
- Headers → bold

No emoji. Hierarchy via Rich `Table` + `Panel` only.

---

## `bb wallet add <kind>` — interactive setup

Each wallet kind has a different setup story (OTP, CLI tool, browser flow). The `add` subcommand is a thin orchestrator that:

1. Confirms the user wants to add this kind (one-line `Confirm` prompt).
2. Runs the kind-specific setup helper:
   - `frames` → opens browser to frames.ag bootstrap; user pastes apiToken back
   - `twitsh` → prompts for phone/email; runs OTP flow against `twitsh.app/api`
   - `awal` → shells out to `npx awal init` with friendly framing
   - `publish-new` → auto-generates locally; shows recovery key once
   - `paragraph` → opens browser to Paragraph creator settings; user pastes wallet address
   - `custom` → opens `$EDITOR` on a YAML stub
3. Persists the config to `~/.gecko/wallets.toml` (one TOML file with one `[<kind>]` table per configured wallet — single source of truth, replaces per-wallet JSON files).
4. Runs `bb wallet test <kind>` automatically as a smoke step.
5. Prints the resulting row from `bb wallet show --kind <kind>`.

### Failure modes

- Kind already configured → ask before overwriting (destructive; require `--force` to skip prompt).
- Network unreachable during test → set health to `unreachable`, do not block the add (config still persists).
- apiToken rejected by facilitator → set health to `misconfigured`, surface the exact error from the facilitator.

---

## `bb wallet fund <kind>` — funding paths

Prints a Rich `Panel` with the funding rails for that kind, lifted from `docs/runbooks/wallet-options.md`. Example:

```
╭─────────────────── Fund: twitsh (eip155:8453) ──────────────────╮
│                                                                  │
│  Address: 0x7a3eC…D91c                                           │
│  Current balance: 0.00 USDC                                      │
│                                                                  │
│  Funding paths:                                                  │
│    1. Coinbase Onramp     https://onramp.coinbase.com/...        │
│    2. Base bridge         https://bridge.base.org/                │
│    3. Direct USDC send    (Base mainnet, USDC contract 0x833…)   │
│                                                                  │
│  Faucet (Sepolia only):   https://www.coinbase.com/faucets/base  │
│                                                                  │
╰──────────────────────────────────────────────────────────────────╯
```

No browser auto-open. Print the URLs; let the user click. (Auto-open is brittle on headless / SSH / CI machines, and Gecko runs in those contexts.)

---

## First-run behavior (no wallet configured)

When the user runs **any** paid command (`bb research --tier basic`, `bb research --tier pro`, `bb plan`) and `~/.gecko/wallets.toml` does not exist or has zero configured wallets:

1. Stop before any work begins.
2. Render a Rich `Panel` titled **"No wallet configured"**.
3. Body:
   ```
   Gecko needs a wallet to settle x402 payments.
   Cheapest path to first call: TWITSH on Base mainnet (~1 minute).
   Default for Claude Code skill users: frames.ag on Solana.

   Choose:
     [1] frames.ag    — Solana mainnet, browser flow
     [2] twitsh       — Base mainnet, OTP, fastest
     [3] awal         — Coinbase Agentic Wallet
     [4] custom       — bring your own
     [5] cancel
   ```
4. The user picks; control transfers to `bb wallet add <kind>` interactive flow.
5. After successful add, the original command resumes from the start.

`X402_MODE=stub` short-circuits this entirely — stub mode never prompts for a wallet. That keeps the "try the flow without spend" Free tier (landing copy §8) clean.

---

## How `bb doctor` and `bb wallet` interact

These are sibling commands with different jobs. They must **not** duplicate output.

| Command | Job | Output |
|---|---|---|
| `bb doctor` | Readiness check — can the user run a paid call right now? | Pass/fail per dependency: API keys present, Supabase reachable, OpenAI reachable, Tavily reachable, **at least one wallet healthy for the active network**. Exits non-zero on failure. |
| `bb wallet` | State inspection — what wallets exist and how are they doing? | Full per-wallet table, balances, funding hints. Exits zero unless explicitly asked to test. |

**Specifically:** `bb doctor` shows one line for wallet status (e.g. `wallet: ok (frames, solana:mainnet, 4.21 USDC)`) and links to `bb wallet` for detail:

```
wallet     ok      frames on solana:mainnet (4.21 USDC) — `bb wallet` for all
```

`bb doctor` does **not** list every wallet. It picks the active payer for the current `X402_NETWORK` and reports on that one. The full picture lives in `bb wallet`.

If `bb doctor` finds the active payer is `low`, `unreachable`, or `misconfigured`, it fails with an actionable error pointing to `bb wallet fund <kind>` or `bb wallet add <kind>`.

---

## Storage shape

Single TOML file: `~/.gecko/wallets.toml`.

```toml
default_payer = "frames"          # which wallet the factory picks per-network
default_receiver = "publish-new"   # for artifact sales (Sprint 14)

[frames]
network = "solana:mainnet"
address = "9xKp...aF2Q"
api_token_env = "FRAMES_API_TOKEN"  # never store the token inline; reference an env var
added_at = "2026-04-15T12:00:00Z"

[twitsh]
network = "eip155:8453"
address = "0x7a3e...D91c"
api_token_env = "TWITSH_API_TOKEN"
added_at = "2026-04-28T09:30:00Z"

[awal]
network = "eip155:8453"
address = "0x9b1f...E044"
api_token_env = "AWAL_API_KEY"
added_at = "2026-04-29T14:15:00Z"

[publish-new]
network = "solana:mainnet"
address = "3pMr...uV8X"
mode = "receive-only"
added_at = "2026-04-30T10:00:00Z"
```

Tokens / private keys are **never** stored in the TOML. Always referenced by env-var name and read at runtime. This matches the security non-negotiables in `CLAUDE.md` (no secrets in tracked files).

---

## Out of scope for the spec

- Multi-signer / hardware-wallet flows (custom kind only — community responsibility)
- Cross-wallet rebalancing automation (e.g. "auto-bridge USDC from Base to Solana when balance is low")
- Web UI for wallet management — `gecko-mcpay-app` may surface a read-only view in V3, but the source of truth is the TOML
- Per-call wallet selection at the `bb research` invocation level (could add `--wallet <kind>` later; V1 uses `default_payer`)

---

## Implementation notes for Sprint 14/15

- Live in `apps/cli/wallet.py` (new). Logic in `packages/gecko-core/wallets/` (new): config IO, balance fetch, health check.
- The factory in `packages/gecko-core/payments/__init__.py` reads `wallets.toml` to resolve the per-network payer, replacing the current env-var-only resolution.
- Reuse the Rich `Table` + `Panel` style from `bb doctor` (extract to a shared `apps/cli/ui.py` helper if not already factored).
- Tests: snapshot the rendered panel at 80/120/200 cols against fixtures with 0, 1, and 5 wallets configured.
- Live balance fetch uses the same RPCs already configured for x402 settlement — no new RPC dependency.

---

## See also

- `docs/runbooks/wallet-options.md` — supported wallets, funding paths (the data this panel surfaces)
- `docs/build-plan-sprint-12.md` § Track D — the ticket this spec was written for
- `docs/strategy/bazaar-deeper-thesis-2026-04-30.md` — why wallet neutrality is core thesis
- `apps/cli/doctor.py` — visual style reference (`bb doctor`)
