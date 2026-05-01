# Live CDP Bazaar smoke — first paid Base mainnet settle + Bazaar listing

**Status:** Sprint 12 Track C — S12-LIST-01.
**Audience:** operator (Ernani or whoever flips the listing live).
**Predecessors:** Sprint 12 Track A (`CDPX402Client`), Track B (Bazaar route declarations + `/.well-known/x402` metadata).

> **One-shot operator runbook.** Spends ~$0.10 USDC on Base mainnet from your TWITSH wallet. Lands a real `tx_signature` from CDP Facilitator. Triggers Gecko's first appearance in `https://api.cdp.coinbase.com/platform/v2/x402/discovery/search`.

---

## 0. Before you start — one-time seller setup

You only need to do this once.

**A. CDP API keys**

1. Go to `https://portal.cdp.coinbase.com`. Sign in.
2. Create an API key for the gecko-api project. Save the key ID + secret somewhere safe (1Password, a vault — not in shell history).
3. The key needs `x402.facilitator.write` permission to settle payments.

**B. Gecko Base treasury address**

`GECKO_WALLET_ADDRESS_BASE` is where Base mainnet USDC payments land. You have two clean options:

- **Option (a): Pay yourself.** Use your TWITSH wallet address as the treasury. The same wallet pays AND receives. Net spend = 0 (minus gas). Simplest for V1 smoke. Works because TWITSH wallet is just an address — it can pay AND receive USDC.
- **Option (b): Fresh wallet.** Create a new Base wallet (Coinbase Wallet, Rabby, etc.) and use its 0x address. Cleaner separation of buyer/seller for production. Requires you to track two wallets.

**Recommend (a) for the smoke**, then swap to (b) once we have actual paying users.

---

## 1. Configure gecko-api (deployed)

The deployed gecko-api at `api.geckovision.tech` needs three new env vars in production:

```
X402_MODE=cdp
CDP_API_KEY_ID=<from step 0A>
CDP_API_KEY_SECRET=<from step 0A>
GECKO_WALLET_ADDRESS_BASE=<your TWITSH 0x address (option a) or fresh wallet (option b)>
```

How to set these depends on the deploy target. Check `Dockerfile` + `ecs-stack.yml` (per the historical commit `cbc5cfb` deploy stack) or wherever production env is currently configured.

Restart gecko-api after env changes propagate.

**Verify deployed config:**

```bash
curl -sI https://api.geckovision.tech/.well-known/x402 | head
```

Expect `200 OK` and the body should contain `"facilitator": "cdp"` and per-route `bazaarExtension` blobs (Sprint 12 Track B).

---

## 2. Pre-flight from your local machine

```bash
cd ~/PycharmProjects/Gecko/gecko-mcpay-api
set -a; source .env; set +a

# Confirm TWITSH credentials are loaded
echo "TWITSH wallet: $TWITSH_WALLET_ADDRESS"
# Should print 0x...

# bb doctor end-to-end check
bb doctor
```

`bb doctor` should show:
- 6 rows green
- x402 row: `mode=cdp network=eip155:8453 facilitator=cdp-base`
- treasury matches your `GECKO_WALLET_ADDRESS_BASE`

If any row is red, stop here.

---

## 3. The smoke — one paid call

```bash
X402_MODE=cdp \
X402_NETWORK=eip155:8453 \
bb --yes research \
  --idea "Live mainnet CDP Bazaar smoke from Gecko" \
  --tier basic
```

Expected output:
- Spinner runs ~30-60s (basic tier)
- Final block prints `VERDICT ─────── KILL|REFINE|BUILD` + `Gap: <typed_gap>` sub-line (Sprint 11 Track A)
- Cost receipt prints `tx_signature: 0x...` (NOT `stub_`-prefixed) — that's a real Base mainnet settlement
- Session ID printed

Save the `session_id` for verification.

---

## 4. Verify the receipt

```bash
bb economics <session_id> --verify
```

Expected:
- `status: confirmed`
- `tx_signature` matches what step 3 printed
- `paid_to` matches `GECKO_WALLET_ADDRESS_BASE`
- `network: eip155:8453`

