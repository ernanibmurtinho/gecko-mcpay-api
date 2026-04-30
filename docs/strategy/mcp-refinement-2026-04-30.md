# MCP refinement — Sprint 12 thesis stress-test

**Date:** 2026-04-30
**Cost:** $0.015 (basic research + 5-voice plan, devnet x402)
**Session:** `153124bc-0784-4ae1-a958-ea81069bf997`
**Tx:** `2gyLFhQyyhfsNa4UG1MrncHyPnDDzedeLDeJ74dk7Dp4NF2x24dct48EwPKNM2GLM5ybx9bx37bTkoZvQyMr8SFu` (Solana devnet, frames.ag)

This is the first run of the new two-stage operating pattern (`feedback_dogfood_loop.md`). Stage 1: MCP agents bring outside-in signal. Stage 2: local Claude Code subagents implement.

---

## What the dogfood actually surfaced

### Convergent signal (3+ voices independently agreed)

1. **Latency is the binding constraint, again.** CEO ("5-voice debate is commercially suicidal pre-revenue"), CTO ("MCP clients timeout after ~30s, serial 5-turn debate will never complete"), and PM ("agent operators will not tolerate 20-30s latency; their agents will timeout or yank the MCP and hardcode a heuristic") all named latency as the top product risk. This **extends** product-designer's S12 memo finding: PD said latency was the binding constraint for *Pro+ source bundling*; the panel says latency is the binding constraint for *the entire 5-voice debate*. Implication: **parallel fan-out needs to extend INTO the debate itself**, not just source dispatch. That's a bigger refactor than what's currently in Sprint 12.

2. **Agent ICP needs tightening.** PM proposed: drop "general AI agent developers"; narrow to "crypto-native autonomous agent operators running 24/7 wallet-enabled agents." This is the ICP for the **Bazaar listing surface** (different from the founder ICP we converged on in Sprint 11). The two-ICP framing already lives in `docs/strategy/bazaar-deeper-thesis-2026-04-30.md` (founders + agents + sellers); PM's specificity sharpens the agent slot.

3. **Bazaar without product-fit is a graveyard.** CEO explicit: "listing in a marketplace with no liquidity is a vanity metric that fools founders into thinking they have go-to-market when they actually have a graveyard." This isn't an argument *against* listing — it's an argument that latency + price + first-call experience must work or the listing is performative. Pairs with the latency convergence above.

### Useful disagreements (signal in the dispute)

- **Pricing tension: CEO ($0.10 wedge price) vs. BM ($9 DeFi suite).** Resolution: these aren't competing — they're different ICPs. $0.10 is the discovery price for agents (Bazaar listing). $9 is the founder-vertical price. The pricing ladder already accommodates both (Free / $0.10 / $0.75 / $9 / $12-19 / $29).
- **PM disagrees with CTO on RAG depth: 6-source 5-voice in V1 vs. 2-voice cached <2s.** PM's call (cached + fewer voices) sharpens latency response. Don't gut the 5-voice debate — but **cache aggressively at the chunk level** (echoing CTO's LRU cache point, also in staff-engineer's S12 memo).
- **BM disagrees with PM on rail expansion before verdict quality.** BM right: improve RAG + debate prompts before adding rails. This **re-prioritizes** Sprint 12: rubric v2 + transcripts (Track G) is more urgent than CDP Facilitator (Track A). Track G must land first.

### Concrete BD ask (BM)

- **Coinbase Developer Platform partnership:** pitch a featured "Agent Authorization Gateway" listing in CDP Bazaar. Joint blog post + tutorial + CDP developer newsletter. Owner on their side: CDP Ecosystem Manager / DevRel Lead. First ask: 30-minute call. **Effort:** chore, not ticket. Add to Sprint 12 chore list.

### Useful new feature (PM)

- **"Already exists" Red Team voice.** New advisor persona that cites live GitHub stars + last-commit dates for prior art. Borrowed from Colosseum's verdict patterns. Sharp because it directly attacks BUILD verdicts that the model wants to over-confirm. **Slot:** Sprint 13 ai-ml-engineer Track A (DeFi suite prompts gain a Red Team variant) or Sprint 13.5 cleanup.

---

## What the run also surfaced about our own production

Three issues found in the live MCP response that are **diagnostic value** of running this pattern:

1. **Track A's verdict shape is NOT in the deployed MCP response.** No `verdict: "KILL|REFINE|BUILD"` field. Either gecko-api is running pre-Sprint-11 code or the MCP serializer skipped the field. **Sprint 12 add (chore):** verify Track A reaches the deployed MCP surface; if not, redeploy.

