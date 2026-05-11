# Co-founder roadmap brief — 2026-05-11

**From:** Ernani (technical co-founder)
**For:** product-designer co-founder
**Subject:** Where we are, where we're going, and why this matters

---

## TL;DR (executive summary)

We just unlocked the wedge end-to-end. The retrieval bug that kept our investor-canon corpus (Howard Marks + Buffett + Damodaran — **4,874 chunks live in Mongo Atlas**) invisible to `gecko_trade_research` got fixed today; the production endpoint is being redeployed. The trade-vertical architecture is locked: **coach → oracle → agent → execution rail**, all four layers Gecko-shaped, no SaaS lock-in, no venue lock-in.

We have **12-18 months** to lock the "decentralized strategy-oracle" category before Coinbase and Binance ship their own. The play for the next 4 weeks (v0.1) is small, focused: ship a deployable advisor agent that runs on the user's laptop, grounded by the canon corpus, dispatching through OKX OnchainOS for execution. The OKX Skill Quality Award track is opt-in evidence, not a deadline.

**What this brief covers:**
- **Section 1 — Business lens:** market vision, competitor map, three-horizon roadmap, why local-hosted + decentralized is a moat not a preference, success metrics by horizon
- **Section 2 — Product lens:** ICP (vibe trader, primary; Pioneer/data-contributor, secondary), end-to-end user journey, layer handoffs, what users must NOT understand
- **Section 3 — Design lens:** surface inventory, output positioning (Gecko = insight; Claude = renderer), three voices, animation/interaction budget, what the design co-founder owns next, anti-patterns

**Three takeaways for the design co-founder:**

1. **We are upstream of every renderer.** Gecko ships JSON + good copy. Claude renders. OKX wallet renders. We do not own pixels except on `app.geckovision.tech`. This is the most important constraint.
2. **The wedge is dissent-checked judgment, not "the AI says."** Every craft choice protects that frame — cite by author name, surface surviving dissent count, never collapse the panel into a single voice.
3. **One ICP for the next 90 days.** Vibe trader / builder-developer. The Pioneer journey is real but blocked on partner-registry shipping. Do not blend the two.

---

## Section 1 — Market vision, roadmap, and why this matters
*— Business lens (business-manager)*

### 1.1 State of the union (May 11, 2026)

We are no longer a hackathon project pitching an idea. As of this week, the spine is shipped: a 4,874-chunk investor-canon corpus (Howard Marks memos, Buffett shareholder letters, Damodaran NYU Stern PDFs, Mauboussin papers, Boyle/Felix transcripts) lives in Mongo Atlas with Voyage 1024-d embeddings. The retrieval wedge that gates everything downstream — `gecko_trade_research` returning grounded verdicts with surviving dissent and cited chunk IDs — was structurally broken until 2026-05-11 (session_id was scoping `$vectorSearch.filter` to per-request fragments) and is now fixed per `docs/strategy/2026-05-11-retrieval-wedge-sprint.md`. The Kamino-USDC test query now returns 3-4 distinct provider_kinds. The wedge is real, not aspirational.

Alongside the corpus, we have working primary surfaces. `gecko-trade-coach` is scaffolded in `gecko-claude` mirroring OKX's `starter-coach` shape, so it speaks the Skill Quality rubric natively. OKX Agentic Wallet (`onchainos`) is wired as the first execution adapter; SendAI and Backpack land later as stub-first adapters per Pattern C in `CLAUDE.md`. paysh and Bazaar x402 settlement works on Solana mainnet — `paysh_live` and `bazaar_live` caller code shipped in v0.2.20+ — though the production task currently only has an inbound wallet, so live BUYER spend is blocked on a funded SecureString (see `project_buyer_wallet_blocker_2026_05_08.md`). `X402_MODE=stub` stays on until we decide to charge.

Honest about what is missing: the backtest harness for trade strategies is not built (ticket open as part of the 12-ticket trade-vertical expansion). The OKX Skill Quality Award submission is optional, not committed — founder cash-flow constraint per `feedback_okx_no_funding_pressure.md`. Gecko is not yet listed on pay.sh; that is in conversation, not shipped. The Attested Alpha Registry integration with our partner is "additive, not a prerequisite" per `project_three_layer_architecture_2026_05_11.md`. None of these block v0.1; each is sequenced in the roadmap below.

### 1.2 The market we're playing in

The competitor map is the one we corrected on 2026-05-07 and need to keep correcting against model drift. Per `project_real_competitors_2026_05_07.md`:

| Layer | Competitors | Gecko's posture |
|---|---|---|
| Paid-agent marketplaces | frames.ag, Bazaar (pay.sh catalog), Apify | Direct competitors AND ecosystem peers. They sell directories; we sell the verdict about which entries in those directories to call. |
| Wallet/skill bundles | OKX OnchainOS (19 skills), SendAI Solana Agent Kit, Backpack | Execution surfaces we compose above. Never compete; always delegate. |
| Settlement rails | Stripe ACP, Visa, Cloudflare Pay-per-Crawl | Adjacent infrastructure, not competitors. We use x402, we don't compete with x402. |
| Vector infrastructure | Pinecone, Vectara, LlamaIndex | We use Mongo Atlas + Voyage. We don't sell vector infra. |
| Generic knowledge | ChatGPT, Perplexity | Never lead the comparison with these. The defensibility test (Pattern D) is whether we beat Perplexity on a *named axis*, not on generic Q&A. |

