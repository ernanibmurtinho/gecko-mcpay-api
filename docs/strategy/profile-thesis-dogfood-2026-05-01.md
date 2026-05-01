# Profile-typed thesis — dogfood findings

**Date:** 2026-05-01
**Mode:** stub (free, no payment)
**Sessions:**
- `bb research`: 235671f9-affa-493a-bd0d-826dd97484ef
- `bb plan`: same session
**Cost:** $0.016 LLM (advisor panel) + ~$0 stub research

---

## What we learned by pointing Gecko at its own thesis

### Convergence with team synthesis (4 of 5 voices + basic verdict agreed)

- **Verdict: REFINE** (basic tier) — matches the team synthesis "ship Sprint 14 as planned, layer profiles in S15-S17."
- **Investors as wedge profile** — CEO + BM-advisor independently echoed business-manager's team memo.
- **Postgres-shadow ledger before chain** — CEO + CTO independently echoed web3-engineer's team memo.

This is the strongest possible validation: the model produced the same conclusions the team produced, on independent prompts.

### Net-new ideas the advisor panel surfaced

1. **CTO — outbox pattern for atomic Postgres→EAS writes.** Folds into S15-IDENTITY-01 (web3) + S15-DATA-01 (data). Without it, when chain mirror lights up in Sprint 17, drift between Postgres ledger and EAS attestations is possible. The outbox table is a small schema add now (S15) that prevents a multi-day reconciliation in S17.

2. **PM — "bull-bear pair picker" in request flow.** Founders explicitly select two opposing profiles (e.g. one DeFi-bull investor + one DeFi-bear investor) at request time. Built-in adversarial framing at the input level, extending Gecko's debate one layer deeper than the 5-voice internal panel. Worth a Sprint 16 ticket.

### Conflict between panel and team

- **Pricing model for the contributor side.** Panel-BM proposed $99/mo or $499/mo subscription. Team-BM proposed per-call markup. Both might be right — they're testing different hypotheses on different sides of the marketplace:
  - Per-call markup = founders pay Gecko per validation, Gecko pays contributors per cite. Aligned with Gecko's existing no-subscription rule.
  - Subscription = contributors pay $99-499/mo for access to be discoverable + a paid-memo audience. Different ICP (the contributor), different revenue line.
  Resolution: **both ship, in sequence.** Sprint 15 = per-call markup (continuity with Gecko's pricing ladder). Sprint 17+ = test subscription on the *contributor* side as a separate revenue line.

### The dogfood-revealed gap (Gecko didn't catch what the team caught)

**Reputation-gaming was named as #1 anti-pattern by PD in the team synthesis. Neither basic tier nor the 5-voice advisor panel caught it.** Worse — basic tier *proposed* a public leaderboard in V3, exactly the pattern PD flagged.

This is a real finding: **Gecko's adversarial layer doesn't have "platform-design anti-pattern awareness."** The critic agent debates demand/supply/competition/cost/compliance — but doesn't critique the platform-design choices the verdict implies (gaming, echo chambers, reputation laundering, network capture).

**Sprint 15 add (NEW ticket — fold into ai-ml lane):**
- **S15-AIML-02 — `platform_anti_pattern` critique extension.** Add a critic-agent prompt suffix that explicitly checks for: leaderboard gaming, reputation laundering, sybil farming, echo-chamber selection bias, network-effect lock-in, fee-extraction monopoly, content moderation moral hazard. Fixture suite of 20 platform-design anti-patterns to grade against. Acceptance: re-running the same dogfood on the refined thesis produces a critique that names "public leaderboard" as a gaming risk.

This is the kind of self-improving dogfood the deeper-thesis flywheel was designed for: Gecko verdicts catch what Gecko verdicts missed, and the prompt library grows.

### Operational notes

- `staff_manager` (gpt-4.1-nano) failed closing-line detection AGAIN (Sprint 9 fix didn't fully cover this model — confirmed in `mcp-refinement-2026-04-30.md`). Worth a P1 ticket: either swap the model up (gpt-4o-mini) or extend the strict-suffix retry pattern to nano specifically.
- Stub mode worked end-to-end after migrations applied. No regressions from Sprint 13 schema changes.
- Total LLM cost across the dogfood (research + 5-voice plan): ~$0.016. The team review is more valuable per dollar by ~30x but the dogfood is *automatable* — it could run on every sprint thesis without human attention.

## Recommendations

1. **Fold the outbox pattern (CTO finding) into S15-IDENTITY-01 + S15-DATA-01.** Two-line schema add now prevents weeks of drift remediation later.
2. **Add S15-AIML-02 — platform_anti_pattern critique.** The dogfood-revealed gap is the highest-signal finding from this run.
3. **Park the subscription-vs-per-call BM tension as an explicit S17+ test.** Don't pre-commit either side; let Sprint 16 founding-contributor signals inform the call.
4. **Bull-bear pair picker (PM finding) is a Sprint 16 candidate.** Concrete UX extension; lands cleanly on the existing classifier + provider-router seam.
5. **Retire gpt-4.1-nano from staff_manager voice.** Recurring closing-line failures across Sprint 9, 11, 12, and now this dogfood. Swap to gpt-4o-mini or extend the retry shape model-specifically.
