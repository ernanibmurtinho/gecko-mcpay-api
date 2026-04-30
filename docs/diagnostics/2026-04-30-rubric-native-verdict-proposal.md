# Proposal — Migrate Eval Rubric to KILL / REFINE / BUILD Natively

Author: ai-ml-engineer
Date: 2026-04-30
Status: PROPOSAL — schema + migration path only. No fixture relabel yet.
Companion: `docs/diagnostics/2026-04-30-verdict-regression-analysis.md`

## Why now

Sprint 11 Track A (`b1661a0`) replaced the founder-facing verdict surface
from legacy `ship | kill | pivot` to `KILL | REFINE | BUILD`. The pipeline
emits the new tokens (judge's Final-verdict line, `ResearchResult.verdict`,
CLI headline, MCP `gecko_research` payload). **The eval rubric does not.**
It still grades against legacy `ship/kill/pivot` strings, with a softening
rule that maps `pivot ↔ kill` for accuracy. The new `BUILD` token is
collapsed to `ship` in `extract_verdict` (`word.startswith("build")`); the
new `REFINE` token has no slot at all and is read as `pivot` only via
fallthrough lexical heuristics.

This is a measurement gap *and* a strategic loss:

- **Measurement gap.** A judge that emits `Final verdict: REFINE` after a
  `Verdict: SHIP V1 to ...` prose line is signaling "build a smaller scope,
  but build it." The rubric reads only the prose, sees SHIP, and grades it
  as `ship`. If the judge emits `Verdict: KILL` with `Final verdict: REFINE`
  (the prompt actively encourages this hardening on borderline narrow-ICP
  cases — see companion analysis §"Why Track A still nudges the prose"),
  the rubric grades it KILL. Either way, REFINE is invisible. The gate
  cannot tell when the model was right-and-soft vs right-and-hard.
- **Strategic loss.** "Build but scope it down" (REFINE) is genuinely a
  different outcome than "pivot to a different idea." The legacy taxonomy
  conflated them because v3.x prompts only had three slots. v5.4 + S11
  fixed the surface; the rubric needs to follow.

The user's framing is exactly right: *"the alternative of REFINE, KILL and
BUILD, instead of kill only, is huge."* REFINE is the headline lever the
new product UX promises, and the eval gate is the contract that says we
can measure it.

## What lives where today

### Rubric-side consumption of `expected_verdict`

- `tests/eval/runner.py::_evaluate_one` → reads `idea["expected_verdict"]`
  from the suite JSON, passes it to `score_transcript_{mock,live}` via
  `expected_verdict=`.
- `tests/eval/runner.py::_aggregate` → computes `verdict_accuracy` via
  `_normalize_verdict(actual) == _normalize_verdict(expected)`, where
  `_normalize_verdict` maps `pivot → kill` and leaves everything else
  intact.
- `tests/eval/rubric.py::compute_verdict_correctness(actual, expected)` →
  10 on exact match, 5 on `{pivot, kill}` near-match, 0 otherwise.
- `tests/eval/rubric.py::extract_verdict(text)` → parses the FIRST
  `verdict:\s*(\w+)` match from the judge prose. `build` is mapped to
  `ship`; `refine` has no branch (falls through to the lexical heuristic
  block, which can return `pivot` if "pivot" appears, else `unknown`).

### Fixture suites (the "expected_verdict" sources)

All five gate suites are JSON files under `tests/eval/suites/`:

| suite | path | n | expected_verdict values |
|---|---|---|---|
| general       | `tests/eval/suites/general_suite.json`         | 20 | `ship`, `kill` |
| crypto        | `tests/eval/suites/crypto_suite.json`          | 10 | `ship`, `kill` |
| saas          | `tests/eval/suites/saas_suite.json`            | 10 | `ship`, `kill` |
| holdout       | `tests/eval/suites/general_holdout_suite.json` | 10 | `ship`, `kill` |
| holdout_live  | `tests/eval/suites/general_holdout_live.json`  | 10 | `ship`, `kill` |