Where Gecko sits: **categorized, compounding Knowledge-as-a-Service oracle** that other paid skills call (per `project_kaas_positioning_2026_05_08.md` and `project_knowledge_as_commodity_pivot.md`). Not a marketplace operator. Not a curator of human-authored skills. Not a custodian, signer, or order router. We are the thing that says "of the 72 paid services in Bazaar, here is which one to call for *this* idea, with this verdict, this surviving dissent, and these citations."

Why the marketplaces can't take this position themselves: their wedge is curation + payment + discovery. The moment they ship adversarial-debate verdicts grounded in an investor-canon corpus, they become an oracle, which means they have to pick a side on every listing in their own marketplace. That's a structural conflict. They will route to us before they build it themselves — the same way Amazon doesn't run a competitor-ranking service.

The category, not the feature: **decentralized + local-first agents**. Local-hosted means the user runs the loop on their own machine (`gecko-trade-agent` runtime lives in `packages/gecko-core/src/gecko_core/trade_agent/`, not on our servers). Decentralized means the agent dispatches to whatever execution rail the user owns — OKX today, SendAI/Backpack/frames.ag tomorrow. This is not a tech-stack choice; it's a moat shape. SaaS-hosted competitors that pick one venue are one regulatory action or one platform-fee change away from existential risk. We are above all of that, the way Chainlink is above any single chain.

### 1.3 Why now — the 12 to 18 month window

Four things just happened simultaneously that won't stay this way:

1. **OKX Agentic Wallet shipped** (April 2026) with a published Skill Quality rubric whose five criteria — strategy completeness, risk control, execution reliability, user safety onboarding, observability — literally describe Gecko's `TradePanelVerdict` envelope (per `project_okx_diagnostic_2026_05_11.md`). The rubric is a free distribution beacon for grounded-verdict oracles. Coinbase and Binance will ship their own agent wallets within 12-18 months. When they do, the rubric stops being neutral.
2. **paysh + Bazaar are infant marketplaces.** 72 paid services on Bazaar today. Apify-style scale (10k+ actors) is two years out. Being the canonical verdict oracle on a 72-entry catalog is achievable; being the oracle on a 10k-entry catalog after someone else established the position is not.
3. **x402 on Solana works but is undersupplied with insight providers.** 65% of x402 transactions run on Solana. The infrastructure is settled; the supply side of grounded knowledge services is empty.
4. **Autonomous trading skills are demand-overloaded and supply-thin on grounded judgment.** Every "AI trading agent" launched in 2026 ships a strategy emitter with no grounded verdict, no surviving dissent, no citations (we have audited starter-coach and confirmed this). The gap is structural, not temporary, because grounded verdict synthesis requires a corpus none of the strategy-emitter teams are building.

The window closes when (a) Coinbase/Binance ship competing oracle layers, (b) the marketplaces hit 5k+ entries and curation alone becomes the wedge, or (c) one of the existing strategy-emitter teams realizes verdicts beat strategies and pivots. Each of those is 12-18 months out. We have one cycle to lock the position.

### 1.4 Roadmap — three horizons

| Horizon | Window | What ships | What users do | What we charge |
|---|---|---|---|---|
| **v0.1** | Next 4 weeks | Coach + oracle + advisor-mode agent on user's laptop. `gecko-wallet` skill grounds wallet ops via oracle. Investor-canon corpus locked. OKX execution adapter live; SendAI/Backpack stubbed. Optional OKX Skill Quality submission. | Run `bb trade-agent up --spec ...` on their machine. Agent surfaces opportunities (no auto-exec). Coach-emitted strategy spec carries citation IDs per rule. | Per-call x402: $0.25 basic verdict, $0.75 pro panel. Cache-first; charge on miss. |
| **v1.0** | 3 months | Trader mode (gated execution behind paper-trade graduation). Strategy hot-swap (ECS-style versioned cutover). Backtest harness shipped. Multi-venue (SendAI + Backpack contract tests pass, flip to live). pay.sh listing live. Web app at `app.geckovision.tech` for non-technical buyers. | Deploy a strategy that paper-trades for N days, graduates to live execution on a venue they pick. Web buyers run sessions through GUI. | Same per-call x402 unit. Adds session pricing on web surface ($10-20 Basic / $50-100 Pro) per existing PRD. |
| **v2.0** | 6+ months | Bidirectional partner registry (consume Attested Alpha + publish Gecko-authored strategies). Public per-category retrieval endpoint (`market_intelligence`, `investment_signals`, `dex`, etc.) callable as a paid API. Creator-attribution settlement live (V2 PRD line). | External agents from frames.ag / Bazaar / Apify call our endpoints. Strategy authors publish through us into partner registry. Builders consume the categorized base directly. | Per-call x402 stays the unit. Volume tiers for >10k calls/month negotiated. Creator earnings settle on-chain (70/30 split per existing PRD V2 spec). |

Each horizon has a hard graduation gate. v0.1 graduates to v1.0 only after the advisor-mode laptop reliability story validates (cold-restart resumes, no journal corruption, websocket reconnect stable). v1.0 graduates to v2.0 only when retrieval call volume justifies the public endpoint surface (Pattern D defensibility test must still hold at scale — hallucinated-trade rate vs. Tavily-direct on a named axis).

### 1.5 Why skills connect to agents — the strategic reason

Skills are distribution; agents are execution. Without both, we're either a corpus vendor or a SaaS tool. With both:

- The **skill** is how we land in a builder's environment (Claude Code, Cursor, Codex, OpenClaw via OKX bundle). The user types `gecko trade` or `should I bridge USDC` and the skill is there. Distribution is passive — same channel MCPay used to win Cypherpunk.
- The **agent** is what runs when the user walks away. It surfaces opportunities (v0.1 advisor), executes within risk caps (v1.0 trader), and keeps calling the oracle on a cadence the user approved.
- The **oracle** is the wedge. Every skill call and every agent decision routes through `gecko_trade_research`, which is the only path to the corpus.
- The **coach** is the onboarding surface. It composes skill + oracle into a strategy spec that is publish-portable to OKX, partner registry, or the user's own deployment.

