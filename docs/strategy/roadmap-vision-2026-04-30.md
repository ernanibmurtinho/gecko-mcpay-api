# Roadmap vision — 2026-04-30

**Source:** user roadmap brief, 2026-04-30. Audience: all 7 specialist agents (staff-engineer, ai-ml-engineer, software-engineer, data-engineer, web3-engineer, business-manager, product-designer). Each will produce a Sprint-13+ planning memo from their lens.

---

## What's already committed (don't re-litigate)

- **Sprint 11:** verdict unification (KILL/REFINE/BUILD), F18 cache fix, ICP convergence, thesis sub-fold landing copy. CLOSED except cross-repo landing v2 + Solana mainnet smoke (blocked on funding).
- **Sprint 12:** CDP Bazaar listing, Base settlement parallel, wallet-neutrality, SourceProvider Protocol seam, eval transcripts archive, rubric v2 migration.
- **Sprint 13 (conditional):** if gate signals open, ship DeFi vertical suite at $9 (skip generic Pro+ mid-tier).

The deeper Bazaar thesis (`docs/strategy/bazaar-deeper-thesis-2026-04-30.md`) reframes Gecko as **the trust layer of the agentic economy**: capability is commoditized, judgment is scarce, Gecko makes judgment tradeable.

---

## The four new themes (Sprint 13+ horizon)

The user surfaced four product surfaces that didn't exist in the prior roadmap. They are individually interesting; combined, they extend Gecko from "founder-validation MCP" to "lifecycle + monetization fabric for the agentic economy."

### Theme 1 — Lifecycle monetization (pre-product → during build → ongoing)

Today's Gecko serves only **pre-product** validation: "should I build this?". The user wants to extend the same trust+judgment surface to:

- **During-development planning:** "I'm in week 3 of this build. Given what we've learned, what should I ship this week? What can wait? What just got harder?"
- **Ongoing prioritization:** "Is this still the right priority? Has the market shifted? Should I pivot/kill/double-down now?"

Each phase is a different SKU. Same engine (adversarial debate + cited evidence + verdict synthesis), different fixture+prompt set per phase. The eval gate has to grow with the surface.

Concretely the user is describing:
- `gecko_research` (existing) — pre-product validation
- `gecko_plan` (existing) — but evolved into recurring planning, not one-shot
- New surface: `gecko_pulse` (or rename) — weekly/biweekly check-in: "what changed, what should the founder do this week"

This collapses Gecko's monetization from a one-time validation fee into a build-cycle-long relationship. Pricing implications are real (the business-manager memo capped Pro+/orchestrator at single-call pricing).

### Theme 2 — Paragraph creator connector (creator-economy monetization)

Paragraph (paragraph.com / paragraph.xyz) hosts crypto/web3 creator content. The user wants:

- A pluggable connector that fetches Paragraph posts as a source for Gecko research
- When Gecko's pipeline consumes a Paragraph post in its RAG context, **the creator gets paid** (micropayment routed to their wallet via x402)
- The connector is reusable: any other Gecko-consuming project can plug into Paragraph the same way
- This is **the first creator-monetization surface** Gecko ships — a new revenue line that pays *upstream* of our verdict (we're the buyer; the creator is the seller)

Why this matters: it makes Gecko's verdicts *more grounded* (real creator content with editorial signal) AND establishes Gecko as a paying customer in the creator economy. That's a very different brand than "we scraped Reddit." It's also a quick-shippable wedge (one connector, one payment hop) that proves the upstream-monetization pattern.

### Theme 3 — Cloudflare x402 integration

Cloudflare just shipped agentic-payments support: agents pay for content gated behind their CDN. https://developers.cloudflare.com/agents/agentic-payments/x402/. There's also a Cloudflare Claude Code plugin: `/plugin marketplace add cloudflare/skills` + `/plugin install cloudflare@cloudflare`.

Two implications:
- **As a consumer:** Gecko can pay for Cloudflare-gated content (think: paywalled industry reports, premium analyst content) and incorporate it into RAG. Expensive but high-signal sources unlock.
- **As a contributor:** parts of gecko-api could deploy via Cloudflare Workers with x402 native, putting Gecko's surface at the edge with native agentic billing. (Probably V3+ — non-trivial to migrate from FastAPI.)

This extends Gecko's "wallet neutrality" claim into "**facilitator neutrality**" — frames.ag for Solana, CDP for Base, Cloudflare for HTTP-paywalled content. Three settlement paths converge at the same MCP surface.

### Theme 4 — App-launching template ("Lock for agents, unlock the agentic economy")

The biggest structural ask. The user wants Gecko to ship **a scaffolding product** where any builder can launch a paid agentic app in minutes:

> *"User: I want to develop an app for hotels with local and regional guides. Every time an agent wants to consume information from my app, they need to pay (automatically configured). How would they do that? This app can be born with a frames.ag wallet + integration instructions — 'do you want to read or use our guides? Connect your agents and pay.' Lock for agents, and unlock the new agentic economy."*

This is a **template repository** + scaffold CLI:
- `gecko launch app --kind=content-api --domain=hotels` (or similar)
- Generates: frames.ag-compatible wallet config, x402 middleware, Bazaar registration metadata, sample routes, README walkthrough
- Optionally: pre-validated by Gecko itself ("we ran the adversarial debate on your idea before you scaffolded; here's the verdict + PRD")
- Distribution: another Claude Code skill + a `gecko launch` CLI command

Positioning: Gecko isn't just *consumed* by the agentic economy — Gecko *bootstraps* the agentic economy. New buyer (the app builder), new ICP, new revenue line. Connects directly to the deeper thesis ("trust layer of the agentic economy"): if Gecko launches the trust-vetted apps, every Gecko-launched app inherits trust signal.

Concrete monetization vectors here:
- **One-time scaffold fee** ($49? $99?) — pay to generate the app
- **Validation pre-roll** — included is a free Gecko Pro debate on the idea before you scaffold
- **Hosted Bazaar listing** — we register your app's routes in Bazaar for you
- **Marketplace cut** — every successful settlement on your app routes 1-2% to Gecko (the "registrar fee")
- **Premium template library** — vertical-specific templates (hotels, guides, fintech, healthtech) at higher SKU

This is the big bet. It's also the riskiest because we'd be in the scaffolding-product business, not the validation-product business. The key tension to resolve: how does this complement the founder-validation core without diluting it?

---

## Constraints that bind across all themes

These are the rails. Specialist memos should respect them.

1. **Founder-validation core stays intact.** All four themes extend or surround the V1 product; none replaces it. The KILL/REFINE/BUILD verdict is the trust artifact every theme rides on.
2. **Wallet neutrality.** frames.ag for Solana, CDP for Base, Cloudflare for HTTP. Don't couple.
3. **The eval gate is the contract.** Any new surface needs a fixture suite + rubric grade or it ships as preview-only.
4. **Sprint 11 + 12 commitments unchanged.** New themes go to Sprint 13+ (conditional gates already defined).
5. **Composability is one-way.** Gecko consumes Bazaar/Paragraph/Cloudflare; the verdict synthesis + adversarial debate stay first-party.
6. **Cost-of-goods discipline.** Every new SKU needs an explicit COGS line + margin target. The pricing ladder (Free / $0.10 / $0.75 / $9 / $12-19 / $29) holds unless changed with evidence.
7. **No subscription.** Every monetization vector is per-call or per-event. The thesis is anti-subscription.

---

## What I want from each agent (in your lens)

Each agent produces ONE memo at `docs/strategy/sprint-13+-<role>-memo-2026-04-30.md` (replace `<role>` with your name). Under 700 words. Focus on YOUR lane; defer to others where the question crosses.

For each of the four themes, answer:

1. **Sprint sequencing.** When does this theme land? Sprint 13, 14, 15, 16+, or never (defer to a strategic option set)? Why?
2. **What's the smallest shippable wedge of this theme?** Don't propose the whole thing — propose the 1-2 week deliverable that proves the bet.
3. **Risks in your lane.** What breaks first if we ship this badly?
4. **Cross-lane dependencies.** Whose work do you need to land before yours? (Reference other lanes by role name.)

Then end with **one specific Sprint 13 ticket from your lane** that you'd commit to. Concrete, ~3-5 days, with acceptance criteria.

The staff-engineer's memo is the synthesis layer — they can refer to other agents' outputs, but they ALSO produce a per-theme sequencing decision and a recommendation on which theme leads Sprint 13.

---

## What's NOT in scope for these memos

- Re-arguing the deeper Bazaar thesis (locked in)
- Re-arguing the pricing ladder (locked in)
- Re-arguing the engineering org chart (just shipped)
- Detailed code design (that's implementation; this is strategy)
- Sprint 14+ specific tickets (Sprint 13 only; everything beyond is "deferred to" or "leads into")
