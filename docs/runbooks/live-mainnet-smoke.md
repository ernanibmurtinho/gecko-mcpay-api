# Live Mainnet Smoke Runbook

**Status:** Sprint 10 — S10-LIVE-02. Operator runbook for the **first paid
mainnet `bb research` round-trip**.
**Audience:** Ernani (or whoever flips the live switch). Read top to bottom.
**Predecessors:** Sprint 8 typed errors (S8-X402-01), Sprint 9 reconcile
hooks, Sprint 10 S10-LIVE-01 (`scripts/live_preflight.sh`),
S10-LIVE-03 (`NetworkKind` mismatch decoder).

> This runbook covers a **single one-shot** mainnet smoke. It does **not**
> flip `X402_MODE=live` in `.env` — mainnet is per-call only. To revert to
> devnet you simply omit the `X402_MODE` / `X402_NETWORK` overrides.

---

## 1. Pre-flight

Run the gated check from the repo root:

```bash
bash scripts/live_preflight.sh
```

Expected tail when all green:

```
[7/7] READY

Run the live smoke from this same shell:

  X402_MODE=live \
  X402_NETWORK=solana:EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v \
    bb --yes research --idea "Live mainnet smoke from Gecko"
```

If the script aborts, fix the surfaced cause and rerun. **Do not proceed**
with a `FAIL` line above your shell prompt.

---

## 2. The live command

Run from the **same shell** that just passed pre-flight:

```bash
X402_MODE=live \
X402_NETWORK=solana:EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v \
  bb --yes research --idea "Live mainnet smoke from Gecko" --tier basic
```

What each env var does:

| Var | Purpose |
|---|---|
| `X402_MODE=live` | Selects `LiveX402Client` instead of the stub. Mode is read by `gecko_core.payments.x402_client.get_client()`. |
| `X402_NETWORK=solana:<MAINNET_USDC_MINT>` | Resolves to the mainnet `NetworkConfig` and signals the frames.ag bridge to settle on mainnet. The mint is the canonical Circle USDC mint. |
| `bb --yes` | Skips the interactive confirmation prompt. Required because we already gated through `live_preflight.sh`. |
| `--tier basic` | Cheapest tier — keeps the smoke under $0.20. |

---

## 3. Expected output snippets

CLI will print (truncated for clarity):

```
[gecko] paying $0.15 USDC on solana-mainnet via frames.ag…
[gecko] tx submitted: 5KqJ...8rTk
[gecko] tx confirmed (status=confirmed) in 6.4s
[gecko] research session abc123-... created
[gecko] verdict: BUILD-NOW   gap_class: clear-gap
```

Two things to verify:

1. `tx_signature` is **not** prefixed with `stub_` — it is a real base58
   Solana signature. Cross-check at
   `https://solscan.io/tx/<tx_signature>`.
2. The session id in the last line — copy it for step 4.

---

## 4. `bb economics` verification

Confirm the receipt was persisted and reconciles against the chain:

```bash
bb economics <session_id> --verify
```

Expected output:

```
session:       <session_id>
status:        confirmed
tx_signature:  5KqJ...8rTk
network:       solana-mainnet
amount_usd:    0.15
treasury_balance_delta: +0.15 USDC   ← matches charge exactly
```

If `treasury_balance_delta` is `0` or negative, **stop** and investigate —
the on-chain settlement may have hit a different recipient.

---

## 5. Reconcile webhook

If `GECKO_RECONCILE_WEBHOOK_URL` is set in `.env`, the reconcile job
posts a JSON payload to that URL when the session moves from
`pending` → `confirmed`. Tail your webhook receiver (Slack channel,
internal endpoint, or `webhook.site` for ad-hoc) and confirm a payload
like:

```json
{
  "event": "payment.confirmed",
  "session_id": "abc123-...",
  "tx_signature": "5KqJ...8rTk",
  "network": "solana-mainnet",
  "amount_usd": "0.15",
  "confirmed_at": "2026-04-29T..."
}
```

No webhook configured = silent success; that's fine for the smoke.

---

## 6. Common errors and fixes

All errors below come from the typed `LiveX402Error` hierarchy in
`packages/gecko-core/src/gecko_core/payments/x402_client.py` (S8-X402-01).
The CLI surfaces them verbatim — do **not** rephrase before reporting.

### `NetworkMismatchError`

> `network mismatch — you're on devnet (mint=4zMMC9...), server expected mainnet (mint=EPjFWd...)`

**Cause:** Wallet network does not match the `X402_NETWORK` you passed
(decoded by `_resolve_network_kind`, S10-LIVE-03).
**Fix:** Either switch the frames.ag wallet to mainnet (re-run the
frames connect skill) or set `X402_NETWORK=solana:4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU`
to keep using devnet.

### `InsufficientBalanceError`

> `insufficient USDC balance for 0.15 USDC on solana-mainnet`

**Cause:** Wallet USDC < charge amount.
**Fix:** Top up via `https://frames.ag/u/<username>` or transfer USDC to
the wallet pubkey reported by `live_preflight.sh` step 3. Re-run pre-flight
to verify.

### `FramesAuthError`

> `frames.ag rejected the apiToken (likely expired or revoked).`

**Cause:** `~/.agentwallet/config.json` apiToken expired or was revoked.
**Fix:** Re-run the frames.ag connect skill:
`Read https://frames.ag/skill.md` and follow the OTP flow.

### `ConfirmationTimeoutError`

> `tx <sig> not confirmed within 60.0s on solana-mainnet.`

**Cause:** Tx submitted but not seen as `confirmed`/`finalized` within
60s. Usually a Helius/RPC blip or extreme leader rotation.
**Fix:** Check the signature directly at `https://solscan.io/tx/<sig>`.
If it landed, run `bb economics <session_id> --verify` — the reconcile
path will pick it up out-of-band. If it never landed, the funds are still
in the source wallet; re-run the smoke.

### `TreasuryNotConfiguredError`

> `GECKO_WALLET_ADDRESS is not set.`

**Cause:** `.env` missing `GECKO_WALLET_ADDRESS`.
**Fix:** Set it to the Solana pubkey you want to receive the charge. Note
`live_preflight.sh` step 5 catches this — if you got here, you skipped
pre-flight.

---

## 7. Reverting to devnet

There is **nothing to revert** if you only used per-call env overrides
(the supported path). Subsequent commands without `X402_MODE` /
`X402_NETWORK` set will fall back to:

- `X402_MODE=stub` (the default in `PaymentSettings`), or
- whatever `.env` says — typically `stub` or `solana-devnet`.

To explicitly force devnet for further testing in the same shell:

```bash
unset X402_MODE X402_NETWORK
# or, to test the live path on devnet:
export X402_MODE=live
export X402_NETWORK=solana:4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU
```

If you ever edited `.env` to flip mainnet on globally — **don't**. The
mainnet hard-flip is explicitly out of scope for Sprint 10 (see
`docs/build-plan-sprint-10.md` § "Out of scope").

---

## References

- `scripts/live_preflight.sh` — gate this runbook starts from
- `packages/gecko-core/src/gecko_core/payments/x402_client.py` — typed
  errors + `NetworkKind` resolver
- `docs/runbooks/mainnet-cutover.md` — full server-side cutover (different
  scope: deploys the API to mainnet; this runbook is about a single
  client-side smoke call)
- Sprint 8 S8-X402-01 commit — typed `LiveX402Error` hierarchy
- Sprint 10 S10-LIVE-03 commit — `NetworkKind` mismatch decoder