Pull out any one layer and the model collapses into something defensible by a competitor. Coach-only = starter-coach with extra steps. Oracle-only = a corpus vendor (Pinecone with prose). Agent-only = SendAI Solana Agent Kit. Skill-only = a marketing surface. The four-layer composition (per `project_three_layer_architecture_2026_05_11.md`, expanded) is the full vertical capture.

### 1.6 Why local-hosted + decentralized is a moat, not a preference

This is the part that gets miscategorized as engineering taste. It is not. It is the structural reason we cannot be displaced by Coinbase or Binance shipping their own oracle.

**Local-hosted.** The `gecko-trade-agent` runtime runs on the user's laptop. Their keys, their cache, their journal, their RPC quota. We never custody, never sign, never route orders. Strategic consequences:

- No SaaS lock-in. If Gecko goes down, the agent keeps running on the last-cached verdict + circuit breakers. Users can verify this before depending on us.
- No regulatory surface. We are an oracle (informational) and a skill (software distribution). We are not a financial product, not a custodian, not a broker. This is the same legal posture that lets Chainlink operate across jurisdictions.
- No platform tax. Whatever venue the user picks pays us per oracle call via x402. We don't pay them rev-share to be listed.

**Decentralized execution.** Chain-agnostic dispatch through neutral adapters (`exec_adapters/okx.py`, `sendai.py`, `backpack.py`) means no venue can cut us out. Compare:

- Stripe is locked into the card networks. If Visa changes interchange, Stripe absorbs it.
- Coinbase Agent Kit will be locked into Coinbase. If Coinbase changes fees, every agent built on it eats the change.
- Gecko is locked into nothing. If OKX changes terms, the user switches to SendAI in one config flip. The agent keeps running. We keep getting paid per oracle call.

The wallet/facilitator neutrality rule in `CLAUDE.md` is not a hedge — it is the moat. Every time we hard-code one venue, we trade strategic optionality for short-term integration speed. We don't do it.

### 1.7 Success metrics by horizon

Numbers a co-founder can validate, not vanity. No chunk counts. No model usage stats. Outcome metrics only.

**v0.1 (4 weeks):**

| Metric | Target | How to validate |
|---|---|---|
| Verdict-grounded coach completion rate | ≥ 60% of coach sessions emit a schema-valid strategy spec with citations on every rule | Read `agent_journal` for any coach run; count `entry` events with non-empty citation arrays |
| Hallucinated-trade rate vs. Tavily-direct | < 50% of baseline on the 10-prompt trading test set | The Pattern D defensibility test from CLAUDE.md. If we don't beat Tavily here, the oracle thesis collapses |
| Advisor-mode agent uptime on a real laptop | ≥ 72h continuous run with at least 2 sleep/wake cycles, journal intact | Founder dogfood — run an agent on Ernani's machine through the OKX contest window |
| Distinct provider_kinds in citations | ≥ 3 per verdict (canon_marks + canon_berkshire + paysh_live as the minimum baseline) | Already verified post retrieval-wedge fix |

**v1.0 (3 months):**

| Metric | Target | How to validate |
|---|---|---|
| Verdict-gated trades that beat a passive baseline | > 50% over a 30-day window | Backtest harness must be live; baseline = idea-without-verdict execution |
| Paid sessions per week | ≥ 50/week steady state | Stripe-equivalent dashboard on x402 settlements |
| Pro tier mix | ≥ 20% of paid sessions on Pro | Per existing PRD V2 target |
| Agents running in trader mode | ≥ 25 unique user wallets with a live `agent_state.status=trading` doc | Mongo query against `gecko_trade_agent.agent_state` |
| pay.sh listing live and producing inbound traffic | ≥ 10 sessions/week from pay.sh referrers | Referrer tag in session metadata |

**v2.0 (6+ months):**

| Metric | Target | How to validate |
|---|---|---|
| External-agent oracle calls (frames.ag / Bazaar / Apify integrations) | ≥ 1k/week | Per-call x402 receipts grouped by caller-wallet |
| Gecko-authored strategies published to partner registry | ≥ 10, each with non-zero subscriber count | Partner registry public listing |
| Creator-attribution settlements on-chain | ≥ $5k/month flowing to cited creators | Per existing PRD V3 target |
| Categorized retrieval endpoint revenue | ≥ 30% of total revenue | Settlement ledger by endpoint kind |

The first row of each table is the existential test. If verdict-grounded coach completion is low at v0.1, the coach surface is wrong. If we don't beat Tavily on hallucination rate, the oracle is wrong. If verdict-gated trades don't beat baseline at v1.0, the wedge claim is false. If external-agent calls don't reach 1k/week at v2.0, the KaaS thesis is wrong and we have to retrench to a vertical app. We re-check these in that order at each gate.

---

## Section 2 — ICP, user journey, and outcomes
*— Product lens (product-manager)*

This section is the load-bearing artifact between Section 1 (the business frame) and Section 3 (the surface design). If the journey here is wrong, every screen and CLI panel you design downstream will be wrong with it. Push back hard on anything that doesn't feel right — the journey is a falsifiable claim, not a settled answer.

### 2.1 Who we are building for in the next 90 days

We have one primary ICP, one named secondary, and a clear future-state we are explicitly NOT designing for. Two ICPs in one push means zero ICPs — keep that rule honest.

