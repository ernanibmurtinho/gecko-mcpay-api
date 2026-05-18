# S36-#112 — `hallucination_score` root-cause diagnostic (post-WS2)

**Date:** 2026-05-18
**Ticket:** s36-#112 (FREE, READ-ONLY — no code change, no eval run, no LLM spend)
**Branch:** `s36/prompt-grounding`
**Owner:** ai-ml-engineer
**Inputs:** the 50 scored rows in
`tests/eval/live_runs/2026-05-18-s24-defi-rubric-s36-110-shipgate-r{1..5}-10.json`

## Why this diagnostic exists

S36-WS2 (the code-side grounding gate + number-first renderers) and #108
(the abstain prompt) were built on the WS1 diagnosis
(`docs/eval/2026-05-18-s36-hallucination-diagnosis.md`). At N=50 they did
**not** move `hallucination_score`: mean 0.340, CI [0.220, 0.470] —
statistically identical to #103's 0.367. WS1's central claim was *"the
dominant failure mode is a harness truncation artifact; the figure is real
and API-sourced, the judge just never sees it; raise the snippet cap and it
locks."* That claim is now falsified. This doc establishes what is actually
wrong, with row-level evidence, so the next fix is not another guess.

## Q1 — Flag-vs-scrub: **CONFIRMED.** The gate flags but never scrubs.

Cross-tab of all 50 rows, `hallucination_score` vs grounding-gate-fired
(gate-fired = a `blocker_questions` entry beginning `"Grounding gate:"`):

| hall_score | gate fired | rows |
|---|---|---|
| 0 (flagged hallucinating) | yes | **24** |
| 0 | no | 11 |
| 1 (clean) | yes | 5 |
| 1 | no | 10 |

**24 of 35 low-hall rows had the gate fire and still scored hall=0.** The
gate did its job — it detected the ungrounded figure, floored confidence,
appended the blocker — and the row still failed, because the fabricated
number is **still sitting in `panel.turns[]`** where the judge reads it.

Row evidence (`r1`):

- `jito-mev-tip-band` — gate appended: *"Grounding gate: the following
  figures are not confirmed by any cited snippet … 1%, 5.04575e-06, 7e-06,
  8.5e-06, 8.8785e-06."* Confidence floored to **0.00**. Yet `8.8785e-06`
  remains verbatim in **5 turns** (`technical_analyst`, `sentiment_analyst`,
  `fundamental_analyst`, `bull_bear_debater`, `coordinator`). Judge note:
  *"the panel's own blocker_questions flag these as unverified … making them
  ungrounded specific figures."* → hall=0.
- `sanctum-unstake-vs-hold` — gate flagged `26.12%`; confidence floored to
  0.00; `26.12% APY` still in `sentiment_analyst`, `risk_manager`,
  `bull_bear_debater` turns. → hall=0.

The 5 gate-fired-but-hall=1 rows are the tell: every one of them flagged
**only** trivial position-sizing figures (`1%`, `$5,000`) — never a real
data claim. The gate "works" exactly when it has nothing load-bearing to
catch. When it catches a real fabricated figure (`8.8785e-06`, `26.12%`,
`$173M`), the row fails anyway. **`apply_grounding_gate` is confidence-only;
it never mutates `turns[]` or `key_drivers`** (see `grounding_gate.py:292`
docstring — "the gate only signals … via confidence + an explicit blocker").
The judge scores the text, not the confidence number. Flag-no-scrub cannot
move a text-scored dimension. This is the single biggest reason WS2 did
nothing.

## Q2 — How WS1's diagnosis went wrong

WS1 classified the #103 bad rows as **~6 truncation-artifact / 4
misattribution / 3 genuine invented-number** and concluded the dominant
lever was the snippet cap (200→320). Two independent errors:

**Error 1 — WS1 trusted the judge's hedge-language as a taxonomy.** WS1
read judge notes like *"the snippet does not visibly confirm these numbers"*
and classed the row as "truncation artifact — the number is in the API
response, the judge just can't see it." But the judge note describes a
**symptom** (number absent from snippet), not a **cause** (number absent
because truncated vs. number absent because fabricated). WS1 assumed the
former without checking the chunk. The N=50 data refutes it directly: I
checked every gate-flagged figure against the **full 320-char snippet** of
every evidence + framework citation in all 24 gate-fired low-hall rows —
**every flagged figure is absent from every snippet, at the new 320 cap.**
If WS1's truncation theory held, raising 200→320 would have surfaced them.
It surfaced none.

