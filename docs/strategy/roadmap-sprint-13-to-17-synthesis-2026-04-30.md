# Roadmap synthesis — Sprint 13 → 17

**Date:** 2026-04-30
**Inputs:** 7 specialist memos (`docs/strategy/sprint-13+-<role>-memo-2026-04-30.md`) + roadmap vision + bazaar deeper thesis
**Purpose:** turn 7 disagreeing-but-overlapping lens memos into one committable multi-sprint plan.

---

## What all 7 memos agree on

1. **Theme 1 (lifecycle) is the right next bet.** All 7 lanes named lifecycle work in their Sprint 13 tickets — even when their headline call differed.
2. **Theme 4 (app-launching template) needs a positioning decision before any code.** Staff-eng named it explicitly; software said "generator not hosted, no SDK runtime"; BM said "needs new ICP — app builders"; PD said "deferred to S16+ at earliest." The thesis-level call comes first, not the build.
3. **Theme 3 (Cloudflare) is the lowest-risk add.** Consumer-side only. One provider class behind the SourceProvider Protocol seam from S12. Edge-deploy of gecko-api itself is V3+, not in this arc.
4. **Pre-paying seams beats refactoring later.** Staff-eng pre-payment recommendation (`SessionPhase` enum) was independently rediscovered by software (`parent_session_id + phase`), data (`captured_at + project_id`), web3 (`X402Client Protocol with supported_networks`), and ai-ml (`phase` on rubric v2). One coherent S13 seam unlocks four lanes.
5. **Wallet/facilitator neutrality holds.** No agent recommended migrating off frames.ag for Solana. CDP is parallel, Cloudflare is parallel, Paragraph is upstream-payment.
6. **Subscription stays out.** BM held the line: pulse is per-call ($0.50) with optional 12-pack prepay as a commitment device, not auto-renew.

## Where they tense (and how to resolve)

### Tension 1 — Does Sprint 13 lead with DeFi Suite or with lifecycle pulse?

**Staff-eng:** lifecycle leads S13 unless the S12 retro gate opens, in which case DeFi Suite + lifecycle prompt-set work in parallel.

**BM:** DeFi Suite leads S13 (gate-conditional), lifecycle goes S14.