| | **Vibe trader / builder-developer (primary, v0.1)** | **Pioneer / data contributor (secondary, additive)** | **Trading desks / quants (future, OUT of scope)** |
|---|---|---|---|
| Named persona | "Caio v2" — same persona shape as `docs/icp.md`, retargeted to the trade vertical. 28, full-stack dev in São Paulo, lives in Claude Code, has $50–$1,000 of conviction capital sitting in Phantom or Backpack and is angry that "vibes-only" execution lost him money on the last two memecoin cycles. | KOL or independent analyst (Crypto Twitter, Substack, a Telegram desk) who already writes attested signals or strategy notes for an audience. They are *not* a buyer of Gecko; they are a supplier whose alpha grounds it. | Multi-strat desks running Python/Rust agents at HFT cadence with paid Bloomberg / Kaiko / Pyth-pro feeds. |
| Trigger that brings them in | Sees `app.geckovision.tech/skill.md` linked from `gecko-claude`, a Superteam Brasil Discord post, or an OKX skill catalogue entry. The trigger is **"my last trade was vibes-based and I lost money I can't afford to lose."** | Sees a Gecko-published, dissent-grounded strategy on the partner Attested Alpha Registry with their citation attached and a revenue-share line item. Trigger is **"my notes are being cited and paid; I should publish more here."** | n/a — they are not in the funnel. |
| What they are trying to accomplish | Take a thesis they already half-believe ("ETH/SOL reversion on the Friday CPI print"), get it adversarially stress-tested against investor canon + on-chain freshness, and walk away with a *deployable strategy spec* that runs on their own laptop in advisor mode while they sleep. | Earn passive revenue share when their attested signal seeds a Gecko cell, per the bounty reframe in `project_pioneer_as_bounty_reframe.md`. | n/a |
| What they use today | A mix of: GPT-4 with no grounding, CT threads, Discord alpha groups, Phantom mobile, screenshots of TradingView, and gut. The status quo is *unstructured conviction* + manual execution. | Twitter, Substack, paid Telegram. No on-chain attribution; no revenue tied to citation. | Internal stack — not displaceable by us. |
| What success looks like for them | A deployed advisor agent surfacing opportunities they would have missed, with a per-rule citation trail they trust, having spent <$5 in oracle calls to get there. Then: graduating to trader mode (v0.2) once they trust it. | First revenue-share payout from a Gecko cell their signal seeded. | n/a |
| Status | `hypothesis` — needs three watch sessions with vibe-trader candidates before we move to `validated`. The original `docs/icp.md` Caio (hackathon builder) is the same persona shape, retargeted; do not treat the trade-vertical persona as `validated` just because the hackathon one is closer to validation. | `hypothesis` — depends on the partner registry shipping, which it has NOT. Until then, this ICP is a story we tell, not a behaviour we observe. | n/a |
| Anti-persona check | NOT the funded-founder-with-14-months-runway (still the wrong clock — see `docs/icp.md` §2). NOT the casual retail trader who just wants signals (they want certainty, not dissent; sending them Gecko is a category error). | NOT a Gecko buyer wearing a different hat — if a pioneer also pays as a trader, they're two separate journeys, do not collapse them. | n/a |

Two things to flag before you design against this:

1. **The Pioneer journey does not exist yet in product surface.** The partner registry is not built. Per `project_three_layer_architecture_2026_05_11.md`, until the registry ships, "build custom" is the only flow. So for the next 90 days, **design for the vibe trader; reserve a Pioneer placeholder you can light up later.** Do not blend the two journeys.
2. **The primary ICP is technically literate but financially small.** $50–$1,000 of conviction capital is the right anchor. Anyone with more is a different journey (insurance shape changes, paper-trade gate behaves differently). Anyone with less is not yet a buyer; they're a learner, and v0.1 is not a teaching product.

### 2.2 The end-to-end user journey (vibe trader, v0.1)

This is the journey we design every surface against. Each step names: what the user does, what we charge, what they see, what they leave with.

