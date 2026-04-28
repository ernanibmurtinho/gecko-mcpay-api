# Landing Copy v2 — `geckovision.tech` apex

**Date:** 2026-04-28
**For repo:** `gecko-mcpay-landing`
**Status:** Copy spec — handoff to product-designer + frontend-engineer for layout
**Replaces:** Pre-prompts-v4 copy now live on apex

This document is **literal copy**, not design direction. Each section's text below is what should ship in the rendered DOM. Headings, microcopy, button labels, and stamps all included.

---

## 1. Hero headline

**Three alternatives considered:**

A. "Validate an idea for ten cents." *(current — pre-V1-sources)*
B. "Plan your next app for ten cents."
C. "Name your competitors before you write a line of code."
D. "A 5-agent debate, an on-chain receipt, and the three docs you actually need. For ten cents."

**Recommendation: B.**

> # Plan your next app for ten cents.

**Why B over A:** "Validate" is a yes/no question. Post-V1 sources, Gecko does more than validate — it produces a PRD, a story, a GTM, names comparables, and runs a debate. "Plan" captures that surface. "Validate" undersells.

**Why B over C:** C is sharper but narrower; it sells the competitor research and hides the docs. B leaves room for the sub to do that work.

**Why B over D:** D is feature-stuffed. Hero copy earns the rest of the page; it doesn't summarize it.

---

## 2. Hero sub

> # Plan your next app for ten cents.
>
> ### Pre-code planning that names your competitors, drafts your PRD, and hands you an on-chain receipt — all before you open your editor.

Alt one-liners (pick on layout test):

- "Five agents debate your idea, six sources back them, three documents drop. In ninety seconds."
- "Pre-code research and planning, paid per session, settled on Solana."

---

## 3. CTA buttons

Primary:
> ## Try it free → `Read app.geckovision.tech/skill.md`

Secondary:
> ## See a sample session

Tertiary (small text, below):
> Already in Claude Code? `bb research --idea "..."`

---

## 4. "What you actually pay" — cost receipt block

Replace the existing receipt with a two-card layout. Both render as terminal-styled blocks.

### Card A — Basic ($0.10)

```
GECKO BASIC ──────────────── $0.10 USDC
─────────────────────────────────────────
LLM passes ............... $0.0080
Embeddings ............... $0.0019
Tavily search ............ $0.0023
─────────────────────────────────────────
Cost of goods ............ $0.0122
Margin ................... 88%
─────────────────────────────────────────
PAID ON-CHAIN — Solana mainnet · x402
```

### Card B — Pro ($0.75)

```
GECKO PRO ──────────────── $0.75 USDC
─────────────────────────────────────────
LLM debate (5 agents) .... $0.0950
Embeddings + flywheel .... $0.0010
Tavily + HN + Reddit ..... $0.0023
twit.sh judge threads .... $0.0500
Judge distillation ....... $0.0050
─────────────────────────────────────────
Cost of goods ............ $0.1560
Margin ................... 79% cold · 88% warm
─────────────────────────────────────────
PAID ON-CHAIN — Solana mainnet · x402
```

**Caption under both cards:**

> No subscription. No seat fees. No "contact sales." One session, one price, one signed transaction.

---

## 5. Sub-agents grid (5 cards)

Replace existing one-line bios with these. One sentence each, present-tense, what they actually argue about.

| Agent | Copy |
|---|---|
| **Strategist** | Builds the case for shipping. Names the wedge, the moat, and the first 100 users. |
| **Skeptic** | Builds the case for killing it. Surfaces the named competitor that already won, and the assumption that breaks. |
| **Market Analyst** | Sizes the wedge against real signal — HN threads, Reddit pain posts, GitHub topic velocity. |
| **Technical Lead** | Argues feasibility, not vibes. Counts the integrations, the infra cost, and the four-week MVP shape. |
| **Judge** | Calibrates against what real Solana judges and operators have said about ideas like yours, this week. |

**Section heading:**

> ## Five agents. One verdict. No mush.

**Sub:**

> Each runs on the same six sources. None hedge. The debate is in the transcript — we ship it with your verdict.

---

## 6. NEW section — "Sources we read so you don't have to"

**Heading:**

> ## Six sources. None of them you'd Google.

**Body cards (one line each):**

| Source | Copy |
|---|---|
| **twit.sh** | What real Solana judges said about ideas like yours, last seven days. |
| **Internal flywheel** | Every prior Gecko verdict on a similar idea — what shipped, what got killed, what pivoted. |
| **Colosseum Copilot** | Every hackathon submission in your category. Cluster, winner gap, who already filed. |
| **HackerNews** | Top threads where this problem was discussed; the comment that called it. |
| **Reddit** | The pain posts that don't make it to HN. Subreddit-aware. |
| **GitHub** | Repo count, star velocity, and the five repos that are already two years ahead of you. |