(There is no `tests/eval/fixtures/` directory in this repo — fixtures
live as suite JSON files. The agent definition's reference to
`fixtures/general.py` etc. predates the JSON-suite layout and should be
updated when this proposal lands.)

Total fixtures to relabel: **60 ideas**.

### Pipeline-side production of the new tokens

- `packages/gecko-core/src/gecko_core/models.py::Verdict` enum:
  `KILL | REFINE | BUILD`.
- `packages/gecko-core/src/gecko_core/models.py::derive_verdict(gap, consensus)`
  rule table:
  - `Full | False                                    → KILL`
  - `Partial:pricing | Partial:integration` & consensus≥0.8 → `BUILD`, else `REFINE`
  - `Partial:segment | Partial:UX | Partial:geo     → REFINE`
- `packages/gecko-core/src/gecko_core/orchestration/pro/_default_prompts_v5_4.json`
  judge prompt: Final verdict line (`KILL | REFINE | BUILD`).
- `packages/gecko-core/src/gecko_core/workflows.py::_parse_judge_verdict`:
  pulls the Final verdict line out of the judge summary and overrides
  `ResearchResult.verdict`.
- `packages/gecko-core/src/gecko_core/workflows.py::_detect_research_verdict`:
  legacy mapping `BUILD→ship, KILL→kill, REFINE→pivot` for the journal
  /flywheel/scaffold consumers.

## Proposal

### Schema — additive, backwards-compatible

Each suite-JSON idea gets a new optional field
`expected_verdict_v2: "KILL" | "REFINE" | "BUILD"`. Old `expected_verdict`
(`ship | kill`) stays in place for backwards compatibility and for the
journal/flywheel consumers that still key on it.

```jsonc
{
  "id": "holdout-good-ame-intake",
  "text": "...",
  "expected_verdict": "ship",        // legacy — keep
  "expected_verdict_v2": "BUILD"     // NEW — primary when present
}
```

### Rubric — read v2 if present, fall back to v1

Two minimum changes in `tests/eval/rubric.py` and `tests/eval/runner.py`:

1. **`extract_verdict`** gains a v2 path. Either:
   - Parse the judge's `Final verdict: KILL|REFINE|BUILD` terminal line
     directly (cleanest — that's the contract), and use that as the
     primary actual; fall back to the legacy `Verdict:` prose match.
   - OR keep the prose match and add `if word.startswith("refine"): return "refine"`
     and `if word.startswith("build"): return "build"` returning the new
     tokens directly.
   Recommended: parse the Final verdict line. It's the post-S11 contract.
2. **`compute_verdict_correctness(actual, expected, expected_v2=None)`**
   gains an `expected_v2` parameter. When set, it grades on the 3-token
   taxonomy:
   - exact match → 10.0
   - `{KILL, REFINE}` near-match → 5.0 (right intuition, softer framing)
   - `{BUILD, REFINE}` near-match → 5.0 (right intuition, harder framing)
   - `{KILL, BUILD}` mismatch → 0.0 (full disagreement)
   - `unknown` → 0.0

   When `expected_v2` is None, fall back to the legacy 2-token (`ship/kill`)
   path with the existing pivot=kill softening.
3. **`runner._aggregate`** uses v2 when present:
   `if "expected_verdict_v2" in idea: grade against v2 else legacy`.
4. **`runner._normalize_verdict`** stays as-is for legacy mode; gains a v2
   counterpart that doesn't collapse REFINE into KILL (REFINE is its own
   bucket now, and `verdict_accuracy` should reflect that).

### Migration path

| Phase | Owner | Effort | Output |
|---|---|---|---|
| **0. Land transcript archival (Rec 1 of analysis doc)** | software-engineer | 0.5d | `live_runs/*_transcripts.jsonl` per run |
| **1. Schema + rubric wiring** | software-engineer | 0.5d | `expected_verdict_v2` parsed; rubric grades v2 when present, falls back to v1 |
| **2. Relabel 60 fixtures** | ai-ml-engineer | 1d | All 5 suite JSONs get `expected_verdict_v2` populated; v1 unchanged |
| **3. Re-run gate at v2 with `--reruns 3`** | ai-ml-engineer | 0.5d (paid) | Baseline run captured at the new taxonomy |
| **4. Threshold tuning** | ai-ml-engineer | 0.5d | Update `docs/test-plan.md` thresholds for v2 (likely `verdict_accuracy_v2 ≥ 0.80` initially, tighten after 3 baseline runs) |

