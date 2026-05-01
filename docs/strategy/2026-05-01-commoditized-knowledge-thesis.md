# Thesis — Commoditized Knowledge + Profile Reputation, Gecko as Orchestrator

**Date:** 2026-05-01
**Source:** user prompt, post-Sprint-13 closure
**Status:** validation pass — ask the team before treating as canonical

---

## The thesis (in user's words, paraphrased)

> publish.new becomes the substrate where any contributor (judge, investor, product manager, product designer, security researcher, etc.) writes paid knowledge. Each contributor is identifiable on-chain → reputation accrues per profile type over time. Knowledge becomes a commodity. **Gecko's main difference is agent orchestration + knowledge context** — we route across profile types, pull what's relevant, and synthesize into a judgment via the adversarial debate. Everything indexes into one space — Gecko's knowledge graph.

## How this extends the prior thesis

The bazaar-deeper-thesis frame ("Gecko = trust layer of the agentic economy", "Bazaar lets agents discover what to buy; Gecko tells them what to build") was about **judgment as scarce primitive in an agent-saturated world.** This thesis sharpens by naming where the *inputs* to that judgment come from: not Gecko-owned data, but a creator economy on top of publish.new + on-chain identity primitives, with Gecko as the orchestration layer.

Said differently:
- Old framing: Gecko = the judge.
- New framing: Gecko = the **clerk who picks the right judges**, runs the adversarial debate among them, and produces a verdict signed by all parties' reputations.

These are compatible — the new framing actually strengthens the old. The judgment Gecko emits is more trustable when it cites named-judge / named-investor commentary with on-chain attestable identity.

## What's new that V1 doesn't have

1. **Profile types.** Today citations are anonymous URLs. Profiles classify the contributor: judge, investor, PM, designer, security, founder, operator, etc. The classifier (in `gecko_core.classify`) needs a profile-type signal alongside category/vertical.
2. **On-chain identity binding.** publish.new contributors today are 0x wallet addresses. Reputation is implicit. Need a primitive that says "this 0x has been cited 47 times in DeFi BUILD verdicts that later shipped" — a cited-precedent counter, soulbound or attestation-shaped.
3. **Routing logic.** Per query, Gecko needs to pick which profile types to pull from. A "should I build this DeFi protocol?" needs DeFi judges + investors + on-chain analysts, not designers. The classifier already does this for sources; extending to profiles is the same shape.
4. **Aggregated index.** Today RAG hits per-source. New: a unified knowledge index keyed by (profile_type, idea_signature, reputation_score) so Gecko can rank contributions before retrieval.
5. **Creator monetization at scale.** Sprint 14 Track B + C wires Paragraph + publish.new for citations + verdict publishing. This thesis extends that to all profile types — every cited contributor earns from being cited.

## Validation question matrix — what each lens should answer

Each agent answers ≤500 words from their lens, focused on:

- **staff-engineer:** where does the new "profile registry" surface live? new first-party module or pluggable extension? Does the X402Client Protocol from S13-PAY-01 generalize to a "ContributorClient Protocol" or are these different abstractions? What's the architectural one-pager?

- **ai-ml-engineer:** the differentiation claim is "orchestration + context." Is that defensible against Perplexity / ChatGPT / Bazaar's own discovery? What does the orchestrator actually DO that those don't? New prompts? New routing model? RAG impact at scale (1000s of contributors)?

- **web3-engineer:** on-chain identity primitive. Soulbound token? Verifiable credentials? Attestation via EAS / Lens / Farcaster? What's the smallest workable shape for V1, and when does it need to land?

- **data-engineer:** the unified knowledge index. Schema? Per-profile-type partition? How does cited-precedent counting compose with the existing precedent table? Storage cost at 10k contributors × 100 cited posts each?

- **business-manager:** commoditization economics. If knowledge is commodity, what's Gecko's pricing model? Revenue split with creators? GTM for each profile type (judges first? investors first?). Two-sided marketplace cold-start problem.

- **product-designer:** the reveal moment changes. Today verdict + 5-voice debate + citations. Tomorrow verdict + "judges who weighed in" + "investors who agreed/disagreed" + "PMs who'd ship this" with attestable identity. How is this surfaced without overwhelming?

## Tensions to resolve (already visible)

1. **"Index everything in one space" vs SourceProvider Protocol.** S12 Track F's seam was *more* providers, not one consolidated index. Either (a) the unified index is downstream of the providers (every provider feeds it), or (b) Gecko owns the index and providers are deprecated. Big difference.
2. **Profile self-classification vs platform-verified.** Self-claimed "I'm a judge" is cheap and noisy. Platform-verified is gated and slow. Hybrid (self-claim + reputation accrual) is the obvious answer; need to spec.
3. **Differentiation drift.** "Orchestration + context" is also what Perplexity claims. The wedge is the verifiable creator economy underneath + adversarial debate output, not orchestration alone.
4. **V1-honesty vs V3 promises.** This thesis describes a V3 surface. The Sprint 14 plan can't carry it; needs a multi-sprint arc with explicit milestones.

## Asks for the team

Each lens produces a memo at `docs/strategy/profile-thesis-<role>-2026-05-01.md` (≤500 words). Headlines:

1. Does the thesis hold from your lens? (yes / refine / different framing)
2. What's the smallest concrete piece that could ship in Sprint 15-16?
3. Top risk you'd defend against
4. One Sprint 15+ ticket from your lane

Staff-engineer synthesizes after.

## Constraints for the team

- Use OpenRouter for any LLM calls (`LLM_ROUTER=openrouter`).
- No paid eval gates, no live mainnet smokes.
- Stub-mode only for any test runs.
- Don't touch the in-flight CDP smoke fixes (separate path).
- This is research/synthesis, not implementation. ≤500 words per memo, opinionated.
