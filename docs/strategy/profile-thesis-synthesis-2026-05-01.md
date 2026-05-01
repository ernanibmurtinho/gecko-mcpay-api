# Profile-typed knowledge thesis — synthesis across 6 lenses

**Date:** 2026-05-01
**Inputs:** 6 lens memos (`docs/strategy/profile-thesis-{staff-engineer,ai-ml-engineer,web3-engineer,data-engineer,business-manager,product-designer}-2026-05-01.md`)

---

## Refined thesis (in one line)

**"Gecko is the orchestrator of profile-typed paid expertise. Knowledge gets commoditized via publish.new + on-chain identity. Gecko's wedge is the adversarial-debate verdict that synthesizes across investor / judge / PM / designer reputation — and that verdict's value is what no one else can replicate."**

The refinement vs the user's original thesis: **"orchestration + context" alone is not the wedge.** Per the AI/ML lens, that framing is also what Perplexity, ChatGPT, and Bazaar's own discovery claim. The defensible wedge is what Gecko already has: the **adversarial-debate output (KILL/REFINE/BUILD + grounded dissent)** signed by named, reputation-attestable contributors. Profiles add **inputs**, not the wedge itself.

## Where the 6 lenses converged

1. **Thesis holds — refine, don't reframe.** All 6 said yes with adjustments. No one rejected it.
2. **Profiles are an orthogonal axis to SourceProvider + X402Client.** Don't collapse them. Three protocols, three lifecycles (staff-eng, web3, data).
3. **Sprint 14 ships as planned.** No re-anchor. Sprint 14 Track B/C only adds a `contributor_address` column on chunks (one-line cost) to unblock Sprint 15+ profile work without rewrite.
4. **Sprint 15 = seam prep, Sprint 16 = wire orchestration, Sprint 17 = chain mirror.** Three-sprint arc, not one big-bang.
5. **Three-tier verification: self → observed → peer-quorum.** Only Tier-1+ counts in routing weight (web3, echoed by data's tag table).
6. **Bucketed reputation bands** (`emerging` / `established` / `senior`), never raw scores public. **No leaderboard.** (PD's anti-pattern check; web3 echoes via 180-day half-life.)

## Where the 6 lenses pushed back (the sharp signal)

### Pushback 1 — AI/ML: profiles are inputs, not the wedge

The most important refinement. "Orchestration + context" is also Perplexity's claim. The Gecko wedge is the **structured verdict + adversarial dissent**, both of which already exist. Profiles enrich the inputs (citing named investors with prior outcomes is more credible than citing anonymous URLs) but they don't create the moat. This means the apex landing line "profile-typed orchestration" earns its slot only after the verdict ships have proven repeat-rate.

**Implication:** don't market "orchestration" as the wedge. Market "verdict reliability per dollar" — and let profiles be the texture under that claim.

### Pushback 2 — BM: investors are the wedge profile, not judges

The user's thesis listed all profile types in parallel. BM picked one: **investors first.** Reasoning:
- Highest WTP for paid-memo content (Substack-paid-investor-newsletter precedent)
- Legible reputation (anyone can verify "Y Combinator partner")
- Existing paid-content behavior (no need to teach the on-chain payment mental model)
- Smallest cold-start cohort: $25k seed buys 50 hand-picked investors at $500 retainer

Sequence: **Sprint 15 founding-contributor program → 50 investors → flywheel signal → broaden to PMs (S17) → judges + designers (S18+).** Skip the "all profiles in parallel" trap.

### Pushback 3 — PD: reputation gaming is the #1 anti-pattern

If profiles see their score, they optimize for the score. Defense: **bucketed bands, outcome-tied deltas, no public leaderboard.** The landing copy line "profile-typed orchestration of paid expertise" earns its slot only after `min(profile_types_cited) >= 3` over 7 days — not before.

### Pushback 4 — Data + web3: don't bridge V1, don't collapse tables

- Single-chain identity (Base) is enough for V1; Solana payments stay separate; cross-chain bridge in S17+ (web3)
- Three new tables (`contributors`, `contributor_attributions`, `contributor_profile_tags`), not one unified index. Tag table beats fixed enum because profile type is multi-source. (Data)

## Sprint 15 plan — six convergent tickets

All six lenses produced a Sprint 15 ticket from their lane. They compose into a **seam-prep sprint** that doesn't touch user-facing surfaces:

| Ticket | Lane | Effort | Depends on |
|---|---|---|---|
| **S15-ARCH-01** ContributorRegistry Protocol stub + read-side only | staff-eng | 4d | — |
| **S15-DATA-01** registry + attribution tables + `match_chunks_by_profile` RPC | data-eng | 3-4d | S15-ARCH-01 schema decision |
| **S15-IDENTITY-01** Postgres reputation ledger (no chain writes) | web3-eng | 4d | S15-DATA-01 |
| **S15-AIML-01** profile-aware RAG scoring + 50-fixture `profile_rag` suite, flag-gated | ai-ml | 4d | S15-DATA-01 |
| **S15-BIZ-01** Founding Contributor Program spec (50 investors, $25k seed) | business-manager | 3-5d | independent |
| **S15-PROFILE-CITE-01** extend `Citation` with `profile_type` + `reputation_band` | product-designer | 3-4d | S15-DATA-01 |

**Total Sprint 15 effort:** ~22 days across 6 owners ≈ 3-4 day calendar sprint with overlap.

**Acceptance gate (sprint-level):** stub-mode `bb research --idea "..."` produces output **byte-identical** to pre-S15. Profile machinery is dormant until S16 wires the routing layer.

**Reversibility:** seam-prep is two-way except for `S15-DATA-01` (one-way schema migration); `S15-IDENTITY-01` is two-way until chain-writes start (S17+).

## Sprint 16-17 arc

- **Sprint 16:** wire profile classification into the orchestrator. AI/ML's `S15-AIML-01` scoring goes live behind a flag; PD's reveal-moment renderer ships. First five investors onboard via `gecko profile init`. ICP narrows: pre-seed founders raising in 90 days (per BM).
- **Sprint 17:** chain mirror — EAS attestations on Base. Apex landing earns "profile-typed orchestration of paid expertise" sub-fold (PD's gate: `min(profile_types_cited) >= 3` over 7 days). 6-month MRR target $35k mid-case (per BM).

## What the synthesis tells the user

1. **Your thesis holds.** Refine the framing: profiles are *inputs* to a verdict-quality wedge that already exists, not a new wedge.
2. **Pick one profile to launch with: investors.** Not parallel rollout. $25k seed program for 50 hand-picked.
3. **Sprint 14 stays as planned.** Sprint 15-17 is a three-sprint arc that wires this in incrementally without breaking pulse v1 / Paragraph / publish.new.
4. **Defend against reputation gaming from day one.** Bucketed bands, outcome-tied deltas, no leaderboard.
5. **Apex copy doesn't change today.** Earned by signal: 3+ profile types cited consistently over 7 days.

## What gets committed to memory

Recurring pattern worth encoding in `CLAUDE.md`: **when a thesis adds an "orchestration" claim, ask the AI/ML lens whether that's defensible against Perplexity/ChatGPT.** The wedge is rarely orchestration; orchestration is table stakes. Find the actual moat (verdict shape, dissent quality, settlement layer, etc.) and lead with it.

## Six follow-up commitments

- [ ] business-manager identifies the 50 founding contributors (target: end of Sprint 14 retro)
- [ ] web3-engineer tests EAS-on-Base read flow before Sprint 16 wire-up
- [ ] data-engineer validates pgvector ivfflat at 1M chunks (synthetic load)
- [ ] product-designer mocks the `gecko profile init` flow (paper, no code)
- [ ] ai-ml-engineer assembles the 50-fixture `profile_rag` suite (50 fixtures × 6 profile types each)
- [ ] staff-engineer keeps `ContributorRegistry` boundary clean — no `X402Client` imports in the new module
