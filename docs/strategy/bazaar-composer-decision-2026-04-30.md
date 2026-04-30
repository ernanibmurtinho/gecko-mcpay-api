# Bazaar-as-composer — synthesized decision + Sprint 12/13 commitments

**Date:** 2026-04-30
**Inputs:**
- `docs/strategy/bazaar-composer-staff-eng-review-2026-04-30.md` (architecture)
- `docs/strategy/bazaar-composer-business-review-2026-04-30.md` (GTM + pricing)
- `docs/strategy/bazaar-composer-design-review-2026-04-30.md` (UX)
- `docs/research/bazaar-as-composer-2026-04-30.md` (5-vector proposal)
- `docs/research/cdp-bazaar-2026-04-30.md` (landscape probe)

---

## The synthesized call

The three specialists converge on one shape with one productive tension. Both are resolvable cleanly.

### Where they agree (lock these in)

1. **Sprint 12 stays focused on Vector 1 (listing).** Staff-eng + BM + PD all agree: don't expand sprint scope. List Gecko in Bazaar; settle one Base mainnet payment through CDP; verify discovery; ship.

2. **The thesis sub-fold extends, doesn't dilute, under composer framing** — *if* we hold the line on what stays first-party. Staff-eng named the non-composable core: verdict synthesis, adversarial debate, memory/flywheel, classifier+router. PD agreed by recommending dual sub-folds rather than unifying ("above frames.ag" + "above the Bazaar, too").

3. **Latency is the binding constraint, not provider transparency.** PD's strongest call. Pro+ latency budget = Pro's budget. Parallel fan-out + per-provider 6s hard cap. Critic agent flags missing data as a real critique (turns degradation into a feature). Staff-eng's three levers (parallel + cache + circuit-breaker) match PD's mandate.

4. **Wallet neutrality stays.** No agent advocated coupling to Coinbase Agentic Wallet. frames.ag relationship preserved.

### Where they tense (resolve here)

**Staff-eng:** "Defer Vectors 3-5 entirely until Bazaar traffic > 0. Don't commit vertical suites in Sprint 13 — re-evaluate based on post-listing signals."

**BM:** "Sprint 13 wedge = DeFi vertical suite at $9 (Vector 3). Skip horizontal Pro+ (Vector 2). Suites are a moat; SKUs are a tier."

These look opposed. They're not. Staff-eng is asking for a **gate condition**; BM is naming the **right product if the gate opens.** Combine:

> **Sprint 13 commitment is conditional.** If by Sprint 12 retro we have (a) ≥1 Bazaar agent call land at gecko-api AND (b) ≥1 named DeFi-relevant provider on Bazaar with ≥100 calls/30d AND clean schema, we fire BM's DeFi vertical suite at $9 directly — skipping the generic Pro+ that staff-eng was right to be skeptical of. If either signal is absent, Sprint 13 stays in Vector 1 territory: better Bazaar metadata, better quality-rank signals, better Claude Code skill distribution. Re-evaluate at Sprint 13 retro.

This buys:
- Staff-eng's risk control (don't refactor for vapor)
- BM's wedge (skip the cannibalizing $1.50 mid-tier; go straight to a moat-shaped product)
- A measurable decision rule we can hold ourselves to

### The architectural pre-payment (non-negotiable)

Staff-eng's strongest tactical call: **pre-pay the `SourceProvider` Protocol seam in Sprint 12, even though we won't use it yet.** ~1 day's work. Saves 3-5 days + eval-gate breakage risk in Sprint 13 if we go.

- Add `gecko_core/ingestion/providers/__init__.py` with the Protocol.
- Refactor `discover()` + `pipeline.ingest()` to dispatch through it.
- Existing Tavily/web/youtube paths become the default `FreeProvider`.
- Add a `Provenance` field to `Citation` so the verdict-renderer can show evidence source without changing the verdict shape.
- **No new provider implementations this sprint.** The seam is the deliverable.

