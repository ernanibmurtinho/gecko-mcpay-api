# Trade-agent pricing model — design doc (2026-05-11)

**Owner:** business-manager
**Audience:** technical co-founder (Ernani) + product-designer co-founder
**Status:** proposal for founder sign-off; gates v0.1 live launch (not v0.1 dev)
**Source decision:** `project_trade_vertical_v01_decisions_2026_05_11.md` #4 — "every agent call is paid; pricing pass-through covers infra. No per-agent caps."

---

## 1. TL;DR

Keep **Shape A (per-call only)** for v0.1: **$0.25 basic verdict / $0.75 pro verdict / $0 cache hit**. No agent-hour rental, no bundles, no per-agent caps. A typical vibe-trader advisor day costs **~$1.50–$3.00** (1 startup-pro + 1 daily-basic + ~3 triggered baselines, most of which are cache hits). That fits the "≤ $5/day, < 1.5% of $500 working capital" outcome metric from the co-founder brief §2.3, and our gross margin at v1.0 scale lands at **~55–65%** before fixed overhead. Move to bundles (Shape C) at v1.0 only after one full sprint of cost telemetry — bundles without usage data are a hedge we can't price.

## 2. Cost of goods — per-unit math

### 2.1 Rate card (sourced)

| Input | Rate | Source |
|---|---|---|
| Voyage `voyage-3` embedding | $0.06 / 1M tokens | voyageai.com/pricing (Jan 2026 card) |
| Voyage `rerank-2` | $0.05 / 1M tokens | voyageai.com/pricing |
| `gpt-4o-mini` | $0.15 in / $0.60 out per 1M tokens | openai.com/pricing |
| `gpt-4o` | $2.50 in / $10.00 out per 1M tokens | openai.com/pricing |
| Mongo Atlas M10 (dedicated) | $0.08/hr ≈ $58/mo flat | mongodb.com/pricing — current shared-tier ceiling |
| Atlas Vector Search reads | included in cluster compute | no per-query meter on M10 |
| Helius Developer plan | $49/mo, 25M credits | helius.dev/pricing |
| Pyth Hermes SSE | free | pyth.network/developers |
| Solana mainnet gas | ~$0.0001/tx | typical observed |
| x402 facilitator (paysh / Coinbase) | ≤ 0.5% of settlement | paysh.fun + cdp.coinbase.com docs |

Where I can't cite a precise rate, I mark `[est]` and give a sensitivity range.

### 2.2 Per-call COGS — oracle (`gecko_trade_research`)

| Component | Basic call | Pro call |
|---|---|---|
| Voyage embedding (query, ~50 tokens) | $0.000003 | $0.000003 |
| Voyage rerank (top-50 chunks @ ~200 tok ea) | — | $0.0005 |
| Atlas Vector Search read | $0 marginal | $0 marginal |
| LLM panel — basic (single 4o-mini pass, ~3k in / 1.5k out) | $0.0014 | — |
| LLM panel — pro (7 turns AG2; mix: 5×4o-mini @ 4k in/2k out, 2×4o @ 6k in/2k out) | — | ~$0.054 |
| x402 facilitator fee (0.5% of $0.25 / $0.75) | $0.00125 | $0.00375 |
| Solana gas | $0.0001 | $0.0001 |
| **Total marginal COGS** | **~$0.003** | **~$0.058** |
| **Sale price** | **$0.25** | **$0.75** |
| **Gross margin (per call, ignoring fixed)** | **98.8%** | **92.3%** |

**Caveat:** that margin ignores the fixed Mongo + Helius monthlies. Folded in at scale below.

### 2.3 Per-agent-day COGS — advisor mode (v0.1)

Re-verdict cadence per `2026-05-11-trade-vertical-expansion.md` §5: 1 startup-pro (once, amortised at agent spin-up) + 1 daily-basic + ~3 triggered/day average; cache-hit ratio target **≥ 60%** (alert at 30% per the design spec). Helius and Mongo are amortised fixed cost spread across the active user base.

