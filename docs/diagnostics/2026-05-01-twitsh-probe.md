# twit.sh probe — wire shape, quality, and recommendation

- Ticket: `S14-TWITSH-01`
- Date: 2026-05-01
- Owner: `web3-engineer`
- Spend: ~$0.01 USDC on Base mainnet (2 paid calls × $0.005 estimate; actual
  402 challenge quotes 10000 base units = $0.01/call — see "Cost" below)
- Cap: $0.10 USDC (per task constraints) — not breached
- Mode: `TWITSH_BYPASS_CACHE=true`, single live `tweets/search` invocation
- Wallet: `0x7cc33a7BbA8409374f754f1f811BC63D1ea5bCFC` (Base mainnet, $5
  USDC pre-probe; not echoed beyond the address since the address itself
  is non-secret per `runbooks/twitsh-refill.md`)

## TL;DR

twit.sh **works**, returns X v2 API-compatible JSON, and is a viable paid
research-time source. The existing `gecko_core.sources.twit_sh.TwitshSource`
needed a one-line catalog correction (`/search/tweets` → `/tweets/search`,
`q` → `words`) which we fold into a `TwitshProvider` skeleton under the
Sprint 12 `SourceProvider` Protocol. **Recommend: ship as a paid provider in
Sprint 14**, gated to crypto/defi/hackathon-team categories with the
existing `_FIRES_FOR` set, alongside the Paragraph connector.

## What was probed

1. `GET https://x402.twit.sh/.well-known/x402` (free) — discovery doc lists
   23 paid resources; the relevant one is `/tweets/search`.
2. `GET https://x402.twit.sh/tweets/search?words=x402` (paid, 402-then-200) —
   one full payment + content round-trip via the `x402AsyncTransport` we
   already wire in `_build_x402_client()`.
3. `GET https://x402.twit.sh/tweets/search?words=x402+stablecoin+agent`
   (paid, 402-then-200) — second call to confirm shape stability and
   capture full author + meta envelope.

## Wire shape

### Discovery (`.well-known/x402`)

Free GET. Returns `{version, resources, ownershipProofs, instructions}`.
The 23 resources are the full bazaar surface; for Gecko V2/V3 the
research-time hits we care about are:

- `/tweets/search` — keyword filter, V2-compatible JSON, ~20/page, paginated
- `/tweets/user` — author timeline
- `/tweets/by/id` — single-tweet fetch (citation rehydration)
- `/articles/by/id` — long-form Twitter Articles (interesting for Sprint 14+)

### 402 challenge (search)

Server returns HTTP 402 with header `payment-required: <base64-jwt>`. The
decoded payload (relevant fields):

```json
{
  "x402Version": 2,
  "resource": {
    "url": "https://x402.twit.sh/tweets/search?words=x402",
    "description": "Search Twitter/X tweets with advanced filters …",
    "mimeType": "application/json"
  },
  "accepts": [{
    "scheme": "exact",
    "network": "eip155:8453",
    "amount": "10000",
    "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    "payTo": "0x9dBA414637c611a16BEa6f0796BFcbcBdc410df8",
    "maxTimeoutSeconds": 300,
    "extra": {"name": "USD Coin", "version": "2"}
  }],
  "extensions": {"bazaar": {"discoverable": true, "category": "api", …}}
}
```

`x402AsyncTransport` handles sign-and-replay transparently; we never see
the 402 in `httpx` user code.

### Paid response (search) — V2-shaped

```json
{
  "data": [
    {
      "id": "2049792149651075334",
      "text": "…",
      "created_at": "Thu Apr 30 10:05:06 +0000 2026",
      "author_id": "2002862554062987264",
      "conversation_id": "…",
      "lang": "en",
      "possibly_sensitive": false,
      "reply_settings": "everyone",
      "edit_history_tweet_ids": ["…"],
      "in_reply_to_user_id": "…",
      "public_metrics": {
        "retweet_count": 0,
        "reply_count": 0,
        "like_count": 0,
        "quote_count": 0,
        "bookmark_count": 0
      },
      "referenced_tweets": [{"type": "replied_to", "id": "…"}],
      "note_tweet": {"text": "…full long-form text if >280 chars…"},
      "author": {
        "id": "…",
        "username": "GalacticCatCoin",
        "name": "Galactic Cat",
        "description": "…",
        "verified": true,
        "verified_type": "blue",
        "profile_banner_url": "https://…",
        "entities": {…}
      }
    }
  ],
  "meta": {
    "next_token": "DAADDAABCgABHHJTGY9WMQYKAAIcZ1X5…"
  }
}
```

Key observations vs the assumptions baked into `twit_sh.py`:

- Top-level key is `data`, not `tweets`. The existing normalizer already
  falls back to `body.get("data")`, so this works as-is.
- Author is nested under `author` (full object) **and** mirrored as
  `author_id`. Username path: `data[].author.username`. The normalizer's
  `user.username` branch covers it.
- **No `url` / `tweet_url` / `permalink` field.** This is a gap — we have
  to synthesize the canonical URL ourselves:
  `https://x.com/{author.username}/status/{id}`. The current normalizer
  emits `url=""`, which would break Citation provenance. **Fix needed**
  in the provider.
