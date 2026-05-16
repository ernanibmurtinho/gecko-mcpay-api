# Live buyer-path recorded fixtures

Cassette fixtures for the Solana buyer-side x402 client
(`LiveBuyerX402Client`). CI replays these; live re-record requires:

```
GECKO_LIVE_SOLANA=1 \
BUYER_WALLET_PRIVATE_KEY_PATH=/secrets/buyer.json \
BUYER_WALLET_PUBKEY=<pubkey> \
X402_SELLER_PAYTO=<seller-pubkey> \
uv run pytest tests/integration/test_live_buyer_path.py -m live_solana --record
```

Pattern C (CLAUDE.md): adding any new live buyer-path code is gated on a
fixture-replay test passing. The `live_solana` marker is the template
copy of `live_cdp` from S12.5-TEST-04.

## Fixture files

- `buyer_charge_success.json` — full happy path: build + sign + submit +
  confirmation poll until `finalized`. Generated 2026-05-20 from the
  Day-9 flip smoke run.
- `buyer_insufficient_balance.json` — facilitator/RPC error path when
  the buyer wallet has < requested USDC.
- `buyer_confirmation_timeout.json` — submitted but never confirmed
  within 60s (synthetic — induced by mocking RPC to return null).

Each fixture is JSON with `{request, response}` pairs keyed by RPC
method. The replay client matches on `method + params[0]` so deterministic
playback never requires real-time signatures to match.
