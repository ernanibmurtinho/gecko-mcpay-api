# ADR-0001: Mainnet cutover gated on V1 sources eval pass

**Status:** Accepted
**Date:** 2026-04-28
**Author:** staff-engineer
**Deciders:** Ernani (founder), staff-engineer
**Supersedes:** §2 of `docs/roadmap-post-grant.md` (mainnet-cutover-as-Sprint-2-priority)

## Context

By 2026-04-28 the Gecko platform has:
- Pro tier ($0.75 USDC, AG2 5-agent debate) shipped on Solana **devnet**.
- Mainnet cutover code path complete: CDP facilitator, X402_NETWORK switch, mainnet runbook (`docs/runbooks/mainnet-cutover.md`).
- CDP onboarding in progress on the user side.
- Pro tier verdict accuracy at **0.85** across the current 20-idea eval suite (post-prompts-v4).
- V1 source rollout queued (Sprint 2 Final, 13 tickets): Colosseum, internal flywheel, HN, Reddit, GitHub, twit.sh + embedding-NN classifier + expanded eval (50 ideas across crypto/saas/general sub-suites).

Two sequencing options:

**(A)** Cut over to mainnet first (this week), then ship V1 sources to a live mainnet service.
**(B)** Land V1 sources first, eval-validate verdict accuracy ≥ 0.85 on each sub-suite, then cut over to mainnet.

## Decision

**Option (B). Mainnet cutover triggers immediately after `S2X-15` (eval gate) passes.**

## Consequences

**Positive:**
- First paying-user cohort sees Pro+V1 quality (verdict accuracy validated against expanded sub-suites), not pre-V1 quality.
- The eval gate (`S2X-15`) becomes the load-bearing quality bar for mainnet eligibility — every future feature must clear it.
- Reputational risk on launch is bounded. Gecko's whole pitch is "we kill bad ideas accurately"; shipping pre-V1 verdicts to real-money users contradicts that.
- Compresses no schedule: V1 sources land in Sprint 2 Final (~4-5 days); mainnet cutover is ~1 day immediately after.

**Negative:**
- Monday demo runs on devnet x402 instead of mainnet. The "mainnet" narrative gets pushed to Tue/Wed of Sprint 3 week 1.
- If S2X-15 fails (any sub-suite < 0.85), mainnet slips by however long prompt rework takes (typically 1-2 days based on tonight's prompts-v4 iteration cadence).

**Neutral:**
- The cutover runbook (`docs/runbooks/mainnet-cutover.md`) and CDP onboarding both stand ready; this decision only re-orders timing, not the work itself.

## Alternatives considered

**Option (A) — cutover first.** Rejected. The product narrative depends on verdict quality, and the only signal we have on V1 source impact comes from the expanded eval. Cutting over before that signal lands means staking "Solana mainnet" launch positioning on agent quality we can't yet defend.

**Hybrid — cutover for Monday demo, then iterate on mainnet.** Rejected. ECS task swap is inexpensive but the demo slot is a one-shot reputational moment. Better to demo on devnet (which is already the existing public surface) and announce mainnet GA once V1 lands.

## Confirmation gates

- `S2X-15` passes: each of crypto / saas / general sub-suites ≥ 0.85 verdict accuracy.
- CDP credentials populated in SSM (`/gecko/prod/CDP_API_KEY_ID`, `/gecko/prod/CDP_API_KEY_PRIVATE_KEY`).
- Mainnet keypair generated, $20 USDC float funded (per runbook §1).
- web3-engineer executes runbook §2 (cutover) within 24h of S2X-15 pass.

## References

- `docs/build-plan-v2-and-beyond.md` §1, §2
- `docs/runbooks/mainnet-cutover.md`
- `docs/v1-build-plan-external-sources.md`
- `tests/eval/baselines/2026-04-28-4.json` (current 0.85 baseline; pre-V1 sources)