Total: ~3 days end-to-end. Rec 0 is a hard prerequisite — without
transcript archival we can't relabel fixtures with confidence (we'd be
guessing what the judge actually emits for each idea).

### Relabel rule of thumb (Phase 2)

For each fixture, the v2 label is decided by the *founder action*, not the
judge's keyword:

- `expected_verdict_v2 = "KILL"` — idea should not be built. Existing
  `kill` legacy fixtures.
- `expected_verdict_v2 = "BUILD"` — idea ships V1 as scoped, against a
  concrete named ICP and named payment moment. The strongest of the
  legacy `ship` fixtures (e.g. `holdout-good-twilio-webhook-replay`,
  `holdout-good-mcp-clickhouse-explainer` — both are
  named-buyer + named-artifact + 4-day-V1 candidates).
- `expected_verdict_v2 = "REFINE"` — idea has a real wedge but the V1 as
  described needs scope cuts before shipping. The borderline `ship`
  fixtures where the judge legitimately should hesitate (e.g.
  `holdout-good-comp-band-diff` — the wedge is real but the Pave
  comparison alone may not justify a paid V1 without segment narrowing).

Rough split estimate, pending fixture-by-fixture review with archived
transcripts: of the 30 legacy `ship` fixtures, ~20 → `BUILD`, ~10 → `REFINE`.
All 30 legacy `kill` fixtures stay → `KILL`.

## Strategic upside

Once the rubric speaks v2:

1. **The gate measures the lever the founder cares about.** REFINE is the
   "build it, smaller" lane that v5.4-S11 was designed to surface. The
   gate currently can't see it.
2. **Prompt iteration gets unstuck.** Today, when the judge correctly
   identifies a borderline-narrow-ICP idea as REFINE, the rubric grades
   it as a miss against `ship`. Prompt-tuning to make the gate happy
   pushes the judge toward unconditional SHIP, which is the wrong
   long-run incentive.
3. **The advisor panel becomes a measurable surface.** The `BUILD` vs
   `REFINE` boundary in `derive_verdict()` is gated on
   `advisor_consensus ≥ 0.8`. Once v2 lands, the advisor panel's
   contribution to verdict quality becomes directly testable — set
   `expected_verdict_v2 = "BUILD"` for fixtures where the panel SHOULD
   reach consensus, `REFINE` where it shouldn't, and grade accordingly.
4. **Sprint 12 vertical-suite fixtures (DeFi, regulated, etc.) ship with
   v2 from day one** — no relabel debt accrues.

## Open questions for staff-engineer / business-manager

- Should `verdict_accuracy_v2` REPLACE `verdict_accuracy` in the gate
  contract, or live alongside it? Recommendation: live alongside for one
  sprint, then deprecate v1 once v2 has 3+ baseline runs.
- Threshold for v2: stay at 0.80 or tighten to 0.85? Recommendation: hold
  at 0.80 for the first three runs (the 3-bucket scoring is structurally
  harder than 2-bucket; same accuracy means better calibration), then
  tighten only when there's evidence.
- Do we want a v2-aware variance check? E.g. "verdict_accuracy_v2
  AND distribution(KILL, REFINE, BUILD) within ±15% of suite priors" —
  catches a model that gets the binary right but collapses everything to
  REFINE. Probably yes; defer to Sprint 12.

## Out of scope

- Relabeling fixtures (Phase 2) — this proposal is the schema + path only.
- Touching `_detect_research_verdict` legacy mapping (journal stays on
  `ship/kill/pivot` until the journal consumers are migrated separately).
- Changing the v5.4 judge prompt — the prompt is correct; the rubric is
  the lagging piece.
