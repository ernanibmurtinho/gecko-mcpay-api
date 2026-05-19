# S37 Phase-1-A — `verdict_accuracy` deep diagnosis (N=50 #110 ship-gate)

**Date:** 2026-05-18
**Ticket:** S37-#A (READ-ONLY — no code change, no eval run, no LLM spend)
**Branch:** `s36/prompt-grounding`
**Owner:** ai-ml-engineer
**Inputs:** `tests/eval/live_runs/2026-05-18-s24-defi-rubric-s36-110-shipgate-r{1..5}-10.json`
(50 scored rows, persisted `panel.turns[]`)
**Code:** `packages/gecko-core/src/gecko_core/orchestration/trade_panel/`
— `personas.py` (`CLOSING_LINE_PATTERNS`), `__init__.py`
(`_parse_closing_line`, `_extract_json_block`, `_coerce_verdict_token`,
`_build_verdict_from_coordinator`), `_default_prompts.json` (coordinator prompt).

## Why this diagnostic exists

#110 came back 3/6. `verdict_accuracy` mean **0.880**, CI **[0.780, 0.960]**
— dips below the 0.85 bar (6 misses / 50). #112-Q4 named the failure a
"coordinator verdict-mapping problem on rotation questions" but did not
trace it to a category. This doc traces all 6 misses to one of three
categories — **(1) reasoning**, **(2) emission**, **(3) parse/mapping code**
— because that split decides the fix lane.

## Method

For each of the 6 misses I dumped the strategist, bull_bear_debater, and
coordinator turns and checked three things:
- **Emission:** do the JSON `verdict` field, the prose body, and the
  `Final verdict:` closing line all agree, and is the closing line in the
  canonical `^Final verdict:\s*(act|pass|defer)\s*$` form?
- **Parse:** does `_parse_closing_line` + `_coerce_verdict_token` +
  `_build_verdict_from_coordinator` extract what the coordinator wrote?
- **Reasoning:** does the coordinator's verdict match the fixture's
  `expected_verdict_v2_set`, given the fixture `why`?

## The 6 misses

| Run | Fixture | Got | Expected set | Fixture `why` (abridged) |
|---|---|---|---|---|
| r1 | `jupiter-lst-rotation-msol` | `act` | {PASS, DEFER} | "points-narrative rotation is speculative — points are not cash. PASS is correct." |
| r5 | `jupiter-lst-rotation-msol` | `act` | {PASS, DEFER} | same |
| r2 | `jupiter-jlp-vs-jitosol` | `pass` | {ACT, DEFER} | "100% rotation is a concentration call. DEFER is calibrated… PASS would be wrong." |
| r5 | `jupiter-jlp-vs-jitosol` | `pass` | {ACT, DEFER} | same |
| r2 | `kamino-sol-leverage-entry` | `pass` | {ACT, DEFER} | "3x leverage… ACT without strong dissent is a hallucination of safety. DEFER with strong dissent is the most calibrated answer." |
| r2 | `drift-jto-perp-short` | `pass` | {ACT, DEFER} | "unlock-supply short at 1x/21d is a defensible directional call. ACT justified; DEFER acceptable if short-squeeze flagged." |

## Per-miss category determination

### Emission + Parse are CLEAN on all 6 — ruled out first

Every one of the 6 coordinator turns ends with a canonical closing line
(`Final verdict: act` or `Final verdict: pass`), carries a `json`-fenced
block whose `verdict` field matches, and the prose body states the same
token. `_parse_closing_line` captures it; `_extract_json_block` parses the
fence; `_build_verdict_from_coordinator` reads `block.get("verdict")`
first (closing-line is only the fallback) — both surfaces agree, so the
fallback never even matters. `_coerce_verdict_token` whitelists `act`/`pass`
/`defer` and all 6 tokens are in-whitelist. The S24 dissent/abstain
override (`dissent_count>=3` or `derived_abstains>=3` → flip to `defer`)
did not fire on any of the 6 — none reaches 3 directional dissents or 3
abstains (see below). **Category 2 (emission) = 0/6. Category 3 (parse/
mapping code) = 0/6.** There is no ambiguous closing line and no regex/
mapping mishandling anywhere in the 6.

### r1 / r5 `jupiter-lst-rotation-msol` — **Category 1 (reasoning)**