**Error 2 — WS1 fixed one renderer and assumed it fixed all of them.** WS1
named `protocol_native.py`'s renderer and said "lead with the numeric
payload." WS2 re-ingested `protocol_native` (342 chunks per the S36
end-state memory). But `jito_mev.py` has its **own** `render_chunk`
(`jito_mev.py:93`) that WS2 never touched — it emits a 4-line header
(`Protocol-native API:` + `Endpoint description:` + `Source URL:` +
`Content kind:`) **before** `----- body -----`. That header alone is ~280
chars; the 320-char snippet cap is exhausted before the body's lamport
figures. `jito-mev-tip-band` cite[1] (the live tip_floor API) snippet at
320 chars is *still* a pure endpoint description with **zero numeric
tokens**. The "number-first" fix never reached the Jito renderer.

**The deeper error — most of these are genuine fabrications, not artifact.**
WS1 said ~3/22 were genuine invented numbers. N=50 shows the opposite ratio.
Definitive row: `jupiter-lst-rotation-msol` r3. The cited chunks state
**bSOL market cap `$87.08M`, mSOL market cap `$267.25M`**. The panel's
`risk_manager` and `bull_bear_debater` turns state **bSOL liquidity
`$832.78M`, mSOL liquidity `$173.03M`** — neither number exists in any
chunk. `$832.78M` is *JitoSOL's* market cap, which appears in a **different
fixture's** chunk (`jupiter-jlp-vs-jitosol`). The model is leaking a
memorized/cross-fixture figure and relabelling "market cap" as "liquidity."
That is textbook fabrication, not a truncation artifact. WS1's
artifact-vs-genuine call was inverted: ~⅔ of the bad rows are genuine
fabrication, not ⅓.

**Takeaway:** WS1's diagnosis method — classifying rows from judge note
*language* without cross-referencing the actual chunk text — is not
trustworthy. The next fix must verify against chunk content, not judge
prose.

## Q3 — Judge audit: the judge IS trustworthy on hallucination.

Sampled 10 low-hall rows across all 5 runs and checked, for each,
whether the gate-flagged figure is genuinely ungrounded:

| Row | Flagged figure | In any chunk? | Judge correct? |
|---|---|---|---|
| r1 `jito-mev-tip-band` | `8.8785e-06 SOL` | no — cite[1] is endpoint desc; cite[2] is a docs page | YES |
| r1 `sanctum-unstake-vs-hold` | `26.12% APY` | no — all 4 cites are Sanctum doc prose, no APY | YES |
| r1 `jupiter-jlp-vs-jitosol` | `$173M` liquidity | no — cites show market cap `$832.78M`, never `$173M` | YES |
| r3 `jupiter-lst-rotation-msol` | `$832.78M` bSOL liq | no — bSOL chunk says market cap `$87.08M` | YES |
| r3 `jupiter-lst-rotation-msol` | `$173.03M` mSOL liq | no — mSOL chunk says market cap `$267.25M` | YES |
| r2 `jito-mev-tip-band` | `5.04575e-06 SOL` | no | YES |
| r4 `sanctum-unstake-vs-hold` | `26.12% APY` | no | YES |
| r3 `drift-jto-perp-short` | `0.3035` falsifier | no — arithmetically derived from cited `0.275925` | borderline-correct |
| r5 `jupiter-lst-rotation-msol` | `$100` threshold | no — falsifier level, no citation | YES (strict) |
| r2 `jupiter-lst-rotation-msol` | `$70M` threshold | no | YES (strict) |

The judge is **correct** that these figures are ungrounded. There is no
case in the sample where the judge penalized a figure that was actually
present in a citation it overlooked. This is **not** the S29-#29 pattern (a
miscalibrated judge); `hallucination_score` is measuring exactly what we
think it measures — the panel emits a specific number with no source.

One nuance worth tracking but **not** a judge bug: the binary 0/1 shape
conflates a real fabricated data figure (`8.8785e-06`) with a strict-reading
falsifier threshold (`$100`, `0.3035` derived arithmetically from a cited
price). The judge applies "zero tolerance per the rubric" consistently and
correctly — but a fixture that ships a fabricated tip-floor figure scores
the same 0 as one whose only sin is an unsourced round-number stop-loss.
That is a rubric-granularity issue (partial credit), not a trust issue. The
#110 scorecard's claim that the judge "conflates confidence-text mismatch
with hallucination" (re: `kamino-sol-leverage-entry`) did **not** reproduce
in this sample — every flagged row had a genuinely ungrounded figure.

