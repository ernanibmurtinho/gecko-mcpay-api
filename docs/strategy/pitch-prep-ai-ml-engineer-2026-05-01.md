# Pitch-prep — ai-ml-engineer lens

**Date:** 2026-05-01
**Inputs:** stub-mode dogfood (`df14664b-c9d4-49f1-9ade-e437a7eb5499`), `profile-thesis-ai-ml-engineer-2026-05-01.md`, eval-gate scripts (`run_eval_gate_live.sh`), `tests/eval/fixtures/*`.

---

## What's technically defensible vs hand-wavy

### Defensible (cite on stage)

**1. Verdict reliability — concrete eval gate scores:**
- **`live-V1 holdout` ≥ 0.80** under rubric v2 (S12 baseline, carry-forward acceptance in S13 + S14)
- **`general` / `crypto` / `saas` holdouts at 1.0** (perfect on canonical fixtures)
- **`rubric v2` aggregate ~0.85** across stub-mode runs
- These are real, reproducible numbers from `run_eval_gate_live.sh` — not vibes.

**2. Adversarial debate fidelity (5-voice AG2 GroupChat):**
- 5 voices: CEO, CTO, PM, Staff, Designer — each a separate model invocation, then judge synthesizer collapses to KILL/REFINE/BUILD via `derive_verdict` (S11-VERDICT-01).
- Models: voice-by-voice in the curated catalog (`tier-preset balanced` → Kimi K2.6 / DeepSeek; `quality` upgrades to gpt-4o; `budget` falls to nano-class).
- Advisor consensus shape is a **single signed token** (KILL/REFINE/BUILD) plus typed `gap_classification` — not a free-form summary. This is the moat against Perplexity: Perplexity grades chat helpfulness; Gecko grades verdicts against expected outcomes on a holdout.

**3. Contract test policy (S12.5 + S14):**
- Single source of truth for `PaymentMode` (`gecko_core.payments.modes`)
- Schema-drift gate on 6 shared `Literal` types (S14-TEST-POLICY-02)
- Universal X402Client contract test (S14-TEST-POLICY-01)
- Pre-deploy contract gate workflow (S14-TEST-POLICY-03)

### Hand-wavy (defer or gate)

- **Profile-aware RAG scoring** — coefficients eval-tuned, fixture suite (50×6) lands S15 (S15-AIML-01). **Don't claim live profile routing today.**
- **`platform_anti_pattern` critic extension** — S15-AIML-02; Gecko's own dogfood missed reputation-gaming. **Honest disclosure on stage:** the dogfood found this gap; the fix lands S15.
- **Bull-bear pair picker** — S16 candidate; not shipped.
- **Cross-chain reputation** — V3+, gated on S17 EAS-on-Base mirror.

## Specific results to cite vs defer

**Cite:**
- "Eval gate at 0.80 on live-V1 holdout, 1.0 on general/crypto/saas, ~0.85 rubric v2"
- "151 test files; 180/180 passing in S13 Track E baseline"
- "Stub-mode dogfood on our own thesis (session `df14664b…`) returned REFINE — model agreed with the team that orchestration alone is not the wedge"

**Defer:**
- Profile-typed RAG accuracy (S15 fixture suite incoming)
- Cross-model A/B numbers (we have logs, not deck-grade)
- Token-cost-per-verdict (we deliberately don't surface this — session pricing is the unit per CLAUDE.md)

## Perplexity defense (per dogfood-loop memory rule)

If pitch leans on "orchestration": **stop**. Per `feedback_dogfood_loop.md`, ai-ml MUST defend orchestration claims against Perplexity. The defense is *the verdict shape* (signed KILL/REFINE/BUILD + typed dissent), not the routing. Lead with verdict reliability per dollar.

## Three slides ai-ml must own

1. **Verdict reliability slide** — eval gate table (live-V1 0.80 / general 1.0 / saas 1.0 / crypto 1.0 / rubric v2 0.85).
2. **5-voice debate architecture** — diagram: 5 voices → judge → `derive_verdict` → signed token. Show model fan-out.
3. **Self-dogfood transparency slide** — "We pointed Gecko at our own thesis. Verdict: REFINE. It caught us. It also missed reputation-gaming — that fix is S15-AIML-02." Builds credibility through honesty.
