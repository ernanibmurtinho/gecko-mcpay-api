# Pitch-prep — staff-engineer lens (synthesis)

**Date:** 2026-05-01
**Inputs:** BM lens, ai-ml lens (this dir), `sprint-11-13-architectural-review-2026-05-01.md`, S14/S15 plans, dogfood `df14664b-c9d4-49f1-9ade-e437a7eb5499`, README post-`400bed4`.

---

## V1-honest framing — what's earned, what's not

Per the 2026-05-01 architectural review:

**Earned today (V1 facts):**
- **"Budget approver above x402"** — adversarial validation as pre-spend gate. Earned.
- One CDP Bazaar listing live (`/.well-known/x402`).
- Three rails wired through one X402Client Protocol seam (stub, frames.ag/Solana, CDP/Base).
- Five-voice AG2 GroupChat → unified verdict via `derive_verdict` (S11).
- 0.80 live-V1 holdout eval; 1.0 on general/crypto/saas; ~0.85 rubric v2.
- 151 test files; S12.5 test policy enforces single-source-of-truth on 6 `Literal` types.
- `gecko_pulse` v1 + 12-pack SKU shipped (S14-PULSE-01..04).
- Paragraph MCP ingest + creator payouts on cite (S14-PARA-01..02).
- publish.new opt-in artifact publishing (S14-PUB-01..02).

**Not yet earned (gate before promoting to headline):**
- "Trust layer of the agentic economy" — V3+; gated on S17 4-rail proof (frames.ag + CDP + Cloudflare + awal).
- "Profile-typed orchestration of paid expertise" — gated on PD's signal: `min(profile_types_cited) >= 3 over 7 days`.
- "Discrimination layer for the agentic economy" — V3+ macro frame.
- $35k MRR / chain-attested reputation — S17 milestones.

## The 3-sprint S15-S17 credibility check

Outsider-test: does the S15→S17 arc make sense without insider context?

- **S15 (seam-prep):** ContributorRegistry Protocol stub, Postgres reputation ledger (no chain writes), profile-aware RAG scoring (flag-gated, default off), 50-investor program spec. **Dormant** behind flags. Stub `bb research` byte-identical to pre-S15.
- **S16 (wire):** flip the flags, onboard the first 5 investors via `gecko profile init`, ship pulse delta renderer.
- **S17 (chain mirror):** EAS attestations on Base via outbox pattern, 4th rail (Cloudflare or awal) closed, apex copy graduates.

This is **legible**: each sprint produces a thing an outsider can verify, none of it is big-bang, and reversibility is two-way until S15-DATA-01 (one-way schema migration). Verdict: makes sense.

## The wedge sentence

> **Gecko is the pre-spend verdict every agent and founder should pay $0.10–$0.75 for before committing real money to a build. Five-voice adversarial debate, 0.80 holdout eval, signed KILL/REFINE/BUILD, on-chain receipt — listed in CDP Bazaar today.**

Concrete (price ladder + eval score), measurable (Bazaar listing is verifiable), and V1-honest (no "trust layer" promise).

## "What we ARE" / "What we are NOT" saying

| What we ARE saying (V1 fact) | What we are NOT saying (V3 promise) |
|---|---|
| Budget approver above x402 — pre-spend verdict | Trust layer of the agentic economy |
| Five-voice adversarial debate, single signed verdict | Profile-typed orchestration of paid expertise |
| Live on CDP Bazaar at `/.well-known/x402` | Verdicts are on-chain commitment surfaces |
| 0.80 live-V1 holdout, 1.0 on general/crypto/saas | Smart contracts can gate execution on Gecko verdicts |
| Wallet-neutral seam: stub / frames.ag / CDP today | 4-rail proof closed (Cloudflare + awal still pending S15/S17) |

## The dogfood-revealed gap (transparency slide)

Stub-mode dogfood (`df14664b-c9d4-49f1-9ade-e437a7eb5499`) returned **REFINE / Gap: Partial:UX** on our own macro thesis. The 5-voice panel converged with the 6-lens team synthesis on most points but **missed reputation-gaming** as a platform-design anti-pattern (per `profile-thesis-dogfood-2026-05-01.md`). Fix: S15-AIML-02 ships a `platform_anti_pattern` critique extension. **Use this on the deck — it's a credibility win, not a weakness.**

## Three sequencing rules for feedback-collection week

1. **Don't pitch the V3 framing.** If a listener anchors on "trust layer," redirect to the V1 wedge ("budget approver above x402") and the milestone gate (S17).
2. **Lead with eval gate numbers**, not architecture. Numbers convert; architecture confuses non-technicals.
3. **Use the dogfood slide for honesty signal**, not as evidence of completeness. "Gecko caught itself missing X; fix lands next sprint" is a stronger pitch than "Gecko caught everything."