**Verdict: trust `hallucination_score`. Do not "fix the judge."** A fix
that targets the judge would be chasing a phantom.

## Q4 — verdict_accuracy Jupiter cluster: **NOT #92.**

The 6 verdict_accuracy misses at N=50:

| Run | Fixture | Got | Expected set |
|---|---|---|---|
| r1 | `jupiter-lst-rotation-msol` | `act` | {PASS, DEFER} |
| r5 | `jupiter-lst-rotation-msol` | `act` | {PASS, DEFER} |
| r2 | `jupiter-jlp-vs-jitosol` | `pass` | {ACT, DEFER} |
| r5 | `jupiter-jlp-vs-jitosol` | `pass` | {ACT, DEFER} |
| r2 | `kamino-sol-leverage-entry` | `pass` | {ACT, DEFER} |
| r2 | `drift-jto-perp-short` | `pass` | {ACT, DEFER} |

#92 is *protocol_native pulls wrong-instrument data* (a JTO question
retrieving ETH-PERP chunks). I checked the evidence citations on every miss:
they are **on-instrument**. `jupiter-jlp-vs-jitosol` r2 cites
`jupiter/jupiter-lst-prices` + JitoSOL price/market-cap — correct
instrument. `jupiter-lst-rotation-msol` cites bSOL + mSOL chunks — correct
instruments. The retrieval is fine.

The miss is **verdict-synthesis semantics**. `jupiter-jlp-vs-jitosol`'s
fixture `why` explicitly says *"PASS would be wrong"* — yet the panel emits
`pass`. The coordinator parsed `verdict: pass` because the strategist turn
said *"hold JitoSOL for now"* — semantically a stand-pat call. On a
**rotation** question, "stand pat / don't rotate" is `pass`, but the fixture
treats not-rotating-into-a-100%-concentration as `DEFER` (flag the
concentration risk), not `pass`. Symmetrically `jupiter-lst-rotation-msol`'s
fixture says *"PASS is correct"* (points are not cash) — the panel emits
`act`. The coordinator cannot reliably distinguish "do the rotation" (ACT)
from "stand pat" (PASS) from "not enough info, flag the risk" (DEFER) on
rotation-framed questions.

This is a **coordinator verdict-mapping problem on rotation questions**, not
retrieval. Note `kamino-sol-leverage-entry` r2 and `drift-jto-perp-short` r2
are in the same cluster — both emit `pass` against {ACT, DEFER}. The common
factor is the coordinator collapsing a thin-evidence/conservative stance to
`pass` when the fixture wants `defer`. Per `feedback_prompt_iteration_
plateau`, verdict-token logic belongs in code (`derive_verdict` /
coordinator parse), not in a prompt.

## What actually moves `hallucination_score` — and is it a real fix

The chain is now unambiguous:

1. The panel **genuinely fabricates** specific data figures (~⅔ of bad
   rows) — invented tip-floor lamports, market-cap-relabelled-as-liquidity,
   cross-fixture figure leakage, and invented event anecdotes (`26.12% APY
   during the October 2025 flash-crash`).
2. The grounding gate **detects** most of them (24/35 low-hall rows) but
   only floors confidence — it leaves the figure in `turns[]`.
3. The judge scores `turns[]`, sees the figure, flags it. Correctly.

So three levers, in order of effect and honesty:

### Lever A (real fix, primary) — scrub ungrounded figures from verdict text

Extend `apply_grounding_gate` so that when `check_grounding` finds an
ungrounded numeric claim it **rewrites the offending span** in
`key_drivers`, `blocker_questions`, and `turns[].content` — replacing the
figure with a qualitative phrasing (`"8.8785e-06 SOL"` → `"an elevated P75
tip floor"`) or an explicit `[unverified]` tag the judge rubric is taught to
not penalize. The gate already extracts every ungrounded `NumericClaim` with
its `surface` tag; the missing step is the mutation. The judge scores text —
remove the figure from the text and the flag cannot fire.