2. **Paragraph appeared organically in source discovery!** `https://paragraph.com/@arcabot/the-mcp-takeover-defi-protocols-are-racing-to-build-doors-for-ai-agents` — but indexed 0 chunks. Theme 2 (Paragraph connector, Sprint 14) is **exactly** the right next move; the discovery layer wants Paragraph but the extractor can't read it. Pre-validates the strategic call.

3. **Two voices failed closing-line detection again** (CTO, staff_manager). Sprint 9 Track B fixed `business_manager` but the issue is back for other voices. **Sprint 12 ai-ml-engineer chore:** triage why retry-with-stricter-suffix is failing for these models specifically. Likely model-specific (Kimi K2.6 + GPT-4.1-nano have different formatting behaviors than the gpt-4o family the original fix targeted).

4. **Staff_manager voice (GPT-4.1-nano) was the weakest.** Generic, no synthesis. Model choice is too light for the synthesis role — should use a stronger model (gpt-4o-mini at minimum). **Sprint 12 ai-ml-engineer ticket candidate:** model-routing per-voice based on cognitive demand, not flat tier-preset.

---

## Sprint 12 amendments proposed

These are concrete edits to the existing Sprint 12 plan (`docs/build-plan-sprint-12.md`):

### A. Re-order tracks by criticality

Current order: A (CDP) / B (Bazaar declarations) / C (smoke) / D (wallet docs) / E (research) / F (SourceProvider seam) / G (transcripts + rubric v2).

**Proposed order:** **G first, F second, A/B/C third, D/E parallel.** Rationale per BM panel voice: improve verdict quality before adding payment rails. Track G (rubric v2) and Track F (provider seam) are quality-side; Tracks A/B/C are surface-expansion. Quality before surface.

### B. Add new track for latency

**Track H — Parallel-debate dispatch (S12-LATENCY-01).** Today's pro-tier 5-voice runs serially in `gecko_core/orchestration/pro/`. Refactor to `asyncio.gather` for the per-voice LLM calls where the GroupChat structure permits (analyst + critic can argue in parallel; architect waits on both; scoper waits on architect; judge synthesizes). Target: ~50% latency reduction for pro-tier runs without sacrificing debate quality. **Owner:** ai-ml-engineer + software-engineer. **Cost:** ~3 days. **Acceptance:** pro-tier wall_seconds drops from current p50 to half; eval gate passes unchanged.

### C. Add chores

- **CDP Ecosystem partnership outreach** (BM): 30-min call request to CDP DevRel for Agent Authorization Gateway featured listing
- **Verify Track A verdict shape reaches deployed MCP** (software-engineer): redeploy if needed
- **Triage closing-line retry failures on Kimi K2.6 + GPT-4.1-nano** (ai-ml-engineer): may require per-model strict-suffix templates

### D. Add Sprint 13 carry candidate

- **"Already exists" Red Team voice** as new advisor persona: cites live GitHub stars + last-commit dates for prior art. Defer to Sprint 13 (alongside DeFi suite prompts).

---

## What I would NOT change based on this MCP run

The CEO's most contrarian call ("ship Tavily-only + Stripe; abandon x402 + 5-voice debate temporarily") is the kind of advice you'd give if Gecko had no differentiator. We do — the panel proved it: even running on our own infrastructure, the 5-voice debate produced 4 distinct, sharp, contradictory perspectives that a single model wouldn't have generated. That IS the product. Stripe + Tavily + single-pass model is what the OrbisAPI proxies in CDP Bazaar already are (and they have <5 calls/30d, per `cdp-bazaar-2026-04-30.md`).

The CEO voice is correct that ICP conversion matters more than feature surface — but the right response is to **make sure the ICP converts on what we've built**, not to delete the moat. Sprint 12's listing + Sprint 13's DeFi vertical + the latency fix (new Track H) are the conversion-enablement work. Tavily + Stripe is the wedge that someone with no advisor panel would ship; we have the advisor panel.

---

## Pattern validation

This was the first run of the two-stage pattern. Verdict: **the pattern works.** The MCP refinement produced 3 convergent signals + 4 useful disagreements + 1 concrete BD ask + 4 self-diagnostic findings (verdict-shape leak, Paragraph 0-chunk, closing-line failure, weak staff_manager model) — for $0.015 in 5 minutes. None of the 7 local subagent memos surfaced the latency-into-debate point or the staff_manager model-routing issue.

**Recommended cadence:** every sprint boundary, run `gecko_research` (basic, ~$0.10 max) on the proposed sprint hypothesis + `gecko_plan` ($0.015) for the 5-voice panel reaction. Capture as a refinement doc. Then dispatch local subagents with the refinement as input.
