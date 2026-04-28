# Gecko ↔ frames.ag — Builder-to-Builder Coordination

**From:** Gecko (geckovision.tech)
**To:** frames.ag platform team
**Date:** 2026-04-28
**Status:** Open proposal — non-blocking on either roadmap

---

## TL;DR

Gecko already runs on frames.ag wallets. Pro tier ($0.75 USDC, x402 on Solana) settles through the same `apiToken` you issue. We'd like to deepen that integration with two read-only context endpoints. They unlock per-builder personalization in our agent debate, drive more frames.ag wallet activity per Gecko session, and cost you near-zero to ship — the data already lives in your Helius pipeline.

If it's a fit, great. If not, we have fallbacks (twit.sh + Helius direct) and ship V1 either way. This is a proposal, not a blocker.

---

## What Gecko is, in one paragraph

Builder Bootstrap Platform. A founder describes an idea, pays $0.75, and 90 seconds later has a verdict (ship / kill / pivot), three docs (PRD, product story, GTM), and named comparables — produced by a 5-agent AG2 debate grounded in HN, Reddit, GitHub, Colosseum, twit.sh judges, and our own session flywheel. Distribution is Claude Code skills (frames-style: `Read app.geckovision.tech/skill.md` → bootstrap → use). Pro is live on Solana mainnet; verdict accuracy is at 0.85 across our eval suite.

## Why this is a flywheel, not a favor

| Frames.ag gets | Gecko gets |
|---|---|
| More $/wallet/month — Pro sessions are recurring once a builder has product-market fit | Persona-grounded prompts; the judge agent stops asking "who is this user?" |
| A reference customer using your platform end-to-end (skill distribution + wallet + x402) | Authoritative builder context without asking users to paste wallets |
| Co-marketed dogfood story: "frames.ag wallet → Claude Code skill → on-chain settlement" | Better verdicts → more session reruns → more wallet top-ups |
| Zero new pipelines — both endpoints aggregate data you already index | Differentiated "Max" tier built around frames.ag enrichment |

We're proposing this read as a peer integration, not vendor onboarding.

---

## What we're asking for

Two read-only endpoints, gated by the bearer `apiToken` you already issue.

### Endpoint 1: `GET /v1/user/context`

```json
{
  "user_id": "fa_u_01J...",
  "display_name": "ernani",
  "wallet_pubkey": "Sol...",
  "connected_handles": {
    "x": "@ernani",
    "github": "ernani",
    "farcaster": "ernani.eth"
  },
  "account_age_days": 312,
  "verified_at": "2026-01-15T..."
}
```

### Endpoint 2: `GET /v1/user/onchain-summary`

Abstracted footprint sourced from the Helius integration you already run.

```json
{
  "programs_deployed": 3,
  "primary_categories": ["nft-tooling", "defi"],
  "tvl_across_protocols_usd": 4200,
  "first_tx_at": "2024-09-01T..."
}
```

Field-level negotiable. Coarser is fine; we can derive the rest.

---

## What Gecko commits in return

- **Attribution surface.** "Persona grounded by frames.ag" line in every Pro session result page and PDF export.
- **Co-marketing.** Joint launch post when Max tier ships (Sprint 3, ~3 weeks). We position frames.ag as the wallet + identity layer, not just a payment rail.
- **Privacy guarantees:**
  - Raw response bodies never persist beyond the session row.
  - Signal compresses into our `user_context.builder_profile` block; pubkeys / handles never logged or shipped to third parties.
  - Per-user opt-out at `/v1/me/preferences`; we do not call your endpoints for opted-out users.
- **Reciprocity hook.** When Gecko's flywheel surfaces "builders like you shipped X," we'll pipe an aggregate (no PII) back to frames.ag if useful for your tool registry signal.

---

## Timeline

- **Ideal:** spec agreed by mid-Sprint-3 (~2 weeks). Live by Sprint 4.
- **Acceptable:** Sprint 4 (~5 weeks).
- **No-go:** we ship V1 without it. Fallback below.

## Fallback if no

- **twit.sh** covers public X-handle signal (already integrated for V1).
- **Helius direct** covers Solana on-chain history at our Premium tier (already integrated).
- The unique slot only frames.ag fills: asserting "this user has shipped 3 programs" without making the user paste a wallet — because their session already proved it.

---

## Open to

- Bilateral sync (30 min) — happy to walk you through the Pro tier debate and where your context lands in prompts.
- A pilot scoped to ~50 frames.ag-authenticated Pro sessions before any GA commitment.
- Endpoint shape that's easier for you to ship than what we've sketched. The data shape is negotiable; the unlock is the question.

---

## Send instructions

**Recommended primary channel: X DM to the frames.ag founder/team handle** linked from `https://frames.ag/` footer (most x402-native teams default to X for partnership intros and respond fastest there).

Concrete steps the user clicks:

1. Open `https://frames.ag/` and scroll to footer / hero — capture the official X handle linked from the site (the team brands themselves on X for builder discovery, not via a corporate contact form).
2. Open `https://github.com/frames-engineering` — note the maintainer GitHub usernames; their X handles are usually linked from their GitHub profile READMEs. Cross-check those handles match the site footer.
3. Send an X DM to the official handle with the opening:
   > "Builder of geckovision.tech here — we ship Pro on frames.ag wallets + x402 today. Drafted a one-pager on a context-API integration that drives more $/wallet for both sides. OK to share?"
4. **If no response in 48h**, fall back to opening a GitHub Discussion (or Issue if Discussions disabled) on a public `frames-engineering/*` repo, titled `Partnership: Gecko x frames.ag context API proposal`. Public surface forces a routing decision on their side.
5. **If still cold after 4 days**, ship V1 without it. The fallback (twit.sh + Helius direct) covers 80% of the unlock.

**Why X DM over email:** frames.ag has no published partnerships email and no contact form on the apex page. Their team operates publicly on X (consistent with the x402-native builder pattern). An X DM signals we're peers in the same ecosystem; an email to a generic `hello@` would land in a triage queue that may not exist.

**Do not use:** generic `info@` / `hello@` guesses, or LinkedIn InMail (slower, off-pattern for this audience).

---

**Contact (Gecko side):** ernanibmurtinho@gmail.com · `@ernani` on X · `geckovision.tech`