- **Effort:** moderate. The extraction half exists. The new work is a
  span-safe rewrite (re-locate each `NumericClaim.raw` in its surface,
  substitute) plus extending the mutation to `turns[]` (currently
  `model_copy` only touches `confidence` + `blocker_questions`).
- **Risk:** moderate. Mutating `turns[]` changes verdict-render text that
  ships to users — needs `product-designer` sign-off on the rewrite phrasing
  and a test that a scrubbed verdict still reads coherently. Over-scrubbing a
  *grounded* figure (false positive in `_claim_in_text`) would degrade a
  good verdict. The fuzzy matcher is already conservative; an audit of the
  11 hall=1-no-gate rows confirms it does not over-fire.
- **Is it real?** Yes — it is the only lever that addresses the proven
  mechanism (judge scores text; text still has the figure). It is **not**
  another guess: Q1 proves the gate detects but does not scrub, and Q3
  proves the judge correctly penalizes whatever figure remains.

### Lever B (real fix, supporting) — fix the Jito/Jupiter renderers so genuinely-grounded figures survive

`jito_mev.py:render_chunk` must lead with the numeric body, not a 4-line
header (the header → trailing-line move WS1 prescribed for `protocol_native`
but never applied here). Same audit needed for the Jupiter LST renderer.
This will not fix the fabrications in Lever A's scope — but it stops the
gate from *correctly* flagging figures that the panel could have grounded if
the snippet had shown them. Without B, Lever A would scrub a real tip-floor
figure the panel legitimately had. **B must land before or with A.**

- **Effort:** small. Reorder the render template; re-ingest Jito + Jupiter
  LST chunks (data-engineer owns the renderer + re-ingest).
- **Risk:** low. Display-only reorder; the provenance header moves to a
  trailing line.

### Lever C (do NOT do) — "fix the judge"

Q3 establishes the judge is correct. A judge-side change would mask the
true-signal floor and is the failure mode this ticket exists to prevent.
The only judge-adjacent work worth tracking is a **partial-credit rubric
shape** (fraction of numeric claims grounded) — it lowers CI half-width at
fixed N and stops a fabricated-data row scoring identically to an
unsourced-stop-loss row. Track as a separate, non-blocking ticket.

### Sequencing

1. **Lever B** — renderer fix + re-ingest (Jito, Jupiter LST). Cheap, no
   risk, unblocks A.
2. **Lever A** — extend `apply_grounding_gate` to scrub ungrounded spans
   from `turns[]` / `key_drivers`. Gate `data-engineer` (renderer) +
   `product-designer` (scrub phrasing).
3. Re-run N=50. Expected: the 24 gate-fired low-hall rows convert toward
   clean. At a true mean ≥0.55, N≥16 certifies the 0.30 floor. If genuine
   fabrications survive (the model invents a figure the gate's regex misses),
   that residue is the real true-signal floor and the honest next step is a
   panel-prompt grounding constraint accepted as imperfect — but Lever A
   removes the bulk first.

`verdict_accuracy` (Q4) is a separate, smaller fix: a coordinator
verdict-mapping correction for rotation-framed questions, in code, not
prompt. Out of scope for `hallucination_score` but should ride the same
sprint.

## Bottom line

WS2 did nothing because it built on an inverted diagnosis: it treated a
genuine-fabrication problem as a truncation artifact, shipped a gate that
flags-without-scrubbing, and fixed one renderer out of several. The
`hallucination_score` lever is **Lever A — scrub the ungrounded figure out
of the text the judge scores** — paired with **Lever B** so legitimately-
grounded figures survive the snippet window. That is a real fix grounded in
the proven mechanism (Q1 + Q3), not another guess. The judge is trustworthy;
do not touch it. The Jupiter cluster is not #92; it is a coordinator
verdict-mapping bug, fixable separately.

## Artifacts referenced

- `tests/eval/live_runs/2026-05-18-s24-defi-rubric-s36-110-shipgate-r{1..5}-10.json`
- `packages/gecko-core/src/gecko_core/orchestration/trade_panel/grounding_gate.py`
- `packages/gecko-core/src/gecko_core/sources/jito_mev.py` (untouched renderer)
- `tests/eval/scripts/score_defi_trade_rubric.py` (judge rubric, lines 594-605)
- `docs/eval/2026-05-18-s36-hallucination-diagnosis.md` (the WS1 diagnosis this refutes)
