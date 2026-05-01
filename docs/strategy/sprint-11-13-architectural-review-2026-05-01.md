# Sprint 11–13 architectural review

**Date:** 2026-05-01
**Author:** staff-engineer
**Inputs:** `docs/build-plan-sprint-11.md`, `docs/build-plan-sprint-12.md`, `docs/build-plan-sprint-13.md`, `docs/strategy/roadmap-sprint-13-to-17-synthesis-2026-04-30.md`, `docs/strategy/bazaar-deeper-thesis-2026-04-30.md`, `docs/diagnostics/2026-05-01-cdp-settle-failure-rca.md`

---

## 1. What landed cleanly

**Sprint 11 — verdict unification + thesis-aligned landing.** Track A (`derive_verdict` in `synthesizer.py`) collapsed the parallel `gap_classification` / advisor-consensus / landing-word universes into a single `KILL | REFINE | BUILD` token. This is the most consequential design decision of the three sprints because every downstream surface (Bazaar listing schemas, rubric v2, transcripts archive, pulse delta in S15) anchors on it. Tracks B and D landed cleanly. Track C (F18) closed root cause. Track F re-baselined under the new renderer.

**Sprint 12 — CDP Bazaar + parallel rails.** Track F (`SourceProvider` Protocol at `packages/gecko-core/src/gecko_core/ingestion/providers/__init__.py`) is the single highest-leverage piece of work in the three-sprint arc — it is what makes S13 vertical suites, S14 Paragraph, and the twit.sh integration plan below all 1-day adds rather than 5-day refactors. Tracks G (transcripts + rubric v2) and H (parallel-debate dispatch) shipped. Track I hardening landed before Track C smoke per gating discipline.

**Sprint 12 — what's still bleeding.** Track A (CDP settlement plumbing) chewed through ten fix iterations and still surfaced the Permit2-dispatch bug tonight (`docs/diagnostics/2026-05-01-cdp-settle-failure-rca.md`). The fix is one line (`assetTransferMethod: "eip3009"`), but the *meta-bug* is what the section below names. Three latent issues remain filed: short validity window, well-known omits `extra`, `pay_to` checksum.

**Sprint 13 — in flight tonight.** Phase primitive seam (Track B) and X402Client Protocol (Track C) are pure additive seams; if they land they're invisible to users and unblock four downstream lanes. DeFi Suite (Track A) is gate-conditional on S12 retro signals which we have not yet evaluated.

---

## 2. The recurring-pattern debt

Three patterns bit us repeatedly across 11–13. All three should be encoded in `CLAUDE.md` § "Recurring patterns" so S14+ doesn't repeat them.

### Pattern A — Parallel `Literal` redeclarations of the same concept

`PaymentMode` exists in at least four places: `gecko_core.payments`, `gecko_api.settings`, `gecko_core.sessions.store`, and the Supabase `CHECK` constraint. Each S12 fix flipped one and missed the others. Same shape on `NetworkKind` (Sprint 8 Solana variants vs Sprint 12 Base addition) and `Verdict` (Sprint 11 `KILL/REFINE/BUILD` vs legacy `ship/kill/pivot` in rubric v1).

**Encoding:** *Single source of truth for shared `Literal` types lives in `gecko_core.types`. Every consumer imports from there. SQL `CHECK` constraints reference a comment block that names the canonical Python enum. Adding a value requires touching exactly one file in Python plus one migration; nothing else.*

### Pattern B — Stubbed-but-shipped code that takes ~10 fix iterations

`CDPX402Client._build_payment_payload` shipped with placeholder defaults (no `assetTransferMethod`, no `extra` advertisement on the well-known, lowercase `pay_to`). Each was discovered by an actual settle attempt against mainnet at non-zero cost. The local diagnostic script that finally found the dispatch bug (`scripts/diagnose_cdp_settle.py`) was free and could have been written first.

**Encoding:** *For any new wire protocol integration: the first deliverable is an `eth_call`-style local simulation script that can falsify the implementation without spending money. Live smoke is the final verification, not the primary debug tool.*

