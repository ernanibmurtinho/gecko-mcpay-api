# Builder Bootstrap Platform — Product Story
**For:** Co-founder (Design)
**Date:** April 25, 2026
**From:** Ernani

---

> This document covers everything: how we got here, what we learned building Gecko, why we stepped back, how we reasoned through the pivot, and why the Builder Bootstrap Platform is the stronger bet. I've written it section by section, including the reasoning steps — not just conclusions — so you can follow the thinking and push back on anything that doesn't feel right.

---

## Part I — Where We Started: Gecko

### The Original Idea

Gecko was born from a real, documented problem. Brands running creator campaigns on crypto-native platforms have no enforcement mechanism. A brand locks a deal with a creator, the creator posts the content, and the brand delays payment — or ghosts them entirely. On the flip side, a brand pays upfront and the creator disappears. Both scenarios happen constantly. We saw it in esports, in influencer marketing, in DeFi protocol promotions.

The insight was clean: put the money in a smart contract vault. Neither party can move it until the conditions are met. The brand's budget is locked at campaign launch. The creator receives a guaranteed floor payment automatically. Performance milestones are enforced by an oracle — not by a promise.

The core promise we landed on: **"The brand cannot cancel. The creator cannot be ghosted."**

That is not a feature claim. It is a structural guarantee enforced by code on Solana, not by a terms of service that nobody reads.

### What Gecko Built

- Smart contract vaults where brands lock campaign budgets
- A cliff date: brand cannot withdraw before it expires
- A guaranteed floor yield stream to creators, regardless of campaign outcome
- Performance-based milestone payouts triggered by oracle data
- Squad campaigns: multiple creators, each with an on-chain allocation
- A frontend that displays the locked funds, the timeline, and the protocol's enforcement state

### The Validation Signal We Had

The market signal was real:

| Signal | What It Confirmed |
|---|---|
| Daisy Pay: $1M revenue in 7 months, $3.9M seed | Crypto stablecoin creator payments is a funded, working market |
| Lever.io: live with real crypto brands | The demand for crypto-native influencer deals exists right now |
| Traditional agencies doing $100M+ off-chain | Brands are manually hacking together contracts + PayPal — the problem is real, the solution is primitive |
| Trexx (largest LATAM esports hub, 16+ teams, 42K MAU) | Spontaneous pain confirmation: founder immediately mapped Gecko to his biggest operational headache |
| Failed projects (Friend.tech, DeSo, SocialFi wave) | Multiple funded teams proved the problem is real — they built the wrong solution (speculation), not the wrong market |

The Colosseum research confirmed no one has won with this approach across 5,400+ Solana hackathon projects. The space is sparse. The opportunity is real.

---

## Part II — The Honest Critique of Gecko

Despite the strong signals, Gecko has structural problems that slow it down. We need to be honest about them.

### Problem 1: It's a Two-Sided Marketplace

Gecko needs **brands** and **creators** simultaneously. Neither group shows up alone.

A brand logs in, sees no creators — leaves. A creator logs in, sees no campaigns — leaves. This is the classic cold-start problem. Every two-sided marketplace in history has had to solve it, and most fail. Uber solved it city by city. Airbnb solved it by manually onboarding hosts in New York first. We don't have that runway.

The only way to crack it is to find a single distribution partner who has both sides already assembled. Trexx was the lead candidate. But Trexx is **one company** — and their involvement is still a hypothesis, not a signed commitment.

### Problem 2: The ICP Is Hard to Reach

Crypto-native brands willing to put USDC into a smart contract vault are a small, specific audience. They are not easy to find at scale. The closest reference (Lever.io) built their client base through direct relationships, not inbound. That is a high-touch, slow GTM.

### Problem 3: The Yield Story Is Regulatory Exposure

Every version of Gecko's pitch that leads with "guaranteed yield" creates legal risk. The moment yield enters the conversation, we are describing something that looks like a financial product to a regulator who is not friendly. We solved this in messaging by leading with rug-protection, not yield — but it required constant discipline, and it limits how clearly we can explain the value to creators.

### Problem 4: The Timeline Is Long

Brands won't lock $5,000 in a smart contract on day one. The average campaign size grows over time as trust in the protocol builds. The revenue per transaction is real, but the time to meaningful volume is 12–18 months minimum with a well-funded team.

### Why We Didn't Kill Gecko

Gecko is not dead. The thesis is sound. The problem is real. The differentiation is strong. But it is a **protocol play** — it needs ecosystem timing, distribution partnerships, and potentially a regulatory environment that is still forming. The right version of this product will exist. Our assessment is that it is a 2027+ play, not a 2026 hackathon play.