| # | Step | User does | We charge | User sees | User leaves with |
|---|---|---|---|---|---|
| 1 | **Discover** | Reads a Superteam Brasil Discord post or an OKX skill-catalogue entry pointing at `app.geckovision.tech/skill.md`. | $0 | A one-paragraph promise + one terminal screenshot of a verdict. | The decision to run one command. |
| 2 | **Install** | `Read app.geckovision.tech/skill.md` in Claude Code → bootstrap, then `curl -fsSL app.geckovision.tech/install.sh \| bash`. | $0 | Skill registers; `bb` and `gecko-trade-coach` show up; `gecko-mcp doctor` passes. | A wired local environment. No login, no signup. |
| 3 | **First paid verdict** (cold-start trust) | In Claude Code: "I'm thinking ETH/SOL mean reversion on the Friday CPI print — verdict?" Coach invokes `gecko_trade_research` (basic tier). | **$0.25 USDC via x402** (stub for testing, live when flipped per `project_x402_stub_then_live.md`). | A KILL / REFINE / BUILD verdict with surviving dissent + 3–5 cited canon chunks. | A first taste of the wedge — grounding they did not get from ChatGPT. |
| 4 | **Coach session** | Multi-turn conversation with `gecko-trade-coach` — risk tolerance, capital size, venue preference, time horizon. Coach asks; user answers. | $0 (free conversation) | A profile being built, citations attached to each suggested rule. | A profile + a candidate strategy template. |
| 5 | **Strategy spec emit** | Coach calls `gecko_trade_research` per meaningful decision (template choice, sizing tier, exit rule). Each call grounded; per-rule citation IDs baked into spec. | **~3–5 × $0.25 basic verdicts** during the build (cache hits are free). Total: ~$1–$2. | A schema-validated JSON strategy spec rendered as a human-readable summary. | A deployable spec on disk at `~/.gecko/trade-agent/specs/<name>.json`. |
| 6 | **Deploy agent (advisor mode)** | `"Deploy strategy <name>"` → coach invokes `bb trade-agent up --spec <path> --mode advisor`. Per `project_trade_vertical_v01_decisions_2026_05_11.md`, **v0.1 is advisor-only.** | **$0.75 USDC pro-tier startup verdict** on cache miss; **free on cache hit** (cache-then-charge). | "Agent <id> deployed in advisor mode. Watching N tokens. Will surface opportunities; will not sign." | A running long-lived Python process on their laptop. |
| 7 | **Review opportunities** | Agent surfaces candidates in the journal: "spot reversion signal triggered on SOL at 09:14, dissent: macro voice flagged CPI surprise, see citation X." User reads, decides manually whether to execute via their existing wallet. | **~$1.50/day** in scheduled re-verdicts + triggered breakers (per §5 of the design spec). Cached entries are free. | A journal tail in `bb trade-agent inspect <id>` and Claude Code summaries. | Trades they would not have made on vibes; trades they avoided because dissent was strong. |
| 8 | **Graduate to trader mode (v0.2, not this push)** | Once advisor telemetry shows the agent's dissent and verdicts are trustworthy, user flips `--mode trader`. Gated execution kicks in via OKX / SendAI / Backpack adapter. | Same oracle math + adapter fees pass-through. | "Trader mode armed. Paper-trade gate active for 24h." | Autonomous execution they trust. |
| 9 | **Publish back to registry (future)** | When partner registry exists, user one-clicks "publish my strategy" — Gecko-attested, dissent-baked, citation-attributed. Per `project_three_layer_architecture_2026_05_11.md` this is bidirectional from day one of registry GA. | $0 to publish; revenue share back via the bounty model in `project_pioneer_as_bounty_reframe.md`. | A "published" badge on the strategy + future earnings ticker. | The vibe trader becomes a Pioneer. The two ICPs join. |

**The critical design moment is step 3.** That is where we earn the right to step 4. If the $0.25 first verdict does not feel like a *qualitatively different answer* than ChatGPT-with-no-grounding, the journey dies and we never see the same user again. Every other step has slack; step 3 has none.

### 2.3 Outcome metrics — what success looks like FOR THE USER

These are user outcomes, not our metrics. The business-manager section owns ARR / retention / unit economics. Here we only care about whether the user leaves better off.

| Metric (user-facing) | Tied to step | Pass bar |
|---|---|---|
| **Verdict-gated PnL vs ungated baseline** over a rolling 30-day window per user. "Did my trades that survived a Gecko verdict outperform my trades that did not?" | Steps 3, 7 | ≥ +200 bps median lift across the cohort of users who completed ≥10 verdict-gated and ≥10 ungated trades. Below that, the wedge is decoration, not utility. |
| **Time-to-first-deployed-agent** from `install.sh` to `bb trade-agent up` exit code 0. | Steps 2 → 6 | ≤ 45 minutes for a user who has never seen Gecko before, in a screen-share watch session. Above 90 minutes the onboarding is broken and the coach is doing too much. |
| **Verdict-citation recognition rate** — when shown the chunks Gecko cited, can the user name the source ("oh that's a Howard Marks memo") or do they squint and say "I don't know what this is"? | Step 3 | ≥ 60% citation recognition on first verdict. Below that we have a "powered by Howard Marks" badging problem, not a corpus problem (see §2.6). |
| **"Would you tell another trader about this" (binary NPS)** asked at end of first session and 7 days after first deployed agent. | Steps 3, 7 | ≥ 50% yes at session 1; ≥ 70% yes at day-7. If day-7 is below session-1, the agent is not delivering and we are running on novelty. |
| **Dollars spent on Gecko vs trade size** ratio. Caio with $500 of conviction capital spending $5 in oracle calls to deploy is a healthy ratio; spending $40 is not. | Steps 5–7 | < 1.5% of working capital per first session. Above 3% the cache-then-charge model is failing and we need to revisit. |

Notice what is NOT on this list: model accuracy, token usage, retrieval F1, agent uptime SLO. Those are internal eval-harness concerns owned by ai-ml-engineer and software-engineer. The user does not see them.

### 2.4 The layer handoff — coach, oracle, agent, execution

The user has to understand three Gecko-owned components (coach, oracle, agent) and one external rail (execution). Per `project_three_layer_architecture_2026_05_11.md`, this is the spine of the product. The handoff between them is the most under-designed surface today.

```
                    USER (Claude Code session)
                            |
                            v
   +----------------------------------------------------+
   |                    COACH                           |
   |  conversational, multi-step, slow                  |
   |  builds profile + strategy spec                    |
   |  calls oracle at every decision point              |
   +-------------------+--------------------------------+
                       | invokes per-rule
                       v
   +----------------------------------------------------+
   |                   ORACLE                           |
   |  gecko_trade_research — single MCP call            |
   |  fast, paid ($0.25 basic / $0.75 pro)              |
   |  verdict + surviving dissent + citations           |
   |  cache-first per idea_hash                         |
   +-------------------+--------------------------------+
                       | strategy spec handed off
                       v
   +----------------------------------------------------+
   |                    AGENT                           |
   |  long-running, on user's laptop                    |
   |  advisor mode (v0.1) — surfaces, never signs       |
   |  trader mode (v0.2) — gated execution              |
   |  re-invokes oracle: scheduled 24h + triggered      |
   +-------------------+--------------------------------+
                       | (trader mode only)
                       v
   +----------------------------------------------------+
   |          EXECUTION RAIL (NOT Gecko)                |
   |   OKX Agentic Wallet / SendAI / Backpack           |
   |   neutral; user chooses; we delegate               |
   +----------------------------------------------------+
```