Coordinator emits `act` cleanly. Fixture wants PASS (primary) or DEFER.
The fixture thesis: a **points-narrative** rotation is speculative —
"points are not cash" — and the panel "must call out points-as-non-cash
explicitly." Neither run's panel does. r1's coordinator: "leaning towards
action… capitalizing on the bullish sentiment and yield potential."
r5's: "strong alignment towards executing the proposed rotation." The
panel treated the bSOL "active yield narrative" as a real cash yield and
went long. The closing line faithfully conveys a genuinely wrong verdict.
This is the coordinator (and the whole panel upstream of it) reasoning
incorrectly — it never engages the speculative-points distinction the
fixture is testing. Pure Category 1.

### r2 / r5 `jupiter-jlp-vs-jitosol` — **Category 1 (reasoning), rotation-semantics subtype**

Coordinator emits `pass` cleanly. Fixture wants DEFER (primary) or ACT;
PASS is explicitly wrong. The question is "rotate 100% of a JitoSOL stack
into JLP." The strategist turn in both runs reads `action: hold` /
"hold JitoSOL for now." The coordinator faithfully maps "hold the current
holding" → `pass` ("the idea is rejected for now"). Per the coordinator
prompt's own definition, `pass` = "the panel does not support action."
**On a rotation question that is semantically correct** — declining to
rotate IS declining the proposed action. But the fixture treats a 100%
all-in rotation as a *concentration call* that an honest panel must
**flag** (DEFER), not silently reject (PASS). The panel never surfaced
concentration risk as a blocker; it folded "don't rotate" into a flat
reject. This is a reasoning miss: the panel under-weighted the
concentration framing. It is *not* an emission/parse miss — but note the
**root cause is a genuine semantic ambiguity** in the rotation frame
(see "The rotation-frame ambiguity" below).

### r2 `kamino-sol-leverage-entry` — **Category 1 (reasoning)**

Coordinator emits `pass` cleanly. Fixture wants DEFER (primary) or ACT;
PASS is outside the set. Fixture `why`: "DEFER with strong dissent is the
most calibrated answer" — the panel should surface liquidation/cascade
risk and *defer on it*. The panel DID surface it: risk_manager =
`unacceptable`, strategist = "observe… pending improvements in risk
profile." But the coordinator collapsed that to `pass` ("does not support
executing… the risks outweigh the bullish technical indicators"). The
inputs for a DEFER were present in the turns — one named blocker (the
audit-status question is literally in `blocker_questions`) — yet the
coordinator chose `pass`. The DEFER-threshold clause in the prompt blocked
it: only **one** voice hit an exact-match opposition token (risk=
`unacceptable`); the prompt's clause-(iii) needs **≥3**, and clause-(i)
needs ≥3 abstains. So the coordinator correctly followed the prompt and
still produced a verdict outside the fixture's accepted set. Category 1,
but the proximate cause is the **DEFER-threshold rule in the prompt being
mis-tuned** (too strict — see fix).

### r2 `drift-jto-perp-short` — **Category 1 (reasoning)**

Coordinator emits `pass` cleanly. Fixture wants ACT (primary) or DEFER;
PASS is outside the set. The unlock-supply short thesis "is a defensible
directional call." The panel read it bearishly (technical volatility,
risk=`unacceptable`) and the strategist said "observe… due to unacceptable
risk." The coordinator mapped "observe" → `pass`. But the fixture's point:
a 1x/21d short on an unlock-supply thesis IS the directional call (ACT) —
or DEFER if short-squeeze risk is flagged hard. The panel treated the
*short itself* as the risky action to reject, rather than recognizing the
short is the bearish position the bearish reads support. This is a
reasoning inversion: bearish analysis on a *short* thesis should push
toward ACT (take the short), not `pass`. The coordinator has no notion
that the proposed action's direction is itself short. Category 1.

## The dominant failure category

**6/6 are Category 1 — reasoning.** Zero emission misses, zero parse/
mapping-code misses. The closing-line contract and the extraction code are
sound. The bug is entirely in *what verdict the coordinator picks*.

Within Category 1 there are two distinct sub-failures:

- **Sub-failure 1A — rotation/short-direction blindness (4 rows:** both
  `jupiter-jlp-vs-jitosol`, `drift-jto-perp-short`, and arguably both
  `jupiter-lst-rotation-msol`**).** The coordinator's `act`/`pass`/`defer`
  definitions are written for a *long-entry* mental model: `act` = "execute
  the strategist's intent," `pass` = "the idea is rejected." When the
  proposed action is itself a **rotation** ("move X into Y") or a **short**,
  "don't do it" and "do it" do not cleanly map to `pass`/`act`. Declining a
  rotation collapses to `pass`; a bearish read on a short thesis collapses
  to `pass` instead of ACT-the-short. The fixtures want the
  concentration/squeeze risk *flagged* (DEFER), not the idea flatly
  rejected.