- `note_tweet.text` carries the full long-form body when `text` is
  truncated. The current normalizer doesn't read it; we should prefer
  `note_tweet.text` when present.
- `public_metrics` has more keys than the normalizer reads
  (`quote_count`, `bookmark_count`). Not blocking — engagement summary is
  fine without them.

## Quality signal

Single `words=AI agent x402 payments stablecoin micropayment` query
returned 1 tweet — relevant (Kite AI agentic-payments thread). A broader
`words=x402 stablecoin agent` query returned multiple tweets, all on-topic
(x402 / agent payments / Kite / programmable wallets). Recency was strong:
results dated April 2026, weeks old — the live-signal angle Tavily can't
match.

Quality verdict for crypto/defi/hackathon-team ideas: **strong**.
Author handles are real, text is substantive (not bot-noise on the two
queries probed), `verified_type=blue` distinguishes paid blue-checks from
genuine verified-orgs (we should surface `verified_type` in
provenance to let downstream rubric weights penalize blue-check noise).

## Cost

- **Listed price:** 10000 base units of USDC (6 decimals) = **$0.01 / call**
  on `eip155:8453`. The source's `ASSUMED_PER_CALL_USD = 0.005` is
  off by 2× — the per-session cap of $0.05 buys 5 calls, not 10.
  Recommend updating the constant or sourcing the price from the 402
  challenge after the first call.
- **Settlement latency:** ~7.5 s wall-clock for the first paid call (cold
  signer + Permit2 sign + facilitator wait). Subsequent calls in the same
  session are faster. **Too slow for inline research** unless we
  parallelize against Tavily — which is exactly how `dispatcher.py`
  already runs sources.
- **Per-session worst case:** at $0.01/call × 5 calls = $0.05 cap, still
  in the existing budget. The free Tavily path stays the load-bearing
  source; twit.sh is a "+1 for crypto-flavored ideas" overlay.

## Errors / surprises

1. **Catalog drift.** `DEFAULT_CATALOG` in `twit_sh.py` ships
   `/search/tweets` and `q`. Real surface is `/tweets/search` and `words`.
   First probe call 404'd; the source returned `fired=False` cleanly and
   did not spend (good — the 404 came back before any 402 challenge).
   Catalog override via `TWITSH_CATALOG_JSON` env worked.
2. **No URL field.** Citation builders need a synthesized
   `https://x.com/{username}/status/{id}` — out-of-the-box Citations
   from the V1 source would have empty `url`s.
3. **Long-form note_tweet.** Default normalizer truncates to the 280-char
   `text`. We should prefer `note_tweet.text` for embedding context.
4. **Pagination.** `meta.next_token` exists; the V1 source ignores it
   (single-call loop with `range(1)`). Fine for now (10-tweet cap), but
   when we move to per-judge `userTweets`, paging will matter.
5. **No rate-limit headers seen.** Spend is the rate limiter — wallet
   running dry is the only "limit" surface.

## Recommendation: should Gecko consume twit.sh as a research-time source for V2/V3?

**Yes, ship in Sprint 14 as `TwitshProvider` (kind=`x402-bazaar`) alongside
Paragraph.** Conditions:

1. **Keep the category gate.** `_FIRES_FOR = {crypto, defi, hackathon-team}`
   — twit.sh is a high-signal source for those, near-noise for SaaS or
   regulated ideas. Don't burn $0.05 / session on a healthcare idea.
2. **Fix the catalog default** in-source (path + query param) so we don't
   need the env override in production.
3. **Fix the per-call price** to $0.01 (or read from the 402 challenge) so
   the cap math reflects reality.
4. **Synthesize the URL** in normalization so Citations carry a real
   permalink for the verdict renderer.
5. **Surface `verified_type`** in Provenance metadata so the rubric can
   discount blue-check noise vs verified-org signal.
6. **Run in parallel with Tavily**, not in series — the 7.5 s settlement
   latency is fine when overlapped, fatal in series.
7. **Wallet ops are already covered.** `runbooks/twitsh-refill.md`
   defines refill + alarm + kill-switch via `TWITSH_ENABLED=false`. No
   new ops debt.

### Trust angle

twit.sh is itself x402-native — same protocol Gecko speaks for paid
research. Consuming it cleanly is a story-fit dogfood beat: "Gecko pays
twit.sh in USDC over x402 to give you fresh X signal as part of your
research session." That's a stronger pitch than "we scrape Twitter."

## Follow-up tickets (Sprint 14)

- `S14-TWITSH-02` — Fix the V1 source defaults (path, query param,
  per-call price, URL synthesis, note_tweet preference). Tests via respx.
- `S14-TWITSH-03` — Wire `TwitshProvider` into `IngestionDispatcher`
  alongside `FreeProvider` for crypto/defi categories. Provenance
  carries `provider_name="twit_sh"`, `kind="x402-bazaar"`, payment
  receipt from x402 settlement.
- `S14-TWITSH-04` — Surface `verified_type` + engagement metrics into
  the rubric weighting layer so blue-check noise is discounted.
- `S14-TWITSH-05` — Pagination support (`next_token`) for per-judge
  `userTweets` composition. Defer until rubric weighting lands.
