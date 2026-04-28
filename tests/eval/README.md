# Pro tier eval harness (S1-06)

Deterministic 20-idea quality benchmark for the 5-agent Pro debate.

## Quickstart

```bash
# Mock mode — default. $0, no network. Used by CI.
uv run python -m tests.eval.runner

# Live mode — real OpenRouter/OpenAI calls. ~$3-5 at default reruns=1.
uv run python -m tests.eval.runner --live

# 3 reruns per idea, ~$10-15 total
uv run python -m tests.eval.runner --live --reruns 3

# Filter to one idea
uv run python -m tests.eval.runner --idea bad-uber-for-dogwalkers

# Diff against a saved baseline (non-zero exit on >15% regression)
uv run python -m tests.eval.runner --baseline tests/eval/baselines/2026-04-29-mock.json
```

## Cost ceiling

| Mode | Reruns | Approx cost |
|---|---|---|
| `mock` (default) | any | $0.00 |
| `--live` | 1 | $3-5 |
| `--live --reruns 3` | 3 | $10-15 |

Live cost is per-run — every full pass calls all 5 agents on all 20 ideas
(plus the GPT-4o judge once per idea). The harness will refuse to run
`--live` without `OPENAI_API_KEY` set.

## The 4 axes

Each scored 0.0-10.0:

1. **agent_voice** — does each of the 5 agents stay in their persona?
   (analyst surveys market, critic is adversarial, architect picks stack,
   scoper carves V1/V2/V3, judge calls it)
2. **source_grounding** — are claims tied to retrieved sources, or hand-wavy?
3. **verdict_justification** — does the judge's verdict follow from the
   debate, or contradict it?
4. **cost_predictability** — is the per-turn token usage within ±20% of
   the cohort median? (mock-mode semantic) / does any agent dominate
   >40% of tokens? (live-mode semantic — known divergence; both signal
   the same kind of imbalance)

## How mock mode works

- `tests/eval/mocks.py` holds canned 5-turn transcripts per idea_id.
- `tests/eval/rubric.py::score_transcript_mock` is a deterministic scorer
  that keys off lexical markers (citation patterns, persona vocab,
  judge verdict text). No LLM call.
- The mocks are crafted so kill ideas score lower on grounding and ship
  ideas score higher — this lets CI assert the rubric is calibrated, not
  just that the judge happened to agree with itself.

If you change a canned transcript and the deterministic scores shift,
the harness will regenerate the baseline; commit the new baseline JSON
alongside the mocks change.

## How live mode works

- Calls `gecko_core.orchestration.pro.generate(...)` directly. No `gecko-api`,
  no Supabase, no session_costs persistence. The harness owns its own JSON.
- Rubric uses GPT-4o (`gpt-4o-2024-11-20` pinned) at temp=0 with
  `response_format={"type": "json_object"}`.
- Token costs estimated at OpenRouter passthrough rates for `gpt-4o-mini`.
  Actual numbers come back via AG2's client usage summary.

## Baselines

Saved as `tests/eval/baselines/<date>[-mock].json`. Each PR run with
`--baseline` flag fails on >15% regression on any of:

- `verdict_accuracy` (going down is bad)
- `median_score` (going down is bad)
- `median_cost_usd` (going up is bad)

## CI

`mock` mode runs on every PR via `.github/workflows/eval.yml`. Live runs
are manual; trigger from the Actions tab when you've meaningfully edited
`packages/gecko-core/src/gecko_core/orchestration/pro/agents.py` or the
routing matrix.

## Known divergences from the staff-engineer roadmap spec

- Spec said "cost_variance" — implemented as `cost_predictability` for
  clarity (variance is a stat, predictability is the user-facing notion).
- Spec implied a single judge model for both mock and live. Mock uses a
  deterministic lexical scorer; this is a deliberate departure so CI
  catches *rubric* regressions, not just transcript regressions.
- `pivot` is treated as kill-equivalent for `verdict_accuracy` — not in
  the spec but matches how the judge often softens.
