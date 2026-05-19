# S36-WS1 — Hallucination-score diagnosis (`hallucination_score` lock blocker)

**Date:** 2026-05-18
**Ticket:** s36-#105 (diagnosis only — no code change, no rubric re-run, no spend)
**Branch:** `s36/hallucination-fix`
**Owner:** ai-ml-engineer

## TL;DR

`hallucination_score` is the lone S35 ship-gate blocker (5/6 dimensions
statistically locked at N=30 per #103). It does **not** lock —
per-run pass-rate 0.30 / 0.60 / 0.20, mean 0.367, CI well below the bar.

It does not lock because the panel is **not actually the thing failing
hardest**. The dominant failure mode is a **rubric-artifact**: the judge
scores numeric grounding against a chunk snippet that has been
**double-truncated to ~200 chars**, and for protocol-native API chunks
the first 200 chars are an *endpoint description*, not the numeric
payload. The figure the panel cites is real and came from the API — the
judge just never sees it. Two prior fixes never bound: S29-#33 is a
prompt addendum (prompts don't bind on gpt-4o-mini), and **S31-#49's
code validator was never merged to main** — and even if it were, it
scans a different text than the judge does.

## Evidence base

`#103` N=30 artifacts (committed on `origin/s35/panel-openrouter`, not on
main): `tests/eval/live_runs/2026-05-18-s24-defi-rubric-s35-103-shipgate-r{1,2,3}-10.json`.
`hallucination_score` is binary 0/1 per fixture; the aggregate "mean" is
the pass-rate.

### Per-fixture failure tally (hall=0 means flagged hallucinating)

| Fixture | r1 | r2 | r3 | fails/3 | pattern |
|---|---|---|---|---|---|
| kamino-jlpusdc-entry | 1 | 1 | 1 | 0 | clean |
| kamino-sol-leverage-entry | 1 | 1 | 1 | 0 | clean |
| kamino-jitosol-vault | 0 | 0 | 0 | **3** | structural |
| kamino-usdc-pyusd-roll | 1 | 1 | 0 | 1 | noisy |
| drift-sol-perp-long | 0 | 1 | 0 | 2 | noisy |
| drift-jto-perp-short | 0 | 1 | 0 | 2 | noisy |
| jupiter-jlp-vs-jitosol | 0 | 0 | 0 | **3** | structural |
| jupiter-lst-rotation-msol | 0 | 1 | 0 | 2 | noisy |
| jito-mev-tip-band | 0 | 0 | 0 | **3** | structural |
| sanctum-unstake-vs-hold | 0 | 0 | 0 | **3** | structural |

Four fixtures fail all three runs (`kamino-jitosol-vault`,
`jupiter-jlp-vs-jitosol`, `jito-mev-tip-band`, `sanctum-unstake-vs-hold`)
— this is structural, not variance. Three fixtures are genuinely noisy
(1–2/3). Two are reliably clean. **Fixing the four structural fixtures
alone moves pass-rate from ~0.37 to ~0.77 and locks the CI.**

### Classification of the 22 bad rows

From judge-note language:

- **6 — snippet-truncation artifact** (judge note: *"the snippet does
  not visibly confirm these numbers"*, *"the citations only describe
  the endpoint... no actual numeric values are present in the snippet
  text"*). The number is in the API response; the judge never sees it.
- **4 — misattribution** (a real number cited under the wrong `[n]`,
  or non-target APYs conflated into the target metric — e.g.
  `kamino-jitosol-vault` cites ezSOL/jagSOL staking yields as if they
  were the JitoSOL-SOL vault yield).
- **3 — genuine invented number** (a figure with no source at all —
  e.g. `sanctum-unstake-vs-hold` r1's `"26.12% APY during the October
  2025 flash-crash"` cited as `[4]` when only 2 evidence citations
  exist).
- **6 — other / borderline** (judge hedged: a falsifier price level or
  qualitative-claim framing it half-counted as a hallucination, e.g.
  drift "dovish Fed pivot" framing).

So roughly **6 truncation-artifact / 4 misattribution / 3 true
invented-number** among the cleanly classifiable rows. The wedge is
*not* a panel that fabricates — it is the harness showing the judge
less text than the panel actually grounded against.

## Ranked diagnosis

### Cause 1 (highest confidence) — Chunk snippet is double-truncated; the judge grades against a 200-char endpoint *description*, not the numeric payload

The trade-panel builds each `Citation.snippet` at
`packages/gecko-core/src/gecko_core/orchestration/trade_panel/__init__.py:1521`:
`_CITATION_SNIPPET_LIMIT = 240` → `snippet = snippet[:240]`. The rubric
scorer then truncates *again* at
`tests/eval/scripts/score_defi_trade_rubric.py:647`:
`"snippet": (c.snippet or "")[:200]`. Net: the judge sees ≤200 chars.

Across the 356 citations in #103, **283 snippets are exactly 240 chars**
— i.e. hard-truncated. Protocol-native chunks lead with a mandatory
provenance header (`protocol_native.py:445` `_provenance_header`) plus
an endpoint description, and the 200-char window is exhausted before any
number appears. Concrete examples from the `turns[]` (r2):

- `jito-mev-tip-band` cite `[1]` full snippet (240 ch, **0 numeric
  tokens**): *"Protocol-native API: jito/jito-mev-tip-floor-live ...
  Endpoint description: Jito tip-floor percentiles (P25/P50/P75/P95/P99)
  for bundle landing — current snapshot. Direct lamport figures that
  ground"* — cut off mid-sentence, exactly before the numbers. The
  panel cites `8.8785e-06 SOL` / `5.04575e-06 SOL`; the judge correctly
  says these "are not present in the snippet" → hall=0.
- `jupiter-jlp-vs-jitosol` cite `[1]` (240 ch, **0 numeric tokens**):
  description of the LST snapshot endpoint, prices truncated off. Panel
  cites `$854.93M` / `$177.63M` liquidity → flagged.
- `sanctum-unstake-vs-hold` cites `[3]`/`[4]` (240 ch, **0 numeric
  tokens**): reserve/APY-endpoint *descriptions*. Panel cites
  `0.0% APY` / `over 600k SOL` → flagged.
- **Contrast:** `drift-sol-perp-long` cites `[1-3]` (240 ch, **5
  numeric tokens each** — `funding rate 0.000575916, oracle price TWAP
  82.504218 ...`). Drift's renderer happens to put the numbers inside
  the first 240 chars, so its figures *survive* truncation — and drift
  fixtures are exactly the ones that flip 0→1 between runs. The
  difference between a structural-fail fixture and a noisy fixture is
  literally whether the number lands before or after char 240.

This is the single biggest lever. It is not a panel bug; it is a
harness + chunk-rendering bug.

### Cause 2 — S31-#49's hall validator was never merged; the panel path runs no code-side grounding check at all

`hall_validator.py` and its wire-in exist **only on the unmerged branch
`s31/hall-validator`** (commits `2a8b183`, `4d32a1d`). `git merge-base
--is-ancestor 2a8b183 origin/main` → false. `git grep
GECKO_HALL_VALIDATOR_ENABLED origin/main` → nothing. The #103 run
(descended from main via `panel-openrouter`) executed **zero** code-side
hallucination enforcement. The S31 ticket's own commit message says "the
fix lives in CODE not in prompts" — correct instinct, but the code never
shipped.

Worse, even if merged as-is it would **still miss this**: its
`validate_against_chunks` scans `_chunk_text(chunk)` =
`chunk["text"]/["content"]/["snippet"]` — the *full* chunk dict passed
to `run_trade_panel_with_retrieval`. For protocol-native chunks the full
text *does* contain the numbers, so the validator would mark the claim
**sourced** and pass it through — while the judge, seeing only the
200-char snippet, still scores hall=0. The validator and the judge would
disagree because they read different text. A merged-as-is S31-#49 does
not fix the gate.

### Cause 3 — S29-#33's prompt addendum is present on every voice but does not bind

The "QUANTITATIVE GROUNDING" addendum is in `_opening_prompt`
(`trade_panel/__init__.py:172-186`), the shared seed prompt for all 7
voices — so coverage is not the problem. It instructs each voice to
either quote the exact substring `[3: 'tvl: 1505498759']` or prefix the
claim `unsourced:`. The #103 turns show **no voice ever emits an inline
quoted substring and no voice ever writes `unsourced:`** — they cite
`[1]`/`[2]` bare and state the number. Per repo memory
`feedback_prompt_iteration_plateau`, gpt-4o-mini does not reliably bind
to instruction-style prompt addenda; this is the same plateau S31-#49
already identified and tried (and failed, by not merging) to escape.

### Cause 4 — misattribution: voices conflate non-target metrics into the target

A real-but-separable bug, ~4 of 22 rows. `kamino-jitosol-vault` cites
ezSOL / jagSOL / "unknown mint" staking APYs from the Kamino
`staking-yields` list endpoint as if they were the JitoSOL-SOL vault
yield. The chunks are real, the numbers are real, but the entity
binding is wrong. The judge correctly calls this a hallucination
pattern. This is *not* fixed by the truncation fix — it needs an
entity-match check (does the cited chunk name the asset/pool the
question asks about?).

### Cause 5 — voice concentration

Hallucinating figures are **spread, not concentrated** in
`fundamental_analyst`. In #103: `jito-mev-tip-band` r2 — the judge names
`technical_analyst`, `sentiment_analyst`, `fundamental_analyst` AND
`strategist` all citing the same ungrounded lamport figures.
`sanctum-unstake-vs-hold` — `sentiment_analyst` + `bull_bear_debater`.
`drift-sol-perp-long` — `strategist` + `bull_bear_debater`. The opening
seed prompt is shared, so every voice inherits the same (non-binding)
addendum and the same truncated chunks — a per-voice persona fix would
not help. fundamental_analyst was the *prior* (S29) offender; that
concentration is gone.

## Proposed fix for S36-WS2 — code enforcement, not prompts

**Highest-confidence single fix: a grounding-or-abstain numeric gate
that runs in code, on the panel path, against the SAME text the judge
sees.** This is one change with two coordinated halves:

1. **Stop truncating the numeric payload.** Either (a) raise
   `_CITATION_SNIPPET_LIMIT` and the scorer's `[:200]` to a budget that
   reliably contains the numbers (the protocol-native chunks are short
   — the full text is typically <600 chars), or, cleaner, (b) make the
   protocol-native renderer **lead with the numeric payload** and put
   the endpoint-description prose last, so the figure survives any
   truncation window. (b) is preferred — it is bounded, costs no extra
   judge tokens, and `data-engineer` owns the renderer with
   `ai-ml-engineer` reviewing the prose shape. The provenance header
   (S33-#80) can move to a trailing line; it is display-only.

2. **Merge + fix + bind a numeric grounding validator.** Take
   `hall_validator.py` off `s31/hall-validator`, but change it from
   *soft-redact-after-the-fact* to a **hard grounding-or-abstain gate**:
   - It must scan claims against the **exact snippet text the judge
     receives** (the post-truncation `snippet`), not the full chunk —
     otherwise validator and judge disagree (Cause 2). If fix (1b)
     guarantees numbers are in-window, full-chunk vs snippet converge
     and this is moot; if not, the validator must use the snippet.
   - Unsourced numeric claims are not redacted with a cosmetic marker —
     they are **stripped or replaced with a qualitative phrasing**
     before the verdict is finalized, and the validator additionally
     **adds an entity-match check** (Cause 4): a numeric claim is only
     "grounded" if the cited chunk's snippet contains both the value
     *and* the target asset/pool token.
   - Run it unconditionally on `run_trade_panel_with_retrieval` (drop
     the env-flag default-on escape hatch, or keep it only for eval
     A/B), so the production path and the eval path agree.

Why code, not prompt: S29-#33 proved a prompt addendum present on every
voice does not bind on gpt-4o-mini (Cause 3), and `feedback_prompt_
iteration_plateau` is explicit that grounding logic belongs in code. The
panel will keep emitting plausible numbers; the only reliable lever is a
deterministic post-generation gate that removes any number the cited
snippet cannot support. A code gate is also self-consistent with the
judge in a way no prompt can be — if the validator reads the same 200
chars the judge reads, a row that passes the validator passes the judge
by construction.

**Sequencing for WS2:** ship fix (1b) first — it alone is expected to
clear the four structural fixtures (the figures become judge-visible)
and is the lowest-risk, no-LLM, no-cost change. Re-run N=30. If the CI
still does not lock on the noisy/misattribution residue, add the
validator (fix 2) with the entity-match check. Do not touch personas.

## Out of scope / follow-ups

- The #103 artifacts are committed on `origin/s35/panel-openrouter`, not
  on main — eval artifacts should land on main or a dedicated
  eval-results branch so diagnosis does not require branch archaeology.
- The rubric still grades hallucination as binary 0/1; a partial-credit
  shape (fraction of numeric claims grounded) would lower CI half-width
  at the same N. Track separately — not a WS2 blocker.
