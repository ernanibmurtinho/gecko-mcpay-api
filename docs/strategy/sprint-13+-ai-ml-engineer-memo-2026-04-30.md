# Sprint 13+ AI/ML engineer memo — 2026-04-30

**Lens:** prompts, personas, eval harness, RAG quality, verdict synthesis. The model is the system.

**Reading list consumed:** `docs/strategy/roadmap-vision-2026-04-30.md`, `docs/strategy/bazaar-deeper-thesis-2026-04-30.md`, `.claude/agents/ai-ml-engineer.md`.

---

## Theme 1 — Lifecycle monetization (research → plan → pulse)

**This is the highest-eval-debt theme.** Each phase grades a different question, so each needs its own persona set, fixture suite, and rubric.

1. **Sprint sequencing.** S13 = author phase-aware prompt families (research = current; plan = recurring; pulse = weekly delta). S14 = ship `gecko_plan` v2 prompts behind eval gate. S15 = `gecko_pulse` GA. The current Pro debate prompts in `packages/gecko-core/src/gecko_core/orchestration/pro/_default_prompts_v5_4.json` only grade pre-product theses — `analyst/critic/architect/scoper/judge` is wrong shape for "what should I ship this week." Critic in particular asks "is the demand real" — wrong question for a week-3 founder.
2. **Smallest shippable wedge (1-2 wk).** A `pulse_lite` prompt family (3 personas: progress-auditor, market-delta, scoper) + 8-fixture `pulse_holdout` suite + a verdict-shape extension `{CONTINUE, CHANGE_COURSE, KILL}` mapped onto KILL/REFINE/BUILD for rubric continuity. Run mocked-only in S13.
3. **Risks in lane.** (a) Persona dilution — if we reuse analyst/critic verbatim, pulse outputs collapse to "is the idea good" not "what to do this week." (b) Eval rubric in `tests/eval/rubric.py` grades against `expected_verdict` shaped for pre-product; will false-fail every pulse fixture until rubric v2 lands. (c) Fixture leakage — pulse fixtures must include simulated "weeks of context" or the model regresses to research-mode answers.
4. **Cross-lane deps.** data-engineer for session state schema (pulse needs prior-session context retrieval); software-engineer for new MCP surface; web3-engineer for per-call payment plumbing on the new SKUs.

**Scope estimate.** 3 phase prompts × ~5 personas × ~10 fixtures × rubric variant = ~120-150 net-new artifacts. Realistic 3-sprint program; S13 owns the research foundation only.

---

## Theme 2 — Paragraph connector (RAG-quality question)

Paragraph posts are **opinion-shaped, not data-shaped.** Today's RAG context-builder treats every chunk as roughly fungible (recency + similarity weighting). That breaks when an opinion piece outranks a GitHub repo on similarity score.

1. **Sprint sequencing.** S14 earliest — depends on data-engineer's SourceProvider Protocol seam (S12). Eval-side work can start S13 as a paper exercise: "what should source weighting look like?"
2. **Smallest wedge.** A `source_type` field on chunks (`opinion | data | code | discussion`) plus a context-builder weight table. Critic gets a new line in its system prompt: *"If a claim is grounded only in opinion-typed sources, mark it 'thinly sourced' and require corroboration from data/code/discussion."* Validate via 5 fixtures crafted to be opinion-rich vs opinion-poor and check verdict stability.
3. **Risks in lane.** (a) Critic over-firing — if we add the new critique without calibration, every Pro run flips to REFINE. Must run holdout and confirm verdict distribution doesn't skew. (b) Opinion sources contaminating the BUILD pile — judge prompt at `packages/gecko-core/src/gecko_core/orchestration/pro/_default_prompts_v5_4.json` already weights "evidence quality" but doesn't see source-type. (c) Citation grounding regressions — if opinion citations get same prominence in the rendered output, the verdict's perceived trust drops.
4. **Cross-lane deps.** data-engineer (source_type tagging at ingest), web3-engineer (creator-payment hop is upstream of RAG, so RAG must know which chunks triggered payment for receipts).

**Yes, the critic needs a new "opinion-dependency" critique.** Track as a v5.5 prompt revision.

---

## Theme 3 — Cloudflare x402 integration

Light eval lift — model behavior barely changes when the payment rail does.

1. **Sprint sequencing.** S15+. No prompt changes required for the consumer side; for the contributor side (Workers deploy), nothing in our lane until V3.
2. **Smallest wedge.** A "Cloudflare-gated source" flag on retrieved chunks; rubric extension: when a verdict cites a paid Cloudflare source, the receipt must round-trip through the eval harness. One fixture, one assertion.
3. **Risks in lane.** Mostly invisible. The one real risk: paid premium sources will be *much* higher signal than free sources, which can push the model toward over-confident BUILD verdicts. Need to monitor verdict distribution after enabling.
4. **Cross-lane deps.** web3-engineer (facilitator selection plumbing), data-engineer (source provenance metadata).

---

## Theme 4 — App-launching template ("x402-correctly-configured" eval)

The novel eval surface here is **a generated artifact.** We don't grade text quality; we grade whether the scaffold actually settles a payment.

1. **Sprint sequencing.** S16+ as a product. But S13 should ship the eval primitive: a "scaffold-validation" suite that runs against generated outputs.
2. **Smallest wedge.** Three layers stacked, fail-fast: (i) **schema check** — Bazaar metadata + frames.ag wallet config conform to JSON schema; (ii) **smoke test** — `gecko-mcp doctor`-style local x402 stub round-trip on the generated routes; (iii) **CI live settle on Base Sepolia** — gated, runs on tagged scaffold templates only (cost-discipline). All three are required to ship a template; (i) and (ii) for every PR, (iii) on weekly cadence.
4. **Cross-lane deps.** web3-engineer owns layer (iii) plumbing; software-engineer owns CLI scaffold; we own the rubric that says "this generated app is judgment-validated."

---

## Sprint 13 ticket — S13-AIML-01: Phase-aware persona library + pulse_holdout suite

**Why this one.** Unlocks Theme 1 directly and de-risks Themes 2 and 4 (rubric extensibility is the shared spine). Highest leverage.

**Scope (3-5 days).**
- Author `_default_prompts_v6_pulse.json` with 3 personas: `progress-auditor`, `market-delta`, `scoper-recurring`. Reuse `judge` from v5.4 with an extended rubric line for `{CONTINUE, CHANGE_COURSE, KILL}`.
- Add `tests/eval/suites/pulse_holdout.json` — 8 fixtures, each with `weeks_of_context` and `expected_verdict` in the new shape.
- Extend `tests/eval/rubric.py` with a `verdict_family` field; map pulse verdicts onto KILL/REFINE/BUILD for legacy continuity.
- Mocked-only run via `tests/eval/runner.py --suite pulse_holdout --reruns 2`. No paid eval gate this sprint.

**Acceptance criteria.**
- [ ] 3 new personas committed; v5.4 untouched.
- [ ] `pulse_holdout` mocked rubric pass rate ≥ 0.70 (informational baseline; not a gate).
- [ ] `verdict_family` field round-trips through `Verdict` model (`packages/gecko-core/src/gecko_core/models.py`) without breaking existing eval suites.
- [ ] No regression on `general_holdout` mocked run (≥ baseline in `tests/eval/baselines/general_baseline.json`).
- [ ] Memo entry in `docs/test-plan.md` defining the pulse rubric shape.

Word count: ~695.