---

## Part III — Stepping Back: The Problem-First Reset

In late April 2026, we stopped building for two days and asked a different question.

> **Not:** "How do we make Gecko's GTM faster?"
> **But:** "If we are building on Solana, have access to x402, and want to win a hackathon in two weeks — what is the most acutely painful problem we can solve for developers right now?"

This is the reasoning methodology we used. It has a name: **problem-first thinking**. We had been doing solution-first thinking (we have a good solution — how do we sell it?). Problem-first reverses the order: what problem is so painful that people will pay $20 right now to make it go away?

### Step 1: Map the Competitive Landscape for Context

We ran the Colosseum Copilot analysis — 5,400+ Solana hackathon projects, 293 winners, gap analysis between what winners build and what the field builds.

**What winners build (overindexed vs. the field):**

| Signal | Lift vs. Field |
|---|---|
| Oracle-based data feeds | +27% |
| Natural language processing | +24% |
| Infrastructure / tooling layer | +19% |
| Payment rails + settlement | +15% |

**What losers build:**

| Signal | Lift vs. Field (negative) |
|---|---|
| NFT-based features | -66% |
| Decentralized social / SocialFi | -64% |
| Token rewards / creator economy platforms | -100% (zero winners) |

**Implication:** Winners build infrastructure that other builders use. Losers build consumer apps that require network effects to have value.

We also found our closest ally: **MCPay** — the $25k first-place winner at Cypherpunk, now in the Frames C4 accelerator. MCPay uses the x402 protocol (HTTP 402 payment standard, backed by the Linux Foundation, 65% of transactions running on Solana) as a payment gate for MCP tools. They proved the x402 + Solana formula works at a high level. We are not competing with them — they are validation that the infrastructure layer is where you win.

### Step 2: Identify the Real Bottleneck

We asked: before a developer writes their first line of code, what is the hardest thing they face?

The answer came from a real story. A co-founder spent **hours** transcribing YouTube videos manually. The auto-captions were wrong. By the time he finished, he was overwhelmed by unstructured notes, hadn't started the business plan, and wasn't sure if the market was real. He did the research. He still didn't know what to build or whether it was worth building.

This is the wall every builder hits:

**Two types of builders. Same wall. V1 only ships for the first one.**

| Builder Type | Has | Lacks | V1? |
|---|---|---|---|
| Claude Code / Cursor power user with founder ambition (technical or technical-adjacent) | Can write the code, lives in the terminal, AI-pair-programming workflow | Domain knowledge, research, business plan, market validation | **V1 primary** |
| Non-technical founder | Domain insight, market instinct, lived pain | Data, structured output, the ability to build | Sprint 12+ via web app |

The current workflow:
1. Find relevant videos and articles
2. Manually transcribe (bad quality, slow)
3. Drown in unstructured notes
4. Try to write a business plan from scattered information
5. Give up, or ship something based on intuition that turns out to be wrong

Nobody has solved the **whole workflow**. Tools exist for pieces of it (search, transcription, document writing) — but they require assembly, technical knowledge, and still produce unstructured output.

### Step 3: Validate the Problem Severity

We asked three diagnostic questions:

**1. Would I pay for this right now?**
Yes. If we had this tool when building Gecko, we could have validated the ICP problem in hours instead of weeks.

**2. Does this save a measurable amount of time?**
Yes. 20+ hours of research, transcription, and document writing → 30 minutes of indexed, queryable knowledge base + structured output.

**3. Does the ROI justify the price?**
A $20 session that saves a developer from building the wrong thing for six months has infinite ROI. The comparison is not "$20 vs. free." It is "$20 vs. six months of wasted work."

### Step 4: Critique the Original Pricing Model

Our initial design used **per-query micropayments** via x402: $0.005 per query, $0.20 per video indexed.

We killed this model for three reasons:

1. **Friction creates avoidance.** Every time a user considers indexing a video, they must evaluate whether it's worth $0.20. They will second-guess. They will index fewer sources. The product becomes worse because of the pricing model.

2. **The value is in the output, not the indexing.** Nobody wants to "index videos." They want a business plan. They want to know if their idea is viable. The $0.20 price attaches value to the wrong thing.

3. **The session is the natural unit.** "I'm researching the hotel market in Brazil" is one session. The user pays once for that session. Everything inside the session is invisible infrastructure.

**New model:** Session-based billing.
- Basic: $10–20/session → single LLM pass → business plan + validation report + PRD
- Pro: $50–100/session → AutoGen agent team pre-loaded with domain context + live follow-up

