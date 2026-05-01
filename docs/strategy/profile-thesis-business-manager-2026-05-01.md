# Business Manager — Profile-Thesis Validation

**Date:** 2026-05-01
**Verdict:** Refine. Thesis holds, but pricing model must shift from session-priced SaaS to **orchestration markup + creator payouts**. ICP narrows before it broadens.

## 1. Pricing in a commoditized-knowledge world

Knowledge as commodity = unit cost is contributor-set, not Gecko-set. Gecko prices the **orchestration**, not the bytes. Revised ladder:

- **Free** — single-profile, single-citation preview ($0 to user, Gecko eats the $0.10 contributor fee as CAC)
- **$0.75** — micro-verdict, 3 profiles, 1 idea (replaces old $0.10 tier; floor lifted because contributor payouts are real now)
- **$9** — Basic verdict: 5 profiles, adversarial debate, ~$3 in contributor payouts pass-through
- **$29** — Pro verdict: 12 profiles, weighted by reputation, ~$10 in payouts
- **$99/mo** — Builder seat: unlimited Basic + 5 Pro, priority routing
- **$499/mo** — Fund/Studio seat: API + bulk verdicts + private profile pool

The old $12-19 session price dies. Replace with **per-verdict + seat hybrid**. Gecko's take = 40% gross margin on orchestration, with contributor payouts itemized but not exposed as line items (per CLAUDE.md: don't show per-op costs).

## 2. Two-sided cold start — seed contributors first

Buyers won't pay for an empty index. publish.new's organic growth is too slow for our window. **Pay 50 hand-picked contributors $500 each ($25k budget) to seed 500 high-signal posts across 8 profile types** before public launch. Frame as "founding contributor" with 2x revenue share for life (40/60 instead of 20/80). This is CAC, not OpEx — book it against the first 1,000 paying verdicts.

## 3. GTM wedge — investors first, judges second

Pick **investors**. Why: (a) they already write paid memos (Substack, Paragraph), so the behavior exists; (b) their reputation is legible (portfolio, exits) and on-chain-bindable; (c) buyers pay more for an investor's "would I fund this?" than a judge's "would I score this?" — willingness-to-pay differential is 5-10x. Judges are the second wave (hackathon-tied, episodic). PMs are wave three (highest volume, lowest per-citation price).

## 4. Revenue split — 70/20/10

70% contributor, 20% Gecko, 10% protocol/publish.new fee. Founding contributors get 80/15/5. This is aggressive vs Substack (90/10) but justified because Gecko drives discovery + adversarial framing the contributor can't sell standalone. Markup-only (100% to Gecko) is wrong — contributors won't publish exclusively. Pure marketplace (95/5) is wrong — Gecko's orchestration is the value.

## 5. ICP — narrows then broadens

V1 ICP ("Claude Code power users with founder ambition") **narrows to "pre-seed founders raising in next 90 days"** for the investor-wedge launch — they pay $29-99 for a verdict citing real investors. Then broadens in V2 to: (a) agents-as-buyers (API tier), (b) corporate innovation teams ($499 seat), (c) the original power-user dev. Sellers-of-knowledge are not ICP — they're supply.

## 6. 6-month MRR projection

- **Floor:** $8k MRR (200 paying builders × $29 avg, no seats sold)
- **Mid:** $35k MRR (400 builders + 30 Builder seats + 5 Fund seats)
- **Ceiling:** $120k MRR (1,200 builders + 120 Builder seats + 25 Fund seats + API revenue)

Mid is the plan; floor is the layoff line; ceiling requires a viral verdict moment.

## 7. Biggest commercial risk

**Contributor churn before liquidity.** If the seeded 50 contributors don't see >$200/mo in payouts by month 3, they leave, the index decays, buyers refund. Mitigation: guaranteed minimum $300/mo for founding contributors for 6 months, funded from the $25k seed budget. Everything else (Perplexity competition, on-chain identity UX, regulatory) is downstream of supply integrity.

## 8. Sprint 15+ ticket — S15-BIZ-01: Founding Contributor Program spec

**Scope (3-5 days):** Draft the founding contributor agreement, payout mechanics, and recruitment list.

**Acceptance criteria:**
- `docs/gtm/founding-contributors-v1.md` with: 80/15/5 split terms, $300/mo minimum guarantee, 6-month commitment, 2x lifetime revenue share, exclusivity carve-outs
- Recruitment list: 50 named investors/judges (LinkedIn + wallet where known), prioritized by reputation legibility
- Payout flow diagram: verdict cite → x402 settlement → contributor wallet, with Gecko's 20% accounted
- Updated PRD V2 section: pricing ladder revision, contributor economics, 6-month MRR targets (floor/mid/ceiling)
- Sign-off from staff-engineer that the payout flow is buildable in Sprint 16-17
