# S37 Phase 1 — synthesized root cause + fix design

**Date:** 2026-05-18
**Inputs:** six diagnostics — #112 (judge audit), #113 (retrieval trace),
#114 (panel trace), #115/Phase-1-A (verdict_accuracy), #116/Phase-1-B
(confidence_calibration), #117/Phase-1-C (D1 rubric-contract).
**Status:** analysis complete. Phase 2 (execution) is founder-gated on a
review of this document.

## The result being explained

S36's N=50 ship-gate (#110) came back **3/6**. Three dimensions are red:
`verdict_accuracy` 0.880, `hallucination_score` 0.340, `confidence_calibration`
0.568. N=50 also falsified #103's optimistic 5/6 — that was small-N luck.

## What the six diagnostics ruled OUT

- **Retrieval** (#113) — sound. Every fixture got a healthy on-topic
  15-chunk slate. Not the cause.
- **Context overflow / null turns** (#114) — absent. All 50 rows: 7 complete
  turns, 0 degraded, ~3.5K tokens vs a 128K window. The past-incident failure
  mode is confirmed not present.
- **The rubric judge** (#112) — trustworthy. Audited 10 rows; correct on
  every one. Do not touch the judge.

## Red dimension 1 — hallucination_score (0.340)

**Root cause (confirmed, #112 + #114).** The panel genuinely fabricates
numbers — systemic across all 7 voices, a seed-then-cascade pattern: an
analyst mis-attributes or mis-transforms a real chunk figure (a historical
crash-epoch APY reported as forward yield; figures merged across date-stamped
snapshots); `strategist`/`bull_bear` freelance fresh round numbers with no
source at all; the `coordinator` launders them into `key_drivers`. The
grounding gate **detects every one** — then only mutates confidence and
appends a blocker. It **never removes the figure from `turns[]`/
`key_drivers`**, so the judge reads the fabricated number and penalizes.
S36-WS2's gate was architecturally half-built; S36-WS1's "truncation
artifact" diagnosis was inverted.

**Contributing (D1, #117).** ~5 fixtures are pinned at `hallucination=0.0`
deterministically because `must_not_hallucinate` demands grounding in
`market_data`/`paysh_live` chunks the corpus does not contain — the flagged
figures actually live in `protocol_native` chunks the panel received.

**Contributing (renderers, #112).** S36-WS2 fixed only the `protocol_native`
renderer; `jito_mev.render_chunk` and the Jupiter renderer still emit a
metadata-heavy header, so even an honestly-cited figure can sit past the
320-char snippet window the judge verifies against.

## Red dimension 2 — verdict_accuracy (0.880)

**Root cause (confirmed, #115).** All 6 N=50 misses are coordinator
*reasoning* — the closing-line contract and the verdict-extraction code are
both sound (zero emission bugs, zero parse bugs). Two patterns:

- **No direction model** — `act`/`pass`/`defer` cannot represent "don't
  rotate" or "observe a short"; both collapse to `pass`.
- **DEFER over-suppressed** — the S24 threshold needs 3 opposition tokens, so
  a lone `risk=unacceptable` veto cannot escalate to `defer`.

## Red dimension 3 — confidence_calibration (0.568)

**Root cause (confirmed, #116).** ~85-90% artifact. The gate multiplies
`confidence × grounded_fraction` *after* the coordinator already wrote
0.65/0.75 into the transcript. The judge sees "confidence 0.00 but verdict
ACT" and docks the calibration axis — the gate manufactures the exact
contradiction the metric catches. Gate-fired rows (n=29) score 0.52;
not-fired rows (n=21) score 0.64 and already lock. ~10-15% is genuine
under-confidence on real weak-grounding rows.

## The fix design — Phase 2

### WS1a — gate redaction + stop the confidence mutation (ai-ml-engineer)

`apply_grounding_gate` already extracts every numeric claim with exact
offsets. Two changes:
1. **Redact** the ungrounded numeric span from `turns[]` and `key_drivers`
   instead of leaving it in place. (Rendering of the redacted span is the one
   product decision below.)
2. **Stop the `confidence × grounded_fraction` mutation.** Once the bad
   figure is removed the verdict text is honest — confidence should reflect
   the cleaned verdict, not be driven to 0.00. This is what closes
   `confidence_calibration` (#116): the mutation is what manufactures the
   calibration contradiction.

Closes `hallucination_score` (severs the flag-no-scrub mechanism) **and**
`confidence_calibration` (removes the artifact). Light-fake unit tests on the
recorded `turns[]`, no LLM spend.

### WS1b — renderer fix (data-engineer)

Make `jito_mev.render_chunk` + the Jupiter renderer number-first, as S36-WS2
did for `protocol_native`. Code change; the corpus re-ingest to land it is
founder-gated (`.env`-sourced, like the #109 re-ingest).

### WS2 — coordinator verdict rules (ai-ml-engineer)

Two deterministic rules in `_build_verdict_from_coordinator` (code, not
prompt — per `feedback_prompt_iteration_plateau`): a risk-veto DEFER
escalation and a rotation-framing DEFER escalation. Recovers 4 of 6 misses →
48/50 = 0.96 → `verdict_accuracy` locks. The other 2 (`jupiter-lst-rotation`
— panel treats speculative points as cash) are a genuine upstream
panel-reasoning gap; tracked as a separate persona ticket, not a lock blocker.

### D1 — fixture-contract fix (small, gating)

Per #117 Option 2: rewrite each `must_not_hallucinate` rule in
`tests/eval/suites/defi_trade_rubric_suite.json` to demand only
`protocol_native` — the kind the corpus actually supplies. ~10 string edits,
one file, zero production-code blast radius. **Must land before the
re-measure** or the result is judged against an unsatisfiable rule. Defer the
genuine `ingest_market_data.py` enrichment (free) to a later sprint.

### WS3 — confidence_calibration

No separate fix expected — WS1a subsumes it. WS3 is the post-fix re-measure
check; act only if a residual miss survives.

## The one product decision — how a redacted figure renders

WS1a removes ungrounded numbers from user-facing verdict text. Three ways to
render the gap (a product-designer call):

- **(a) Inline `[unverified]` marker** — `"APY of 26.12%"` → `"APY of
  [unverified]"`. Honest, visible, matches the founder's "say I don't know"
  principle. *Recommended.*
- **(b) Qualitative rephrase** — `→ "an elevated APY"`. Readable but
  substitutes a softer claim.
- **(c) Drop the whole clause/driver.**

## Sequencing

WS1a + WS1b + WS2 + D1 in parallel → founder-gated re-ingest (WS1b) →
founder-authorized N=50 capstone (confirms all three red dimensions lock;
N=50 is sufficient if the post-fix means clear ~0.50 / 0.60 / 0.85). D1 lands
before the re-measure.

## Confidence in this plan

Higher than S36's. Every fix traces to a confirmed mechanism, not a
hypothesis: #112+#114 prove flag-no-scrub; #116 proves the calibration
artifact; #115 traces all 6 verdict misses. The honest residual risk: the
rubric is LLM-judged, so the post-fix N=50 must confirm the locks — no
dimension is declared green on this analysis alone.