The handoffs the user actually perceives:

- **Coach → Oracle:** invisible. The coach calls the oracle silently; the user sees a verdict bubble inline in the conversation. They should never have to type "now call the oracle." If they do, the coach is broken.
- **Coach → Agent:** explicit, ceremonial. "Deploy strategy" is a moment. The user sees the agent boot, the startup verdict run, the journal initialize. This is the second emotional peak of the product (the first was step 3, the first verdict).
- **Agent → Oracle (re-invoke):** mostly invisible (cache hits) with the occasional surfaced moment ("re-checking the thesis — CPI just dropped"). Per the re-verdict triggers in the design spec §5, this is rate-limited and bounded; the user should feel "the agent is awake" without feeling "the agent is panicking."
- **Agent → Execution rail (v0.2 only):** loud and confirmable. Every trade in trader mode is gated by paper-trade then explicit user opt-in. No silent first trade — ever.

The single non-obvious thing for design: **cache-hit vs cache-miss is a UX state, not just a backend detail.** When the oracle returns from cache, the verdict should still feel grounded (citations still rendered) but should *not* charge or spin. When it returns on miss, the spinner and the charge happen together. Conflating these breaks the trust loop.

### 2.5 What the user is allowed to NOT understand

The designer's job is to hide these. The user's job is to never ask about them.

**Must own (surface visibly):**
- The **strategy spec** — they know it's a file, they know they can edit it, they know it's the source of truth for their agent's behavior.
- **Deploy / stop / pause** as verbs for the agent. They control the lifecycle.
- **Fund their wallet** — they own their capital; Gecko never custodies. The "fund my agent" intent in `gecko-wallet` delegates to OKX peer skills (`docs/strategy/2026-05-11-trade-vertical-expansion.md` §2).
- **The verdict** — KILL / REFINE / BUILD and its surviving dissent. This is the product.
- **Cited sources** — at the bucket level ("Howard Marks memo", "Pyth on-chain", "macro paper"), not the raw chunk IDs.