**Resolution:** these are compatible. The disagreement is framing, not substance.
- **If S12 retro gate opens** (Bazaar traffic + ≥1 quality DeFi provider): S13 ships **DeFi Suite (BM's wedge) + the phase primitive seam (engineering pre-payment)**. Different surfaces, different lanes, no collision. The seam doesn't require a user-facing pulse to ship.
- **If gate fails:** S13 = phase primitive seam alone, all engineering effort goes to the seam + first pulse fixtures. No new SKU.

Either way, **the phase primitive seam ships in S13.** It's the only thing 4 of 5 engineering lanes converged on independently.

### Tension 2 — When does `gecko_pulse` user-facing surface ship?

**PD:** S15+ ("deferred until delta renderer exists" — pulse without showing what changed since last pulse is meaningless UX).
**BM:** S14 (gecko_pulse priced and shipping).
**ai-ml:** S14 (pulse_holdout fixtures + during_build prompts).
**software:** S13/S14 (parent_session_id wiring).

**Resolution:** split the pulse work across two sprints, not one.
- **S13:** phase primitive seam (engine layer). No user-facing pulse yet. 100% backend.
- **S14:** pulse engine v1 with a *basic* user-facing surface (single panel, no delta). PD ships first-pass renderer; ai-ml ships fixtures + prompts; BM ships SKU pricing.
- **S15:** pulse delta renderer (PD's "show what changed since last week"). This is the surface that earns the $0.50/call.

This honors PD's "no half-baked UX" principle while letting BM/ai-ml/software ship in parallel.

### Tension 3 — Is Paragraph a sub-fold or a receipt element?

**PD:** receipt element ("Creator payouts" line + footer attribution), NOT a sub-fold.
**Staff-eng:** "first instance of the upstream-monetization pattern." Strategic but not landing-copy-bearing on day one.
**BM:** marketing/content acquisition cost; -$0.06 COGS; the value is verdict trust, not direct revenue.

**Resolution:** PD wins for V1 surface; BM frames the COGS narrative; staff-eng's strategic framing goes in `docs/strategy/option-set.md` for future thesis evolution. Paragraph as sub-fold is earned only when there are 3+ creator-monetization integrations to point at.

### Tension 4 — Should the eval rubric v2 work happen in S12 or S13?

Currently in **S12 Track G**. ai-ml-engineer's S13 ticket extends rubric v2 with a `verdict_family` field for phase-aware scoring. Software's S13 ticket extends `Session` schema. These two need rubric v2 *finished* in S12 to land cleanly in S13.

**Resolution:** S12 Track G stays in S12. If it slips, S13 phase work delays one sprint. Don't double-budget the rubric work.

---

## The committed Sprint 13 plan

### Conditional fork at S12 retro

**At S12 retro, evaluate two signals:**
1. Did ≥1 Bazaar agent call land at gecko-api in the 7-14 days post-listing?
2. Have we identified ≥1 DeFi-relevant Bazaar provider with ≥100 calls/30d AND clean schema?

### S13 if BOTH signals pass — "Suite + Seam" sprint

**Track A — DeFi Vertical Suite at $9** (BM lead)
- Vertical-specific critic prompts (not just bundled feeds — that's what makes this a moat, not a SKU)
- 3-5 named Bazaar providers integrated as `BazaarProvider` instances
- Auto-detect via classifier, `--vertical defi` override
- Vertical-themed spinner ("Pulling DeFi market signals...")
- Receipt: single `Bazaar-routed sources $0.20` line + indented `└─ vertical: defi (4 providers)`
- **Owner:** ai-ml-engineer (prompts) + software-engineer (orchestration) + product-designer (CLI surface)

**Track B — Phase Primitive Seam** (staff-eng pre-payment)
- `SessionPhase = Literal["pre_product", "during_build", "ongoing"]` on `Session`
- `parent_session_id` FK for pulse linkage (per software-engineer)
- Migration `chunks.captured_at` + `chunks.project_id` + `match_chunks_windowed` RPC (per data-engineer)
- Phase-aware fixture loader: `tests/eval/fixtures/{phase}/{vertical}/`
- Rubric v2 row schema gains nullable `phase` field
- **Owner:** staff-engineer (co-design) + data-engineer (migration) + software-engineer (Session schema) + ai-ml-engineer (fixture relabel structure, no prompts yet)
- **No user-facing pulse this sprint.** The seam is the deliverable.

**Track C — `X402Client` Protocol formalization** (web3 pre-payment)
- Narrow Protocol with `supported_networks` + `facilitator_id` properties
- `resolve_client(intent)` factory keyed on network
- Reserve `http-cloudflare` slot raising explicit `NotImplementedError("Sprint 15")`
- **Owner:** web3-engineer

**Track D — Citation creator attribution** (PD pre-payment for Paragraph)
- Extend `Citation` rendering in `apps/cli/src/gecko_cli/render.py` lines 91-107
- Optional `creator_handle` + `creator_payout_usd` fields (default null)
- Footer block when any citation has creator attribution
- **Owner:** product-designer + data-engineer (schema for `source_creators` sibling table per their memo)

**Acceptance:** Suite ships with verdict_accuracy ≥ 0.85 on a new `defi_holdout` fixture; phase seams land with `pre_product` defaults preserving every existing behavior; eval gate passes.

### S13 if EITHER signal fails — "Seam-Only" sprint

Tracks B + C + D ship as above. Track A becomes:

**Track A' — Vector 1 doubling-down**
- Sharpen Bazaar listing metadata (better descriptions, schema completeness)
- Add 2-3 more discoverable routes if surface allows
- Better Claude Code skill distribution (revisit the skill repo, polish the install path)
- **Owner:** software-engineer + product-designer + business-manager

Either way, Tracks B/C/D are non-negotiable.

---

## S14 — first user-facing pulse + Paragraph

- **gecko_pulse v1 engine + basic surface** (single panel, no delta yet) — uses S13's phase primitive
- **Paragraph connector** as first `PayingProvider(SourceProvider)` instance (per software's memo)
- **Creator citation surfacing** in CLI + receipt (Track D from S13 reused)
- **Pulse SKU live at $0.50/call** with 12-pack prepay option

## S15 — pulse delta + Cloudflare consumer-side

- **Pulse delta renderer** (PD's blocking ask): show what changed since last pulse, what new evidence, what shifted in verdict
- **Cloudflare x402 consumer** as a `BazaarProvider`-adjacent client
- **Honor `http-cloudflare` slot** in X402Client factory (drop the `NotImplementedError`)

## S16 — gated decisions

Two gates open at S15 retro:
1. **Pulse repeat-rate:** is gecko_pulse generating recurring usage (≥30% of original validators returning by week 4)? If yes, pulse becomes core; if no, demote to optional surface.
2. **App-launching template positioning:** does the deeper thesis still hold after 4 sprints of evidence, OR has the agent economy shape changed enough that "we ship trustworthy agents" is the next move?

If pulse-yes + thesis-holds → S16 = first **vertical pulse pack** (DeFi pulse for ongoing monitoring, e.g.).
If pulse-yes + scaffold-yes → S16 = **scaffold v0 generator** (per software-engineer's memo: `packages/gecko-launcher`, no SDK runtime, vendored gate per scaffold).

## S17 — sub-fold unification (if all rails proven)

PD's commit: triple sub-fold ("above frames.ag" + "above Bazaar" + "above Cloudflare") earns unification ("above any x402 facilitator") **only at four rails**. Sprint 17 is when we evaluate adding a fourth rail (e.g. Coinbase Agentic Wallet `awal` integration if it's gained traction by then).

---

## Cross-sprint deferred / strategic option set

These exist but are explicitly NOT committed:

- **App-launching template** — needs a thesis-level decision in a dedicated round before any sprint slot.
- **Marketplace cut on hosted-scaffold apps** — honor-system + opt-in relayer per web3-engineer; no protocol-level enforcement until x402 v3 (if ever).
- **Gecko-as-orchestrator (Vector 4 from earlier memos)** — $29 base + 1.5x markup. Post-S16 candidate, gated on vertical pulse pack traction.
- **"Validated by Gecko" certification (Vector 5)** — far future, requires brand strength.
- **Edge-deployed gecko-api on Cloudflare Workers** — V3+. Loses AutoGen GroupChat. Probably never unless we rebuild orchestration.

---

## What this means for the user

**Sprint 12 retro decides whether S13 ships DeFi Suite or seam-only.** Either way the seam ships.

**Sprint 14 is when monetization expands** — pulse SKU + Paragraph as creator-monetization wedge. This is the most consequential sprint for revenue.

**Sprint 15 is when "facilitator neutrality" earns its keep** — Cloudflare lands, dual sub-fold becomes triple sub-fold.

**Sprint 16 is when we decide whether to be a scaffolding company.** Default no. Defer to a deeper-thesis follow-up doc before reconsidering.

**Sprint 17 is when the macro positioning ("trust layer of the agentic economy") earns the right to claim it on the apex landing.** Today's claim is one-rail; that round earns four.

The **biggest commercial risk** (per BM): ICP fragmentation across the 4 themes — "shipping all four at 60% quality" instead of one at 95%. The mitigation gate every theme must pass: *"does this make founders trust the verdict more?"* If the answer is no, defer.

The **biggest architectural risk** (per staff-eng): the scaffold registry surface — if we ship theme 4 without first deciding whether the scaffold metadata + curation lives in `gecko-core` (Python), `gecko-mcpay-skills` (markdown), or a separate `gecko-mcpay-templates` repo (cross-repo contract), we get a maintenance burden visible only in two sprints. **Don't ship theme 4 until that lives somewhere.**

Six-month MRR target (BM): ~$8k mid-case. Floor $1k if S13 gate fails; ceiling $11k+ if scaffold lands.