| Component | Cost/day per agent | Notes |
|---|---|---|
| Oracle calls (1 basic + ~1.2 triggered cache-misses) | $0.25 + $0.30 = **$0.55** | startup-pro amortised below |
| Startup pro verdict, amortised over avg 7-day spec life | $0.75 / 7 = **$0.11** | [est] — revise when telemetry lands |
| Helius credits (websocket subs, ~10 tokens × 24h) | ~$0.05 | [est] — at $49/25M credits, ~25k credits/day/agent |
| Mongo Atlas amortised | $58 / (active agents) | $0.58/day if 100 agents; $0.06/day if 1,000 |
| In-process compute (user's laptop) | $0 to us | externality — user pays power + RPC |
| Pyth | $0 | free |
| **Marginal cost to Gecko per agent-day @ 100 agents** | **~$1.29** | |
| **Marginal cost to Gecko per agent-day @ 1,000 agents** | **~$0.77** | |
| **Revenue per agent-day (per-call passthrough)** | **~$1.50–$3.00** | bracket from §5 of design spec |

Gross margin per agent-day: **~50–65% at 100 agents; ~70–80% at 1,000 agents.**

### 2.4 Per-agent-day COGS — trader mode (v0.2, indicative)

Same as advisor + execution layer. New components:

| Component | Cost/day per agent (v0.2) | Notes |
|---|---|---|
| All of §2.3 | $0.77–$1.29 | unchanged |
| OKX adapter fee (passthrough) | venue-set, ~5–25 bps of trade volume | not Gecko revenue |
| Higher Helius credit burn (transaction subs, not just account) | ~$0.10–$0.20 [est] | scales with position turnover |
| Additional oracle re-verdicts on entry-gate misses | +$0.50–$1.00 [est] | trader mode triggers more often than advisor |
| **Marginal cost to Gecko** | **~$1.50–$2.50/day** | |
| **Revenue (per-call)** | **~$2.50–$5.00/day** | |

Still inside the ≤$5/day envelope but tighter. The trader-mode pricing decision is partially deferred (see §7).

## 3. Three pricing shapes evaluated

| | **A. Per-call only (current)** | **B. Per-call + agent-hour rental** | **C. Bundled tiers** |
|---|---|---|---|
| Shape | $0.25 basic / $0.75 pro per call, $0 cache-hit, no other charge | Same per-call price + flat $0.10/hr ($2.40/day) per running agent | $5/day, $20/week, $50/month — includes N oracle calls + Y agent-hours |
| Right for | Light users; coach-only buyers; trial users; first-week dogfooders | Power users with multiple simultaneous agents; lets us cap our infra exposure | Predictable buyers; converts trial to recurring; sets up retention metric |
| Overcharges | Heavy users mass-deploying agents during volatile days | Light users who let one agent run idle on a quiet day (paying $2.40 for $0.30 of value) | Light users who pay $50/mo and run 6 verdicts |
| Undercharges | Heavy multi-agent users sitting on cache hits (we eat Helius cost, they pay nothing) | Light users who rarely trip cache; close to break-even | Heavy users who burn through allowance day 5 then get capped (or we overage them — bundle-buster) |
| Cash flow shape | Lumpy, follows user activity | Recurring on rental + lumpy on calls | Predictable, monthly |
| Infra exposure | Fully variable — if Voyage 10x, our margin compresses but per-call still positive | Hedged by rental floor | Bundle is a fixed price; rate changes eat our margin directly |
| Intelligibility | High — "you paid for what you got" | Medium — two-line bill | Low for first-time buyer, high for repeat |
| Aligns with KaaS-oracle positioning | Strong — every call returns evidence | Weak — rental fee implies SaaS lock-in we explicitly reject (§1.6 co-founder brief) | Medium — bundle is fine, but commitment shape rhymes with SaaS |
| Aligns with founder cash position | Best — no AR risk; settlement is per-call | Good — recurring floor | Risky — if we overpromise allowances and Voyage hikes rates, we eat it |
| Defensibility against frames.ag / Bazaar | Strong — they don't have a bundle either | Weak — opens "Bazaar charges $0.05, you charge $2.40/day" comparison | Medium |

## 4. Recommendation

**Adopt Shape A for v0.1. Re-evaluate Shape C at v1.0 launch (3 months), gated on telemetry showing ≥50% of paying users would prefer a bundle.**

Rationale:

1. **Per-call matches what the user feels.** Step 3 of the user journey (co-founder brief §2.2) is the moment we earn or lose trust. "You paid $0.25 → here is your verdict with 3 citations" is intelligible. "You rented an agent for $2.40 today + paid $0.25 for this verdict" is two cognitive loads.
2. **Per-call protects us against rate hikes.** If Voyage doubles or Helius adds throttling, every call still settles in the green. A bundle that doesn't pass costs through is the textbook way founders blow up on rate changes.
3. **Per-call kills the SaaS-comparison frame.** The wedge (§1.6 of brief) is local-first, decentralized, no SaaS lock-in. A rental fee or monthly bundle is the surface where that claim gets attacked.
4. **No commitment lowers the trial bar.** Vibe trader with $500 capital running their first paid verdict has zero contractual exposure. Shape B and C both add commitment friction at the wrong moment.
5. **We don't have the data to price a bundle.** Cache-hit ratio, daily call distribution, and tail user behavior are all unknown. Bundling without these numbers means we either overprice (no adoption) or underprice (we eat infra). The right time to bundle is after one sprint of v0.1 telemetry — same logic the founder applied to deferring startup-verdict pricing in `project_trade_vertical_v01_decisions_2026_05_11.md` #2.

What I'm explicitly **not** recommending:

- A discount, a free-tier, a "first verdict on us" — the founder's cash sensitivity (`feedback_okx_no_funding_pressure.md`) plus the KaaS framing argue against any inventory we don't get paid for.
- Per-agent caps — explicitly rejected in founder decision #4.
- Model branding or token disclosure — CLAUDE.md violation.

## 5. Margin analysis at recommendation

Per-agent-day margin from §2.3, fixed costs allocated.

| Scale | Active agents | Marginal cost/agent-day | Revenue/agent-day | Gross margin | Monthly gross (1 agent/user/day) |
|---|---|---|---|---|---|
| v0.1 (4 wks) | 5–25 founder + dogfood | $5.00–$10.00 (fixed cost dominates) | $1.50–$3.00 | **negative** | -$200–-$1,000/mo — expected; v0.1 is investment |
| v1.0 (3 mo) | 100 paid | ~$1.29 | $2.00 (blended) | **~36%** | ~$2,100/mo gross profit |
| v1.0 (best) | 250 paid | ~$0.95 | $2.00 | **~52%** | ~$7,900/mo gross profit |
| v2.0 (6 mo) | 1,000 agent-equivs + external-agent oracle calls | ~$0.50 | ~$2.50 blended | **~80%** | ~$60k/mo gross profit |

**Break-even on per-call alone (ignoring fixed):** ~$0.025/basic call covers marginal COGS. We sell at $0.25 — 10x marginal. The fixed nut ($58 Mongo + $49 Helius = $107/mo) is covered at **~430 basic calls/mo (~$107 revenue × ~95% margin)**. We hit that at ~15 active daily agents.

**Break-even on infra + 50% margin** lands at **~$0.04/basic call effective cost**. Per-call at $0.25 clears it as long as we maintain ≥60% cache-hit ratio (otherwise the ratio of paid:cached calls compresses and we hit closer to ~$0.10 effective).

## 6. Pass-through risk

| Risk | Likelihood | Impact | Mitigation under Shape A |
|---|---|---|---|
| Helius rate-limit or credit overage mid-month | M | M — agents degrade to polling | Per-call price already absorbs infra; if Helius bills us over the $49 plan, the marginal credit cost stays inside the $0.25 sale price up to ~5x current burn |
| Voyage raises embedding rate | L | L | Embedding is $0.000003/call — could 100x and still be inside the noise of LLM cost |
| Voyage raises rerank rate (pro tier) | M | M | Pro tier's rerank line is $0.0005; could 10x and still fit inside the $0.75 pro price |
| x402 facilitator deprecates (paysh shuts down) | L | H | Wallet/facilitator neutrality is the moat (`CLAUDE.md`). Cloudflare and CDP are parallel rails; flip via env var. Pricing model doesn't change |
| OpenAI raises 4o-mini rate | M | H for pro tier | Pro tier's 4o-mini lines = ~85% of marginal LLM cost. A 2x rate hike compresses pro margin from 92% to 84% — still healthy. A 5x hike forces a sale-price revision |
| Mongo Atlas tier upgrade required at higher chunk volume | M | M | Negotiate annual; one-time step function, not a continuous rate |
| Cache-hit ratio < 30% (alert threshold) | M | H | Margin compresses fast. Mitigation is engineering — better idea_hash similarity, longer TTL — not pricing. Flagged in design spec §5 |

**Worst-realistic combined scenario:** Voyage 2x + OpenAI 2x + cache hit drops to 40% = pro-tier marginal cost goes to ~$0.18 (from $0.058). Sale price $0.75 → margin 76%. Still inside the green. Per-call pricing is robust.

The structural argument for Shape A over Shape C: every cost line above is per-call. Pricing per-call means every rate change passes through automatically. Bundles require a hedge (or a contractual repricing clause buyers hate).

## 7. v0.2 trader mode pricing — options to surface

Trader mode adds execution. Three pricing shapes for the new surface, in increasing aggressiveness:

| Option | Shape | Pros | Cons |
|---|---|---|---|
| **Pass-through only** | Same per-call oracle pricing; adapter fees passthrough at cost | Cleanest; matches §1.6 neutrality claim | Leaves money on the table — we move user PnL but capture only the oracle fee |
| **Per-execution fee** | $0.50 fixed Gecko fee per trade Gecko's verdict greenlit | Captures value at the moment of execution | Adds friction; user can route around us by reading the verdict then trading manually |
| **Profit share on verdict-gated trades** | 5–10% of PnL on trades that survived a Gecko verdict, capped at $X | Aligned incentive; the wedge metric is verdict-gated PnL anyway | Custodial-adjacent; tax/legal surface; needs attribution attestation; explicitly anti-positioning |

**My instinct:** ship v0.2 with **pass-through only**, log per-execution as a metric, revisit at v1.0. The profit-share option breaks the "oracle, not financial product" legal posture in §1.6 of the co-founder brief and should not be on the table without a regulatory review.

Don't commit on v0.2 pricing yet. Surface to founder for v0.2 sprint planning.

## 8. Competitive comparison — value-prop math

The reference frame from `project_real_competitors_2026_05_07.md`: we compete with frames.ag / Bazaar / Apify-style paid-agent marketplaces. **Not** Stripe, Visa, or vector DBs.

| Service | What it sells | Indicative price | Gecko comparison |
|---|---|---|---|
| Bazaar (pay.sh catalog, sample listings) | Single data call (CoinGecko-style, Apify actor run) | ~$0.05–$0.10 per call | Gecko is 2.5–5x — must look like 10x value |
| frames.ag agent payment | Settlement only (no oracle) | ~0.5% facilitator | Different layer; we compose above |
| Apify actor run (Apify Store, generic web scrape) | Per-run, ~$0.25–$1.00 | ~$0.25–$1.00 | Apples to apples on price; the value gap is grounding |
| ChatGPT / Perplexity Pro | Subscription, unlimited but ungrounded | $20/mo | We are paid-per-call but grounded; the comparison is hallucination rate, not price |
| Tavily search API | Search call, ungrounded | ~$0.005–$0.05 | We are 5–50x — but we synthesize, not search |

**The math we need to make explicit in marketing copy:**

A Bazaar/Apify call returns *data*. A Gecko call returns *a verdict with surviving dissent + 3+ cited canon sources*. The value claim is:

> One Gecko basic verdict at $0.25 replaces: ~5 Tavily searches ($0.05 → $0.25), one LLM synthesis pass the user would have done in ChatGPT (free but ungrounded → unmeasured hallucination risk), and ~15 minutes of human analyst time (priceless to a vibe trader watching CPI print at 09:14). Verdict-gated PnL lift target is ≥ +200 bps per the user-outcome metric in §2.3 of the co-founder brief — on $500 conviction capital, that's +$10 per trade. Spending $0.25 to capture $10 of edge is the trade.

This is what marketing copy needs to land. Per-call price is defensible if and only if verdict-gated PnL lift holds at v1.0.

## 9. Open questions for founder

1. **Confirm Shape A for v0.1 launch — or do you want a 7-day "free first verdict on us" intro?** I'm against it (KaaS framing + cash sensitivity), but it's a founder call.
2. **When does the per-call price publicly appear?** Today the `X402_MODE=stub` flag means no one pays. Do we publish the $0.25 / $0.75 sticker on `app.geckovision.tech` now (sets expectations), or hold until the live flip?
3. **What's the cache-hit ratio alarm threshold that triggers a pricing revisit?** I have 30% in the design spec as an alert; suggest **40%** as the formal "revisit pricing" gate. Agree?
4. **For trader mode v0.2, are you open to a per-execution fee on top of oracle pricing, or do we keep it pass-through and find revenue elsewhere?** This decision shapes the v1.0 → v2.0 revenue curve materially.
5. **External-agent oracle calls (v2.0, frames.ag / Bazaar callers): same $0.25 / $0.75 retail, or volume-discounted at >10k calls/mo?** Per the v2.0 success metric (≥1k external calls/week), this is the question that decides whether KaaS revenue is a step-up or a flat pass-through.

---

*business-manager · 2026-05-11 · pricing model for v0.1 launch · revisit at v1.0 with telemetry*
