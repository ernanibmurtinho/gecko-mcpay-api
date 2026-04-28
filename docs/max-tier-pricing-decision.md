# Max Tier Pricing — Final Decision

**Date:** 2026-04-28
**Owner:** business-manager
**Status:** Recommendation locked pending user override
**Launches with:** Sprint 3 V2 source rollout (~3 weeks)

---

## TL;DR

- **Confirm $2 / $5 / $15** for Max-S / Max-M / Max-L. Do not adjust on launch. Margins (85% / 83% / 84%) clear the 70/80/80 floors with room.
- **Rebrand:** ship as **Pro** ($0.75) → **Pro+** ($2) → **Studio** ($5) → **Studio Max** ($15). "Max-S/M/L" reads as Anthropic-derivative and gives away architecture instead of value. See §4.
- **Landing surface:** show Basic + Pro on apex. Pro+ as "new" pill. Studio / Studio Max behind a `/pricing` link, not on hero. Don't put the $15 SKU on the public landing — that's an outbound conversation.

---

## 1. ICP stress tests

### Max-S ($2) — indie dev who's outgrown Pro

- **Mental anchor:** a single Cursor query, a coffee, a Substack subscription day-pass. $2 is below the "should I think about it?" threshold. Confirmed in the yeah-why-not zone.
- **Risk:** $2 is *too* close to Pro ($0.75) — the upgrade reason has to be visceral. The 1 included rerun + on-chain calibration is the right wedge; we should call it "second opinion + receipt" in copy, not "more turns."
- **Verdict on price:** keep at $2. The risk is not the number; it's the upgrade narrative. Marketing copy must make Pro+ feel like "I'm not done yet" rather than "I want more turns."

### Max-M ($5) — hackathon team paying with team funds

- **Mental anchor:** team lunch tab, two ChatGPT Plus seat-days. $5 reads as "we are serious enough to spend team money but not so much that anyone has to approve it."
- **Risk:** $5 might be in the "have to ask the team lead" zone for some hackathons. But the Studio buyer has already self-selected — they're past the price-conscious gate.
- **Verdict on price:** keep at $5.

### Max-L ($15) — founder vs. $500 consultant

- **Mental anchor:** a Lovable / Bolt month, a Cursor Pro month, two pizzas. Yes, $15 against a $500 consultant is a steal. **That is the message.** "Replace your $500 pre-build advisor consultation with $15 of agent debate, citations, and an on-chain receipt."
- **Risk:** "too cheap to be premium" is real. v0/Cursor at $20/mo set the benchmark for "serious tool." $15 sits just below — readable as *per-session* premium against monthly competitors.
- **Verdict on price:** keep at $15. **Do not raise** to $20+ at launch. We don't yet have the social proof to defend a number above the Cursor/v0 monthly subscription anchor. Once we have 5 named founder testimonials, revisit at $25.

---

## 2. Anchor comparison

| Product | Price | Unit | Mental shelf |
|---|---|---|---|
| Claude Max | $100 / $200 | month | Power user subscription |
| ChatGPT Pro | $200 | month | Power user subscription |
| Cursor Pro | $20 | month | Dev tool subscription |
| v0 | $20 | month | Dev tool subscription |
| Lovable | $25-50 | month | App builder subscription |
| Bolt | $20-50 | month | App builder subscription |
| Replit Agent | usage | per-run | Agent execution |
| **Gecko Pro** | **$0.75** | **per session** | **Per-decision tool** |
| **Gecko Pro+** | **$2** | **per session** | **Per-decision tool, deeper** |
| **Gecko Studio** | **$5** | **per session** | **Pre-build advisor** |
| **Gecko Studio Max** | **$15** | **per session** | **Replaces $500 consultant** |

**Lesson 1 (from Anthropic / OpenAI):** subscription tiers stack by *capability multiplier* not by feature count. Pro+ should not be "Pro with more turns" in copy — it should be "Pro with a second opinion and a receipt." Studio is "Pro debate by 8 specialists." Studio Max is "as if you hired the consultant."

**Lesson 2 (from v0 / Cursor):** $20/mo sets the social-proof floor for "serious dev tool." Gecko's per-session pricing means we don't compete on that axis directly — but the *highest* per-session SKU should clear ~75% of the monthly competitor anchor. $15 ≈ 75% of $20. Correct.

**Lesson 3 (from Lovable / Bolt):** generous free tier matters more than tier ceiling. We have $0 stub mode covering this.

**Lesson 4 (from Replit Agent):** per-run pricing fatigues users when invisible cost is variable. Our session pricing is fixed per tier — psychologically cleaner. Don't break this with usage add-ons.

---

## 3. Final number recommendation