- **Sub-failure 1B — the DEFER threshold is mis-tuned (2 rows clearly:**
  `kamino-sol-leverage-entry`, `drift-jto-perp-short`; contributes to all
  4 of the {ACT,DEFER} misses**).** The S24 night-shift DEFER clause
  (prompt clause i/ii/iii AND the code mirror in
  `_build_verdict_from_coordinator`, `dissent_count>=3` / `abstains>=3`)
  requires **3** exact-match opposition tokens before `defer` is allowed.
  In every {ACT,DEFER} miss exactly **one or two** voices hit an
  exact-match token (`risk=unacceptable` plus, in r5 jlp, a `bearish`
  technical). The clause explicitly says "Count of exactly 2 is NOT a
  defer trigger." So a panel with one `unacceptable` risk band and a
  strategist saying "observe" is *prompt-forbidden* from deferring and is
  pushed to a directional `act`/`pass`. S24 tightened DEFER to kill a
  defer-rate plateau (see `feedback_prompt_iteration_plateau`); that tighten
  now over-suppresses DEFER on exactly the high-risk fixtures where DEFER
  is the calibrated answer.

The rotation-frame question #112 raised: **is rotating-OUT an ACT or a
PASS-on-the-current-holding?** Answer from the fixtures: it is *neither* —
the fixtures treat a high-concentration rotation as a **DEFER** (flag the
risk). The semantics are genuinely ambiguous *as currently defined* — the
prompt's `pass` definition ("the idea is rejected for now") legitimately
covers "don't rotate," so the coordinator is not wrong by its own
contract. The contract itself does not distinguish "reject the idea" from
"the idea has an unresolved risk that must be flagged." That is the real
defect.

## The concrete fix the diagnosis points to

This is **not** an emission-contract fix and **not** a parse-code fix —
those are clean. It is **coordinator verdict logic**, and per
`feedback_prompt_iteration_plateau` (repo memory: gpt-4o-mini rounds toward
caution on any defer-related *prompt* instruction; S24 already burned 4
prompt iterations 1.0→0.20→0.50→0.90 on exactly this) the fix belongs in
**CODE**, in `_build_verdict_from_coordinator`, not in another prompt edit.

Two code changes, both in `__init__.py`:

### Fix 1 — code-side DEFER escalation on high-risk-without-consensus
(addresses sub-failure 1B, 2-4 rows)

The existing code mirror only flips to `defer` at `dissent_count>=3` or
`derived_abstains>=3`. Add a *third* code-side escalation: when
`risk_manager` closing-line token is `unacceptable` **and** the verdict
the coordinator emitted is `act` or `pass` **and** the strategist's
`strategic_intent` contains an observe/hold/wait token — flip to `defer`
with a blocker naming the risk. Rationale: an `unacceptable` risk band is,
by the risk_manager's own veto-power role (`personas.py` ROLE_TASKS:
"veto on oracle/slippage/contract/concentration risk"), structurally a
defer trigger on its own — it should not need 2 more voices to agree. This
is a deterministic rule on already-parsed `parsed_verdict` tokens, exactly
the shape S24 used for the dissent/abstain overrides. **Honest assessment:
this is a real fix** — it directly converts `kamino-sol-leverage-entry`
and `drift-jto-perp-short` from outside-set to in-set (both have
`risk=unacceptable` + strategist "observe"), and is grounded in the
risk_manager's defined veto role, not a vibe. It will NOT help
`jupiter-lst-rotation-msol` (that panel had `risk=elevated`, not
`unacceptable` — see Fix 2).

### Fix 2 — rotation-frame verdict semantics (addresses sub-failure 1A)

This one is **partly a real fix, partly needs a judgment call from the
founder/PD**, and I will not pretend otherwise. The structural problem:
`act`/`pass`/`defer` have no representation of "the proposed action is a
rotation/short, not a long-entry." Two honest options:

- **2a (code, safe, partial):** In `_build_verdict_from_coordinator`,
  detect a rotation/all-in/short framing from the research question
  (substrings `rotate`, `100%`, `short`, `all-in`) — already available to
  the panel call — and when the coordinator emitted `pass` on such a
  framed question while the risk band is `elevated`-or-worse, escalate to
  `defer`. This converts both `jupiter-jlp-vs-jitosol` rows (PASS→DEFER,
  in-set) and is defensible: on a concentration/rotation call, "reject"
  and "flag the concentration risk" are different answers and the fixture
  wants the latter. **Real, but heuristic** — keying off question
  substrings is brittle and a `software-engineer` should own the exact
  predicate.
- **2b (the deeper fix, NOT code-only):** `jupiter-lst-rotation-msol`
  (got `act`, want PASS) is *not* reachable by any DEFER escalation — the
  panel needs to actually recognize "points are not cash" and decline.
  That is upstream-of-coordinator reasoning (the fundamental_analyst /
  strategist must surface the speculative-points distinction). No code
  rule in `_build_verdict_from_coordinator` can manufacture a PASS here
  without also breaking legitimately-bullish fixtures. This row is a
  **genuine panel-reasoning gap** and the only honest levers are (i) a
  fixture-targeted persona-prompt constraint for speculative-yield
  framings, accepted as imperfect, or (ii) accepting it as residual
  true-signal error. It should be tracked as its own ticket, separate
  from the code fix.

### What this means for the gate

Fix 1 + Fix 2a are pure code, deterministic, unit-testable against the
persisted `turns[]` in these 5 artifacts with **zero LLM spend** — write
a test that feeds the recorded coordinator/strategist/risk turns through
`_build_verdict_from_coordinator` and asserts the new verdict. That
converts **4 of 6 misses** (`kamino-sol-leverage-entry`,
`drift-jto-perp-short`, both `jupiter-jlp-vs-jitosol`) from outside-set to
in-set on the recorded data — `verdict_accuracy` 0.88 → ~0.96 on this
N=50, comfortably above 0.85. The remaining 2 (`jupiter-lst-rotation-msol`
×2) are a separate panel-reasoning ticket and should not block the gate
re-run if the other 4 land — but they must be tracked, not hidden.

**Lane:** Fix 1 and Fix 2a — coordinator verdict logic, in code
(`_build_verdict_from_coordinator`). Owned by `ai-ml-engineer` for the
rule design; `software-engineer` for the rotation-framing predicate in
Fix 2a. Fix 2b — persona-prompt / fixture-reasoning ticket, `ai-ml-
engineer`, separate sprint item. **No closing-line-contract change. No
parse-code change.**

## Bottom line

All 6 `verdict_accuracy` misses are **Category 1 (reasoning)** — the
closing-line emission and the extraction/mapping code are clean on every
row. The dominant mechanism is two-fold: (1B) the S24 DEFER threshold
over-suppresses `defer` — a lone `unacceptable` risk band cannot escalate;
and (1A) `act`/`pass`/`defer` have no rotation/short-direction model, so
"don't rotate" and "observe a short" both collapse to `pass`. The fix is
**code** in `_build_verdict_from_coordinator` — a risk-veto DEFER
escalation (Fix 1) plus a rotation-framing DEFER escalation (Fix 2a) —
which recovers 4/6 against the recorded data with zero spend. The 2
`jupiter-lst-rotation-msol` rows are a genuine panel-reasoning gap
(speculative points ≠ cash) that no coordinator code rule can fix; track
separately. Do NOT iterate the coordinator prompt for the DEFER threshold
— S24 already proved gpt-4o-mini rounds toward caution on that surface.

## Artifacts referenced

- `tests/eval/live_runs/2026-05-18-s24-defi-rubric-s36-110-shipgate-r{1..5}-10.json`
- `tests/eval/suites/defi_trade_rubric_suite.json` (fixture `why` fields)
- `packages/gecko-core/src/gecko_core/orchestration/trade_panel/__init__.py`
  (`_parse_closing_line`, `_extract_json_block`, `_coerce_verdict_token`,
  `_build_verdict_from_coordinator`)
- `packages/gecko-core/src/gecko_core/orchestration/trade_panel/personas.py`
  (`CLOSING_LINE_PATTERNS`, `ROLE_TASKS`)
- `packages/gecko-core/src/gecko_core/orchestration/trade_panel/_default_prompts.json`
  (coordinator prompt — DEFER threshold clause)
- `docs/eval/2026-05-18-s36-112-hallucination-rootcause.md` (Q4 — the
  prior partial diagnosis this completes)