### Step 5: Pick the Right Product Shape

We considered three directions:

**Option A — Full Platform:** Automatic discovery + indexing + structured documents + optional agent team. Solves the whole workflow.

**Option B — Claude Skill (Original):** Keep the existing design, add auto-discovery. Narrower scope, faster to build, but doesn't solve the research bottleneck.

**Option C — Two Products:** Research tool for non-technical founders + Knowledge API for developers. More addressable market, but two GTMs = two cold-start problems.

**We chose A.** The reasoning: the whole workflow is the moat. Individual pieces of it are tools. A tool that does everything from "I have an idea" to "here is your business plan, validation report, PRD, and an agent team that already knows your domain" is a different product category. That product justifies a higher price and creates a stronger reason to come back.

---

## Part IV — The Product: Builder Bootstrap Platform

### One-Sentence Version

> You describe your idea in plain language. 30 minutes later, you have a searchable knowledge base, a business plan, a validation report, a PRD, and — in the Pro tier — a team of AI agents that already know your domain and are ready to help you build.

### User Flow

```
1. Describe your idea
   "I want to build a local guide for hotels in Brazil"

2. Choose your sources
   [A] I know a source → paste URLs
   [B] Find for me → Tavily auto-discovers top sources

3. Platform proposes source list → you approve

4. Ingestion runs automatically
   source → raw text → 512-token chunks → vector embedding → Supabase pgvector

5. Basic:  single GPT-4o-mini pass → 3 documents
   Pro:    AutoGen GroupChat → 4 specialist agents → 3 documents + live agent team

6. x402 payment gate (before indexing starts)
   No payment = no session. No partial charge.

7. Documents delivered. Knowledge base stays alive.
   Ask follow-up questions anytime.
```

### The Three Documents

Every session produces:

**Business Plan** — ICP definition, market size with cited sources, competitive landscape, pricing model, revenue projections, GTM strategy (first 3 months), moat analysis.

**Validation Report** — Demand signals scored by strength (strong/moderate/weak/none), crypto necessity check ("what breaks without blockchain?"), risk map by category and severity, Go/No-Go verdict (score ≥ 8/15 = Go), 2-week sprint plan if Go.

**PRD** — Stack recommendation, MVP scope (what's in, what's explicitly out), core data model, key API routes, user stories, build-vs-integrate assessment.

### Pro Tier: The Agent Team

When a user pays for Pro, they get more than documents. They get **five specialist agents** pre-loaded with their domain context:

- **Orchestrator** — Reads the idea, assigns tasks, merges outputs, handles follow-up questions
- **Research Agent** — Queries the knowledge base across multiple angles, returns structured research context
- **Market Analyst** — ICP definition, pricing model, revenue projections, GTM, competitive differentiation, moat analysis
- **Technical Architect** — Stack recommendation, MVP scope, data model, API design, build-vs-integrate assessment
- **Validator** — Stress-tests the plan, scores demand signals, maps risks, delivers Go/No-Go

These agents stay alive for **72 hours** after session completion. A user can come back and ask: "What's the biggest technical risk in my MVP scope?" and the Validator answers from context, not from scratch.

---

## Part V — Why This Beats Gecko in the Current Window

This is not about which product is "better." It's about which product is the right bet **right now**, for a two-person team with two weeks to a hackathon.

| Dimension | Gecko | Builder Bootstrap |
|---|---|---|
| **Who buys it** | Brands AND creators (two-sided) | Claude Code / Cursor power users with founder ambition — technical or technical-adjacent (one buyer) |
| **Cold-start problem** | Yes — both sides must show up | No — single buyer, immediate utility |
| **Time to first revenue** | Weeks/months (brand acquisition) | Minutes (session purchase) |
| **Proof of value** | Requires a live campaign to demonstrate | Demonstrates value in the first session |
| **GTM** | Direct sales to crypto brands + creator network | Claude marketplace distribution |
| **Regulatory surface** | High (yield, locked funds, escrow) | Low (SaaS tool, no financial product) |
| **Requires ecosystem timing** | Yes (DeFi yield, oracle data) | No |
| **Hackathon demo-ability** | Hard (needs live brand + creator) | Easy (one `bb research` command) |
| **Unit economics** | $75/campaign average, slow ramp | $10–100/session, immediate |
| **Revenue ceiling** | Very high (protocol fees at scale) | High (SaaS + API layer) |

The honest summary: Gecko is a **protocol play** that wins at scale. Builder Bootstrap is a **tool play** that wins immediately. For a hackathon in two weeks, the tool wins.

---

## Part VI — The Moat