If `--verify` fails, the payment landed but reconcile broke — check Sprint 12 Track A's `_map_response` mapping.

---

## 5. Wait for Bazaar indexing (5-30 min)

CDP Bazaar indexing is asynchronous. From the docs:

> If your service does not appear in CDP Bazaar discovery, ensure at least one successful settlement has completed through the CDP Facilitator.

Step 3's settle should trigger this. Indexing typically completes within 5-30 min.

---

## 6. Verify Gecko is listed

Probe semantic search for the slot Gecko should occupy:

```bash
curl -s "https://api.cdp.coinbase.com/platform/v2/x402/discovery/search?query=founder+validation&limit=10" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); [print(r['resource']) for r in d.get('resources',[])]"
```

Expect to see `https://api.geckovision.tech/research` (or the route you settled) in the result list.

Repeat for:

```bash
# semantic queries Gecko should rank well on
for q in "founder+validation" "product+research+MCP" "adversarial+PRD" "kill+refine+build"; do
  echo "=== $q ==="
  curl -s "https://api.cdp.coinbase.com/platform/v2/x402/discovery/search?query=$q&limit=5" \
    | python3 -c "import json,sys; d=json.load(sys.stdin); [print(r['resource']) for r in d.get('resources',[])]"
done
```

Also check the merchant view:

```bash
curl -s "https://api.cdp.coinbase.com/platform/v2/x402/discovery/merchant?payTo=$GECKO_WALLET_ADDRESS_BASE" \
  | python3 -m json.tool
```

This returns every Gecko route Bazaar has cataloged for our wallet.

---

## 7. Acceptance

Sprint 12 Track C closes when:

- [ ] One real Base mainnet settlement landed with a non-stub `tx_signature`
- [ ] `bb economics --verify` returns `status: confirmed`
- [ ] At least one Gecko route appears in `/discovery/search?query=founder+validation`
- [ ] Merchant lookup at `/discovery/merchant?payTo=...` returns ≥ 1 cataloged route
- [ ] `docs/eval/cdp-bazaar-listing-2026-04-30.md` (or current date) captures: settle tx hash, indexing latency, ranking position vs OrbisAPI proxies (per `docs/research/cdp-bazaar-2026-04-30.md` landscape probe)

---

## 8. Known follow-ups (out of scope for this smoke)

- **Multiple route listings:** today's smoke registers `/research`. To list `/plan`, `/advise`, etc., settle one payment per route. They all auto-catalog via the same Track B Bazaar metadata.
- **Quality ranking compounding:** every paid Gecko call from any source (Claude Code skill users, agents discovered via Bazaar) feeds the ranking signal. No additional work needed.
- **Sprint 14 Paragraph integration:** publish.new uses the same CDP-compatible x402v2 contract. Once Track C lands, we have proven Base mainnet plumbing for both directions.

---

## 9. If something goes wrong

| Symptom | Likely cause | Fix |
|---|---|---|
| `bb doctor` x402 row shows `facilitator=stub` | env vars not loaded or `X402_MODE` not set | re-source `.env`; verify `X402_MODE=cdp` in shell |
| `tx_signature` is `stub_xxx` | client fell back to stub mode | check `X402_MODE=cdp X402_NETWORK=eip155:8453` BOTH passed |
| Settle returns `InsufficientBalance` | TWITSH wallet doesn't have enough USDC on Base | top up via Coinbase / DEX |
| `bb economics --verify` returns `pending` after 5 min | Base mainnet block congestion or CDP confirmation lag | wait another 5 min; if still pending, check tx on `basescan.org` |
| Gecko not in `/discovery/search` after 30 min | Bazaar didn't index | check `EXTENSION-RESPONSES` header on the settle response (Sprint 12 Track B); should show Bazaar status `processing` or `accepted` |
| Bazaar shows `status: rejected` | extension JSON Schema validation failed | inspect extension shape against `tests/api/test_bazaar_extensions.py` (Track B harness) |