**Caption:**

> Free sources are always on. Paid sources (twit.sh) only fire when the idea category warrants it. You never see a per-call invoice.

---

## 7. NEW section — "We killed our own pitch"

**Heading:**

> ## We pointed Gecko at Gecko.

**Body:**

> Last week we ran the latest prompt build against our own pitch. The verdict came back: **kill — no named ICP, three competitors already shipping, founder is the user.**
>
> So we fixed the prompts, named the ICP, and shipped V1 sources. Verdict accuracy across our 20-idea eval suite: **0.45 → 0.85 across four iterations.**
>
> You're reading the page that survived the second debate.

**Pull-quote block (terminal-styled, monospace):**

```
$ bb research --idea "Builder Bootstrap Platform for indie devs"

VERDICT ───────────────────────────────── KILL
─────────────────────────────────────────────
- No named ICP. "Indie devs" is not an ICP.
- Three named competitors shipping today.
- Founder is the user — survivorship bias.
─────────────────────────────────────────────
RECOMMENDATION: pivot to a single-vertical
ICP and re-run.
```

**Caption:**

> The transcript is real. The verdict was right. The fix shipped.

---

## 8. Tier pricing — V1 launch surface

**Recommendation: hide Max-tier (Pro+ / Studio / Studio Max) entirely on V1 apex landing.** Re-launch landing when those ship in Sprint 3. Justification:

1. Pro is the only paying SKU at GA. Showing Max as "coming soon" creates "wait and see" friction on the only conversion that matters Monday.
2. Pre-launch placeholders signal "not done." We are done — for V1.
3. The Sprint 3 Max launch deserves its own landing moment with sample outputs and the founder-vs-consultant story. Pre-spoiling it on apex weakens that beat.

**Apex tier section (V1 launch — what ships):**

| Tier | Price | What | CTA |
|---|---|---|---|
| **Free** | $0 | Stub mode. Try the flow without spend. | `bb research --tier=stub` |
| **Basic** | $0.10 | Single LLM pass, three documents. | `bb research --tier=basic` |
| **Pro** | $0.75 | 5-agent debate, six sources, on-chain settlement. | `bb research --tier=pro` |

**Section heading:**

> ## Three tiers. Per session. Settled on Solana.

**Sub:**

> No subscriptions. No seats. Pay when you have a question.

---

## 9. Roadmap row

Replace existing roadmap copy with:

> ## What's shipping next

**Now (Sprint 2 final, this week):**

- Six V1 sources fully integrated — twit.sh judges, internal flywheel, Colosseum, HN, Reddit, GitHub
- Idea classifier picks the right sources per category
- Eval harness expanded to crypto + SaaS sub-suites

**Next (Sprint 3, ~3 weeks):**

- Four V2 sources: Helius DAS, DefiLlama, GitHub trending, Wellfound
- Three new tiers: Pro+, Studio, Studio Max — for deeper work
- Web dashboard at `app.geckovision.tech` — read your prior sessions, run reruns, share results
- Sample-app regen — the PRD becomes scaffolded code, not just docs

**Later (Sprint 4):**

- Creator attribution — when your verdict cites a thread, the author gets a slice
- Public eval leaderboard — every prompt change scored against the same 50-idea suite

---

## 10. Footer credibility line

Replace existing footer tagline with:

> Built by builders who shipped on frames.ag wallets. Settled on Solana. Distributed via Claude Code skills. No subscriptions, no seats, no models named on the box.

---

## 11. Removed / archived from prior copy

- Any "powered by GPT-4o" / "OpenAI under the hood" mentions — never on the box. Implementation detail.
- The "$0.10" hero anchor — moved to a sub-line; hero is now the action ("plan your next app"), not the price.
- Token count / model name references in the cost receipt — replaced with capability lines.
- Per-call cost language — replaced with session-level totals.

---

## Handoff notes for product-designer + frontend-engineer

- Layout, type ramp, and visual hierarchy are not specified here. Copy is the contract.
- The "We killed our own pitch" section is the highest-trust beat on the page. Treat its layout as the most-considered block on apex.
- Cost receipt cards are intentionally monospace / terminal-styled. That's a brand position, not a placeholder.
- Tier pricing table at V1 must not include Max tiers in any form (no "coming soon," no greyed-out cards). Sprint 3 lands them in their own moment.