### Pattern C — Tests that exercise stubs, not real wires

Sprint 12 test coverage on CDP passed in stub mode and on `/verify`. Settle failed because verify is a pure signature check — it doesn't exercise the dispatch branch the facilitator selects. The bug lived in code paths no test ever ran.

**Encoding:** *For payment-touching code, stub-mode tests are necessary but not sufficient. Each `X402Client` conformer ships with a contract-test module that runs against a recorded fixture of the real facilitator's settle response (vcr-style). Adding a new client is gated on the contract test passing.*

**Action:** open a short PR adding all three patterns to `CLAUDE.md` § "Recurring patterns" before Sprint 14 cuts tickets. Owner: staff-engineer. ~30 minutes.

---

## 3. The macro thesis check

The macro frame from `docs/strategy/bazaar-deeper-thesis-2026-04-30.md` is **"Gecko = trust layer of the agentic economy."** Operational version: *"Bazaar lets agents discover what to buy. Gecko tells them what to build."*

**Verdict: still right, but the V1 surface earns a narrower variant.** The full claim — verdicts as on-chain trust artifacts, agents subscribing to Gecko verdicts as filtration, sellers paying for review — is V3+ and explicitly hand-wavy on today's apex (per the deeper thesis doc § "What I'm NOT saying"). The Sprint 13 reality is more constrained: we have one rail listed (CDP Bazaar, modulo the live settle still being on the line as of tonight), one ICP shipping (founders), and zero on-chain commitment surfaces.

The sharper, V1-honest sub-frame: **"Gecko is the budget approver above x402 — adversarial validation as a pre-spend gate."** This is the Sprint 11 sub-fold, narrowed. It earns the right to the bigger claim only after S15 (Cloudflare lands → second rail) and S17 (fourth rail evaluated → triple sub-fold unifies into "above any x402 facilitator").

Don't change the apex landing today. Do gate the deeper-thesis claim in apex copy on the S17 unification milestone PD already named.

---

## 4. Sprint 14 readiness

The roadmap-sprint-13-to-17 synthesis names S14 as "first user-facing pulse + Paragraph + creator citation surfacing + pulse SKU at $0.50/call." Three readiness concerns:

1. **CDP latent issues from tonight's RCA.** Three filed but unfixed: short validity window (60s), well-known x402 omits `extra`, `pay_to` checksum normalization. None block S14, but all three will surface as Bazaar agent-traffic increases. **Recommend: slot a half-day "S14 Track 0 — CDP carry-over" before any pulse work fires.** Owner: web3-engineer.

2. **Sprint 13 in flight tonight means S14 acceptance gates depend on whether 13's Track B (phase primitive) actually lands.** If 13B slips, S14's `gecko_pulse` wires onto vapor. The synthesis correctly notes Tracks B/C/D are "non-negotiable" — but the gate evaluation is what makes that real. **Recommend: explicit S13 retro doc before any S14 ticket cuts, gating S14 on phase seam green.**

3. **Paragraph and twit.sh are the same shape under SourceProvider.** S14 plans `ParagraphProvider` as the first paid `SourceProvider` instance. Twit.sh today is eval-gate-only (see `packages/gecko-core/src/gecko_core/sources/twit_sh.py`); wrapping it as `TwitshProvider` is the same code shape and could be a +1-day add to S14 without expanding sprint scope. See Part 2 for the integration plan.

**S14 plan amendments:**

- Add Track 0 (CDP carry-over, half day, web3-engineer).
- Gate S14 ticket cuts on S13 retro green-light for Tracks B/C/D.
- Consider folding `TwitshProvider` into S14 alongside `ParagraphProvider` — both ride the same seam (see Part 2 for the argument and a concrete S14-TWITSH-01 ticket).
- No other shape changes; the synthesis is otherwise still right.

**Reversibility:** two-way. All three amendments are sequencing/scope, not interface or schema.