This is the only Sprint 12 scope addition. Everything else stays as planned.

### Pricing ladder (locked in for now)

Per BM: **Free / $0.10 (Basic) / $0.75 (Pro) / $9 (DeFi suite, conditional Sprint 13) / $12-19 (other verticals, Sprint 14+) / $29 (orchestrator, Sprint 16+).**

Skip the $1.50 mid-tier entirely. The Pro→Suite gap is a product jump (vertical-specific critic prompts + bundled providers), not a tier jump.

### UX commitments (locked in for if/when verticals ship)

Per PD:
- Spinner stays vertical-themed ("Pulling DeFi market signals..."), never provider-themed
- Receipt: single `Bazaar-routed sources ........ $0.20` line + indented `└─ vertical: defi (4 providers)`. Provider names hidden behind `--show-providers`
- Verticals auto-detected via existing classifier; `--vertical defi` is an override
- Pro+ latency budget capped at Pro's budget (parallel fan-out mandated)
- Sub-fold doubles: keep "above frames.ag," add parallel "above the Bazaar, too" only after both have concrete proof

---

## Sprint 12 — amended scope

**Original five tracks** (in `docs/build-plan-sprint-12.md`) all stand. **One addition:**

### Track F (NEW) — `SourceProvider` Protocol seam (S12-PROVIDER-01)

**Owner:** software-engineer
**Cost:** ~1 day
**Scope:** Define the Protocol; refactor existing dispatch; introduce `Provenance` on `Citation`. No new providers. Tests confirm existing eval-gate suites still pass under the new dispatch.

This is the only architectural debt we pre-pay. Everything else (vertical suites, Pro+ tier, orchestrator) stays in the option set.

---

## Sprint 13 — conditional draft

**If gate passes (post-Sprint-12 retro):**
- DeFi vertical suite at $9
- 1-3 named Bazaar providers integrated as `BazaarProvider` instances
- Vertical-themed spinner + receipt anatomy (per PD)
- Latency budget enforcement
- Category-specific critic prompts (per BM — the moat, not just bundled feeds)

**If gate fails:**
- Stay on Vector 1: better Bazaar metadata for ranking, better Claude Code skill distribution, focus on quality-rank compounding
- Don't refactor for vapor

Sprint 13 build plan gets written *after* Sprint 12 retro, not before. Avoid the trap of pre-committing to a wedge that data may not support.

---

## What's deferred (option set, not committed)

- **Vector 4 — Gecko-as-orchestrator** — $29 base + 1.5x markup. No subscription. Post-Sprint-16 candidate, gated on vertical suite traction.
- **Vector 5 — "Validated by Gecko" certification** — far-future, requires brand strength we don't have.
- **Migration off frames.ag for Solana** — never (unless frames.ag posture changes). frames.ag stays for Solana; CDP is parallel for Base.
- **Bazaar-as-source for free tier** — no margin in it; only bundle paid sources for paid tiers.

---

## Decision rule for the next 3 retros

- **Sprint 12 retro:** does Bazaar listing show traffic? (signal 1)
- **Sprint 12 retro:** is there ≥1 quality DeFi-relevant Bazaar provider? (signal 2)
- **Sprint 13 retro (if fired):** does the DeFi suite hit BM's success metrics on the live-V1 eval (target verdict_accuracy ≥ 0.85 across 5+ DeFi ideas)? (signal 3)

Each "yes" advances. Each "no" holds. No commitment beyond the next sprint until signals cross.

---

## What I won't ship without asking

Per `feedback_destructive_actions.md` (memory rule): the next paid-call eval gate run (Track F of Sprint 11, ~$3-5) requires explicit user OK. I miscalibrated the scope of `run_eval_gate.sh` earlier; not stacking another auto-fire. Tell me when to fire `run_eval_gate_live.sh` to confirm the new verdict renderer holds the 0.80 threshold.