**Confirm: $0.75 / $2 / $5 / $15.**

| Tier | Price | COGS | Margin | Floor | OK? |
|---|---|---|---|---|---|
| Pro | $0.75 | $0.156 | 79% (cold) / 88% (warm) | 70% | yes |
| Pro+ | $2.00 | $0.30 | 85% | 70% | yes |
| Studio | $5.00 | $0.85 | 83% | 80% | yes |
| Studio Max | $15.00 | $2.40 | 84% | 80% | yes |

No adjustment needed. The plan's COGS sketch holds. Margins clear floors with comfortable headroom — meaning we can absorb a 30%+ COGS overrun (extra LLM tokens, frames.ag enrichment overage) without breaking the margin floor.

---

## 4. Naming — "Max-S/M/L" → "Pro+ / Studio / Studio Max"

**Why drop Max-S/M/L:**

1. **Derivative.** Anthropic owns "Max" in this builder demographic. Using it reads as either copying or as a sub-brand of theirs.
2. **Architecture leak.** S/M/L exposes that we have three sub-tiers of the same thing — invites the buyer to ask "what am I actually getting more of?" and forces us into feature-table arms races.
3. **No upgrade narrative.** S → M → L is a number game. Pro → Pro+ → Studio → Studio Max is a story (deeper analysis → workshop → consultant simulation).

**Recommended:**

| Was | Becomes | One-liner |
|---|---|---|
| Pro | **Pro** (unchanged) | The 5-agent debate. $0.75. |
| Max-S | **Pro+** | Pro, with a second opinion and an on-chain receipt. $2. |
| Max-M | **Studio** | Eight specialists, full judge rubric, three reruns. $5. |
| Max-L | **Studio Max** | The pre-build advisor. Replaces a $500 consultation. $15. |

**Alternates considered + rejected:**
- "Lite / Standard / Enterprise" — Enterprise reads B2B SaaS, wrong audience.
- "Pro / Pro+ / Pro Max / Ultra" — too many "Pro"s; Ultra is Apple-coded.
- "Founder / Team / Studio" — implies seat-based pricing we don't offer.

User can override. Don't bikeshed past 30 minutes — the rename can ship in a follow-up if disputed.

---

## 5. Marketing copy hierarchy

### Apex landing (`geckovision.tech`)

**Above the fold — show:**
- Free (stub mode) — "$0 · try the flow"
- Basic — "$0.10 · single pass, three docs"
- Pro — "$0.75 · 5-agent debate, on-chain settlement"

**Below the fold, "Need more depth?" row — show:**
- Pro+ — "$2 · second opinion, calibrated on Solana mainnet"

**Behind `/pricing` link — show:**
- Studio — "$5 · 8 judges, three reruns, full rubric"
- Studio Max — "$15 · pre-build advisor session, contact for volume"

**Do not show on apex landing:**
- Studio Max as a primary tier card. It's an outbound / direct-CLI surface. Public landing presence dilutes the "founder pays $15" psychology — they need to feel like they discovered it, not that they're being upsold.

### CLI surface

All four tiers exposed at the flag layer: `--tier basic|pro|pro-plus|studio|studio-max`. Studio + Studio Max gated behind `--i-know-what-this-costs` confirmation prompt for the first usage to avoid accidental $15 charges.

### Skill (`app.geckovision.tech/skill.md`)

Mention Pro+ inline. Studio / Studio Max listed in a "for deeper work" sub-section, one line each, no card UI. Skill readers are pre-qualified — they don't need the full sales surface.

### Discovery / launch

Studio + Studio Max launch via:
- Tweet thread from `@ernani` with three example session outputs at each tier
- One blog post on `geckovision.tech/blog` titled "We added a pre-build advisor"
- Skill changelog bump

Do not launch on Product Hunt at the Studio Max price point. PH audience price-anchors at free / freemium and will mispitch the value.

---

## 6. Decision summary

| Decision | Locked answer |
|---|---|
| Max-S price | **$2** confirmed |
| Max-M price | **$5** confirmed |
| Max-L price | **$15** confirmed |
| Naming | **Pro+ / Studio / Studio Max** (rename from Max-S/M/L) |
| Apex landing surface | Basic + Pro hero; Pro+ secondary; Studio/Studio Max behind `/pricing` |
| Public Studio Max card on landing | **Hidden** |
| CLI surface | All four exposed; confirmation gate above $5 |
| Launch window | Sprint 3 alongside V2 sources, not waiting for Sprint 4 |
| Future revisit | At 5+ named founder testimonials, evaluate Studio Max → $25 |

**Open decision deferred to staff-engineer:** confirmation-prompt UX for first-usage of $5+ tiers. That's a CLI flow question, not a pricing question.