This is the question that matters most for a designer building the product: **what makes this hard to copy?**

A product's moat is not one thing. It's layers. Here is how the Builder Bootstrap moat stacks:

### Layer 1 — Session Data (Weak Alone, Strong Over Time)

Every session produces structured, validated research on a specific domain. The more sessions we run, the more domain knowledge we accumulate about what builders are researching, what markets are hot, what validation patterns repeat. This becomes a data asset no competitor can replicate without running the sessions themselves.

### Layer 2 — Creator Attribution Graph (Unique, Deferred to V2)

Every source we index comes from a creator — a YouTube channel, a blog, a podcast. We store the creator handle and platform from day one. As sessions scale, we build a graph of which creators' content produces the most valuable research across which domains.

When a creator claims their profile (via OAuth), they see: "Your content has been cited 47 times. $32 pending." That notification is not a cold pitch — it is a **statement of earned value**. No competitor has this graph because no competitor is doing domain-specific, session-scoped indexing at this layer.

### Layer 3 — The Agent Context Advantage

Pro tier agents are not generic AI assistants. They are pre-loaded with the specific knowledge base of a specific domain, indexed from real sources that a real builder curated. An agent team that already knows the Brazilian hotel market is different from ChatGPT in a blank context window. The user's follow-up questions are answered from that specific corpus — not from general training data.

This is the "context as moat" insight. The value is not the agents. The value is the **pre-loaded domain context** that makes the agents useful from the first question.

### Layer 4 — x402 Distribution (Structural)

x402 is an HTTP 402 payment standard backed by the Linux Foundation. 65% of x402 transactions run on Solana. The Claude Code skill marketplace gives us access to every technical developer using Claude Code worldwide.

This is not organic SEO growth. This is **infrastructure distribution** — the same channel that MCPay used to win $25k and get into the Frames C4 accelerator. The distribution is built into the platform, not acquired.

### Layer 5 — The Winner Pattern (Meta-Level)

The Colosseum research showed a consistent winner pattern: oracle data (+27%), NLP (+24%), infrastructure tooling (+19%), payment rails (+15%). Builder Bootstrap scores on three of four:

- NLP: plain-language idea → structured research
- Infrastructure tooling: the underlying knowledge base is usable by other products
- Payment rails: x402 as the session gate

We are not building to win a hackathon aesthetic. We are building to the same structural pattern that 293 winners built to, derived from empirical analysis of the full dataset.

---

## Part VII — Business Plan (Summary)

### Pricing

| Tier | Price | When |
|---|---|---|
| Basic | $10–20/session | On session start, before indexing |
| Pro | $50–100/session | On session start, before indexing |
| Pro follow-up | $0.01/query | Per query to the live agent team |
| Creator earnings (V2) | 70% of query fees | Batch-settled when ≥$15 accumulated |

### Unit Economics (Pro session at $75)

| Item | Cost |
|---|---|
| OpenAI embeddings (text-embedding-3-small) | ~$0.05 |
| GPT-4o (Pro agents, ~$3 per session) | ~$3.00 |
| Tavily search (auto-discovery) | ~$0.10 |
| Supabase storage | ~$0.01 |
| **Total cost** | **~$3.16** |
| **Revenue** | **$75** |
| **Gross margin** | **~96%** |

### Why Session Pricing Beats Per-Query

Per-query pricing ($0.005/query) creates a mental tax on every action. The user thinks: "Should I index this video? Is it worth $0.20?" They index fewer sources. The knowledge base is weaker. The documents are worse. The product fails because of the pricing model.

Session pricing removes the tax. The user pays once and indexes everything relevant. The knowledge base is richer. The documents are better. The user recommends it to their co-founder.

### GTM: Three Phases

**Phase 1 — Hackathon (2 weeks)**
Ship the Claude Code skill. Demo the hotel guide in Brazil. Enter Stablecoins track (9.9% win rate vs. 4.3% for AI). Leverage MCPay as validation/ally.

**Phase 2 — Marketplace Growth (Months 1–3)**
Claude Code skill published to marketplace. Distribution is passive — every developer using Claude Code can discover and install. Target: SuperteamBrasil developer community as first concentrated user group.

**Phase 3 — Web App + Creator Attribution (Months 3–6)**
Next.js web app for non-technical founders. Creator OAuth claim flow. Pro tier agent subscriptions. Creator earnings notification system.

---

## Part VIII — Design Implications

This section is specifically for you.

### What the Interface Must Do

The product has two distinct user types with different mental models:

**The Claude Code / Cursor power user with founder ambition** (V1 primary — technical or technical-adjacent)
- CLI-first. Already lives in Claude Code; installs via `Read app.geckovision.tech/skill.md`.
- Wants: session ID they can reference, source list confirmation, confidence/verdict, on-chain receipt
- Does not want: decorative UI, marketing copy, model branding, explanations of what "embedding" means
- Key moments: payment confirmation, indexing progress, verdict (KILL/REFINE/BUILD), document output in terminal

**The Non-Technical Founder** (Sprint 12+ / V2 audience — unlocks with `app.geckovision.tech` web app)
- GUI-first. Never opened a terminal. Out of scope for V1 because the CLI is the wrong surface.
- Wants: to see progress, feel that something is happening, receive something polished
- Does not want: technical jargon, uncertainty about what the tool is doing with their idea
- Key moments: session creation ("your idea is being researched"), document reveal, agent team introduction

### Emotional Journey Map

```
Describe idea → [Relief: someone is listening to me]
                     ↓
Choose sources → [Agency: I'm directing this]
                     ↓
Indexing runs  → [Trust: something real is happening]
                     ↓
Documents arrive → [Surprise: this is actually good]
                     ↓
Agent team ready → [Excitement: I have a team now]
```

The emotional peak is **document reveal**. This is where the product proves its value. Design the reveal as a moment — not a file download, not a text dump. A structured reveal that feels like opening something.

### What to Never Show

- Cost per operation (kills the no-friction pricing advantage)
- "Powered by GPT-4o" or any model branding (erodes the product's identity)
- Raw JSON, embeddings, vector similarity scores (plumbing, not product)
- "Indexing chunk 47 of 312" (progress indicator is fine, granular internals are not)

### What to Always Show

- Session ID (small, persistent — gives the user a sense of ownership)
- Confidence level (high/medium/low — honest about what the product knows)
- Sources cited (builds trust — "this came from real content")
- Time saved (implicit or explicit — frame the ROI)

---

## Appendix: Reasoning Methodology

This section documents exactly **how** we think through product decisions. It is not a one-time process — it is the method we use every time we are uncertain.

### The Five Questions

Every product decision passes through five questions, in order:

**1. What problem are we actually solving?**
Not the feature. The pain. "Indexing YouTube videos" is a feature. "A developer spends 20+ hours researching before writing a line of code and still doesn't know if the market is real" is a pain. The pain must be visceral before any architecture discussion starts.

**2. Who has this problem badly enough to pay right now?**
Not eventually. Right now. If the answer requires "once the market matures" or "when we have enough users," the timing is wrong. Builder Bootstrap's V1 answer: Claude Code / Cursor power users with founder ambition — technical or technical-adjacent. They have the pain today, they live where our distribution lands, and they will pay per session without a sales call. Fully non-technical founders share the pain but are a Sprint 12+ unlock once the web app ships.

**3. What is the smallest thing that proves value?**
Not the full product. The demo. For Builder Bootstrap, it is one command: `bb research --idea "hotel guide in Brazil"`. A terminal output that contains a real business plan, a real validation report, and a real PRD. That is the proof. Everything else is polish.

**4. What makes it hard to copy?**
If the answer is "nothing," reconsider. If the answer is a single layer (e.g., "we move fast"), it is fragile. The goal is multiple compounding layers, where each layer makes the previous one stronger. Builder Bootstrap's moat compounds: sessions produce a data asset, the data asset makes creator attribution possible, creator attribution makes the platform more valuable for research, more research produces better sessions.

**5. Does the pricing model reinforce or undermine the value proposition?**
Pricing is not just revenue mechanics. It is a message about what the product is worth. Per-query pricing says: "every action costs you something — proceed carefully." Session pricing says: "you paid for the session — use everything you need." The second message is the right one.

### How We Used This in Practice

We applied these five questions to the x402 micropayment model and killed it in one session. The problem it solved (predictable per-operation revenue) was ours, not the user's. The problem it created (friction on every action) was the user's.

We applied them to the two-sided marketplace design of Gecko and identified the cold-start problem as the primary blocker — not the technology, not the market size, not the positioning.

We applied them to the three product shapes (full platform / Claude skill / two products) and chose the full platform because it was the only one that answered question 3 with a single, demonstrable command.

This methodology is not a formula. It is a discipline. The goal is to spend zero time building things that don't survive question 1.

---

*Document prepared by Ernani Britto · April 25, 2026 · gecko-mcpay-app project*
*Updated 2026-04-30 (S11-PRD-01) — V1 ICP converged to "Claude Code / Cursor power users with founder ambition — technical or technical-adjacent"; non-technical founder moved to Sprint 12+ web-app expansion.*