**Must NOT need to understand (hide entirely):**
- x402, USDC settlement mechanics, facilitator routing. Per `project_x402_stub_then_live.md`, the stub→live flip is operator-side; the user just sees "$0.25 paid."
- Embeddings, pgvector, Mongo Atlas Search, the canon corpus shape. Per CLAUDE.md "What to Never Show": no model names, no token counts, no per-operation costs.
- AG2 / GroupChat / the 5-voice panel architecture. Users see voices' *outputs* (surviving dissent), not the orchestration.
- ProviderKind taxonomy, idea_hash cache logic, freshness tiers.
- Whether the verdict came from cache or fresh model call (we surface "verdict" — we don't surface the path).
- Helius credits, Pyth feeds, Tavily — these are roads, not destinations (`project_knowledge_as_commodity_pivot.md`).

The line is sharp: **users own the strategy spec and the wallet. We own everything else.** If a designer ever feels the urge to render "model: gpt-4o" or "tokens used: 1,247" — that's a CLAUDE.md violation and the answer is no.

### 2.6 Where v0.1 will feel rough (be honest with the designer)

These are not bugs we will fix before shipping. They are *known sharp edges* and the design's job is to absorb them, not paper over them.

1. **Agent on a laptop is brittle.** Sleep / wake / wifi drop / OS-killed-the-process. Per the risk register in the trade-vertical design spec (§8), we mitigate with journal+checkpoint on every event and a `bb trade-agent doctor` command, but users *will* find their agent dead on Monday morning. **Design implication:** the `inspect` and `ls` surfaces must make health state obvious at a glance. Last-heartbeat timestamp is non-negotiable. A green dot is not enough — show "last tick: 14 min ago" or "DEAD: process exited at 09:14."
2. **Paper-trade gate adds latency before trader mode.** v0.2 trader mode requires a 24h paper-trade window after deploy. Users who want to trade *now* will feel friction. **Design implication:** the paper-trade window is sold as a *feature* ("your agent earns its license") not an obstacle. Show simulated PnL prominently during the window so they have something to watch.
3. **The partner Attested Alpha Registry does not exist yet.** The Pioneer journey is on paper. "Pick from registry" is not a button we can render. **Design implication:** do not design a registry-browse surface for v0.1. Design "build custom" as the only flow, and leave a single registry hint in the coach onboarding ("future: pick from KOL strategies") with no clickable affordance. Renderings of unbuilt features waste design cycles and lie to the user.
4. **Citations may surface chunks the user does not recognize.** The investor canon (Marks, Damodaran, Mauboussin, Berkshire letters — see §6 of the design spec) is well-known in finance but obscure to a 28-year-old Solana dev. A raw "[chunk_8a3f from canon_marks]" looks like noise; a **"Howard Marks — Oaktree memo, 2008"** badge with author photo looks like authority. **Design implication:** every citation needs source-bucket branding (author photo, source-family name, year if applicable) in the render layer. This is one of the highest-leverage things you can ship in v0.1 — the citation render is the difference between "this LLM is making things up" and "this is grounded."
5. **The verdict can be KILL — and we mean it.** Per `docs/icp.md` §3 and the direction-quality bar, "stop building this" is a legitimate output. Users who paid $0.25 to be told "no, don't trade this" may feel ripped off if KILL is rendered as a flat red banner. **Design implication:** KILL is the *most valuable* verdict, not the worst. Render it as a save — "we just saved you from a bad trade" — with the surviving dissent making the case. Refunds for KILL verdicts are not the answer; better rendering is.
6. **Cache-hit verdicts return instantly with no charge.** Users may distrust "free + instant" after paying for the previous call. **Design implication:** render cache-hit verdicts identically to fresh ones but with a subtle "cached <ts>" affordance. Do not hide it; do not celebrate it. The trust comes from consistency of *content*, not consistency of latency.
7. **OKX contest is not the deadline.** Per `feedback_okx_no_funding_pressure.md`, the contest is opt-in secondary work. **Design implication for the co-founder:** do not let any rough-edge prioritization decision get framed as "but we need this for the OKX deadline." That deadline is not binding on us. If a surface is not ready, we ship without OKX, full stop.

---

The single test for this section: if a designer reads it and can produce a screen-by-screen storyboard of the first session without asking "but who is the user?" or "but what are they trying to do?" — it works. If they ask either question, the section failed and I need to rewrite it. Push back.

---

## Section 3 — Surfaces, flow, and the design brief
*— Design lens (product-designer)*

This section is for you. Section 1 framed the business; Section 2 sequenced the build. My job is to tell you what we are actually designing, in what order, in which voice, and — just as important — what we are deliberately not designing. Read this as a peer brief, not a spec. You will own the craft from here.

### 3.1 Surface inventory

Everything we ship in v0.1 lives in one of six surfaces. None of them is a dashboard. None of them is a SaaS app. Most of them are text.

```
                    [ app.geckovision.tech ]            <- Next.js, we own pixels
                          |
                          v
        [ gecko-claude/skill.md + install.sh ]          <- Markdown + bash, we own copy
                          |
                          v
  +---------+   +---------+   +---------+   +---------+
  |  COACH  |   | WALLET  |   |  AGENT  |   | VERDICT |  <- CC skill turns, JSON envelopes
  |  skill  |   |  skill  |   |  skill  |   |envelope |     we own structure, Claude renders
  +---------+   +---------+   +---------+   +---------+
                                                |
                                                v
                            [ OKX Plugin Store listing ]  <- their chrome, our copy + icons
```

| Surface | Renderer | We control | They control |
|---|---|---|---|
| Marketing site (`app.geckovision.tech`) | Next.js, ours | Everything | — |
| `skill.md` + `install.sh` (gecko-claude) | GitHub + curl | Copy, structure | GH/terminal chrome |
| Coach skill turns | Claude Code CLI | JSON shape + copy hints | Claude's prose layer |
| Wallet skill turns | Claude Code CLI | Confirmation copy, JSON shape | Claude's prose |
| Agent skill turns | Claude Code CLI | Lifecycle JSON + copy hints | Claude's prose |
| Verdict envelope | Whatever consumes it (CC, OKX wallet, downstream agent) | JSON schema, citations, dissent | Renderer's choice |
| OKX Plugin Store entry | OKX wallet chrome | Listing copy, icon, screenshots | Their visual language |

The asymmetry is the whole point: **we control the marketing site fully, and beyond that we ship structured copy + JSON.** Read on.

### 3.2 Output positioning — we are the insight layer, not the design layer

This is the most important constraint and it comes straight from `project_output_layer_positioning.md`. Gecko produces *structured insight*. Claude (Code, Design skill, whatever the user wires up) is the renderer. The OKX wallet is the renderer. A downstream trading agent is the renderer. We are upstream of all of them.

What this means for you in practice:

- We do not ship a chart library. We do not ship slide decks. We do not own a "PDF export" pipeline.
- We ship **JSON schemas with well-named fields and good copy values inside them**. The schema *is* design work — `claim`, `dissenter`, `cycle_signal` are design decisions.
- The "look" of a verdict in CC is Claude's prose-rendering of our envelope. Our craft lives in the envelope's *shape and word choice*, not its pixels.
- The one place we own pixels is `app.geckovision.tech`. That is where visual design effort concentrates. Everywhere else, the design work is **information design and copy**.

If you find yourself drawing a card mock in Figma for the verdict, stop. The deliverable is "what fields, in what order, with what labels". Claude will render them.

### 3.3 Three voices, in tension

We have three audiences and three voices. Pick deliberately.

**Coach voice — calm, opinionated, never textbook.**
The coach is a CC skill flow (the 6 steps in `gecko-trade-coach/SKILL.md`). It asks one question at a time, refuses to lecture, and is willing to push back. Mirrors the `starter-coach` precedent referenced in `project_okx_skill_architecture_2026_05_11.md`. Never say "Beta measures systematic risk relative to..." Say "How rough a week are you willing to have?"

**Verdict voice — terse, attributed, no personality.**
The verdict envelope (`gecko_trade_research` return) has no warmth. Claims are short. Every claim carries an author and a URL. The panel's dissenters survive into the output. This is the voice of `project_2026_05_09_session_endstate.md` — "kamino+vertical=dex returned 3-4 cites" is the energy. Numbers and names, not adjectives.

**Marketing voice — direct, lowercase, builder-coded.**
Matches the existing gecko-mcpay-app site copy. Sentence fragments. "no API keys just a wallet." Lowercase H1s. No filler. No "revolutionize". No "AI-powered". The reader is a builder; treat them as one.

Rule of thumb: if you cannot tell which voice a piece of copy is in, it is wrong.

### 3.4 Animation and interaction budget

Almost zero animation. Be ruthless here.

- **Coach / wallet / agent skills**: terminal text. No spinners on opinion-bearing turns (see anti-patterns). The `rich.progress` budget is reserved for the genuinely long operation — research/indexing — and only there.
- **Verdict envelope**: JSON. No animation. Renderer decides.
- **OKX listing**: static. Icon + screenshots + copy.
- **Marketing site**: this is where motion lives if it lives anywhere. One hero moment showing the coach -> verdict -> agent flow is enough. Do not over-build.

The 80/20 of your design hours: ~60% on marketing site + OKX listing, ~30% on envelope information design, ~10% on copy for skill turns. If you are spending hours on a CLI spinner, something is wrong.

### 3.5 The "feel" of the wedge

When a verdict comes back, the user should feel like **a panel of analysts disagreed in a room and the disagreement survived to the page**. Not "the AI thinks X". This is the brand and it is fragile.

Concrete craft choices that protect it:

- Cite by author name, not chunk id: `Howard Marks — cycle late-stage (2024)`, never `chunk_4d7a`. Author + claim + url. The `project_2026_05_09_session_endstate.md` work already enforces this in the envelope; your job is to make the rendered form respect it.
- **Surface the count of surviving dissenters.** "3 of 5 advisors agreed; 2 dissented — see panel.dissent". A unanimous verdict and a 3-2 verdict must look different.
- **Never collapse the panel into a single voice.** No "Gecko thinks...". The output is always attributed — either to a panel member or to a cited source.
- **Confidence is a number inline, never a gauge.** `confidence: 0.62` reads as "the panel was not certain", which is the honest signal. A semicircle gauge would lie about precision we do not have.
- **Show the cycle frame.** Per `project_three_layer_architecture_2026_05_11.md`, the coach + oracle layer is meant to surface where in the cycle we are. The verdict should make that visible, not bury it.

### 3.6 What you own next, in priority order

1. **Verdict envelope information design.** Field names, ordering, copy values for `panel.dissent`, citation block, `cycle_signal`. This is the highest-leverage design work in the company right now because every other surface renders this envelope. Pair with ai-ml-engineer on field names.
2. **Coach output card** — when the coach returns the strategy spec (end of the 6-step flow), what does CC render? Markdown structure, section ordering, where the "are you sure?" lives. See `gecko-trade-coach/schema.json`.
3. **Marketing site update** — new hero on `app.geckovision.tech` reflecting strategy-oracle positioning and the OKX story. Copy first, layout second.
4. **OKX Plugin Store listing** — icon, three screenshots, listing copy. Small surface, high leverage, must match OKX visual language without losing our voice.
5. **Agent lifecycle copy** — "deployed", "running", "stopped", "halted by guardrail". Confirmation feel for irreversible actions (deploy, stop).
6. **Wallet skill confirmation copy** — natural-language wallet ops are scary; the confirmation step is the entire UX. Treat it like the "send money" screen of a bank app, but in text.

### 3.7 Anti-patterns — do not do these

1. **Do not design a "chart of citations".** Citations are dense textual artifacts. A numbered list with author + claim + url is the right form. A bubble chart of source authority is not.
2. **Do not add a "thinking..." animation to the coach.** Opinion-bearing turns should feel deliberate. Loading spinners on a coach signal "the model is generating" — exactly the wrong frame. Indexing/research is the only place a progress bar earns its keep.
3. **Do not render the AG2 panel as 7 named avatars.** The agents are abstract; the personas are an internal implementation detail. Surface dissent and authority, not characters.
4. **Do not build a "verdict score gauge".** Confidence is a float; render it inline like `confidence 0.62`. A gauge implies a precision we do not have and makes the output feel like a credit score.
5. **Do not collapse the wallet skill into "one-click trade".** Natural-language wallet ops live or die on the confirmation step. Two-step minimum, always.
6. **Do not design a dashboard.** No "my strategies" grid, no portfolio view, no settings panel. Per `project_output_layer_positioning.md` and the broader KaaS positioning in `project_kaas_positioning_2026_05_08.md`, we are an oracle skills call. Dashboards are a different company.

### 3.8 One-line summary

Design the JSON, design the copy, design the marketing site. Let Claude render the rest. The wedge is dissent-checked judgment — make it feel earned, never gimmicky.

---

## Closing — agreement protocol

This brief is shared so we can disagree on paper instead of in code. Three things to align on this week before any design work starts:

1. **The vibe-trader ICP for the next 90 days** — do you agree this is the only persona we design for, with Pioneer kept as a placeholder? (Section 2)
2. **The output-layer-not-design-layer constraint** — do you agree we own JSON + copy + marketing site, and Claude renders everything else? (Section 3)
3. **The three voices** — coach / verdict / marketing. Do you read these as distinguishable, or do they need sharpening before you can write to them? (Section 3)

If we agree on those three, the first design tickets are: (a) verdict envelope information design, (b) coach output card, (c) marketing site hero update for the strategy-oracle positioning. Everything else is downstream.

---

**Source documents and memory entries cited:**
- `docs/PRD.md`, `docs/product-story.md`, `docs/icp.md`
- `docs/strategy/2026-05-11-trade-vertical-expansion.md`
- `docs/strategy/2026-05-11-retrieval-wedge-sprint.md`
- Memory: `project_kaas_positioning_2026_05_08`, `project_knowledge_as_commodity_pivot`, `project_three_layer_architecture_2026_05_11`, `project_okx_skill_architecture_2026_05_11`, `project_okx_diagnostic_2026_05_11`, `project_real_competitors_2026_05_07`, `project_trade_vertical_v01_decisions_2026_05_11`, `project_pioneer_as_bounty_reframe`, `project_output_layer_positioning`, `project_x402_stub_then_live`, `feedback_okx_no_funding_pressure`
