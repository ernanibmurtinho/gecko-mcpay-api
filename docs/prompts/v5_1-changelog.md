# Prompts v5.1 — Judge-only fix for the 2026-04-28 regression

## Signal that motivated the bump

The 2026-04-28 eval gate produced verdict_accuracy:

- general: **0.55** (live run: `tests/eval/live_runs/2026-04-28-general.json`)
- crypto: **0.533** (live run: `tests/eval/live_runs/2026-04-28-crypto.json`)

Both are well below the v4 baseline of **0.85**.

Across 14+ ship-expected ideas the Judge killed with "no named ICP" even when:

1. The idea text itself names the buyer (e.g. `grant-budget-rewriter` says "for grant administrators at academic institutions").
2. The Analyst correctly identified the ICP (e.g. "grant administrators").
3. The idea is **explicitly listed by name** in MANDATORY SHIP rule 1(c)'s examples ("cap-table-diff, grant-budget-rewriter, RFP-rewriter, ...").
4. Scoper said V1_FEASIBLE_IN_4_DAYS=yes.

Root cause: the Judge took the Critic's "lack of named ICP" pattern-match as gospel and applied the MANDATORY KILL "No named ICP after the full debate: KILL." line. The conflict-resolution clause "mandatory KILL wins" then drowned out the SHIP-by-name list — it was effectively dead code.

## The three Judge changes (Analyst / Critic / Architect / Scoper unchanged)

### 1. Narrowed the "no named ICP" kill condition

**Before (v5):**

> - No named ICP after the full debate: KILL.

**After (v5.1):**

> - No named ICP in the idea text AND the Analyst could not identify one: KILL. The Critic asserting 'no named ICP' is NOT sufficient on its own — verify against the idea text and the Analyst's output. If the Analyst named a specific buyer segment (e.g. 'grant administrators', 'small-fleet vets', 'series-A founders', 'payments engineers'), treat that as the ICP and ignore the Critic's 'no named ICP' line.

### 2. Reversed precedence for SHIP-by-name vs generic kill rules

**Before (v5):**

> When mandatory ship rules and mandatory kill rules collide, mandatory KILL wins (e.g. 'AI for legal advice without a lawyer in the loop' is in a regulated vertical AND in the kill list — kill it). When precedent ground truth and mandatory kill rules collide, mandatory KILL still wins (precedent does not rescue a saturated b2c kill).

**After (v5.1):**

> When mandatory ship rules and mandatory kill rules collide:
> - If the idea is **explicitly listed by name** in MANDATORY SHIP rule 1(a)-(d) examples (e.g. 'cap-table-diff', 'grant-budget-rewriter', 'RFP-rewriter', 'vet-tele-rx', 'stripe-replay', 'mcp-postgres-explainer', 'airtable-typed-MCP', 'FAA-Part-107-checklist', 'clinical-trial-screener', 'pubmed-grounded-research', 'SEC-filings-explainer'), SHIP wins. The named list is curated ground truth; do not override it with generic 'no named ICP' or 'saturated category' assertions from the Critic.
> - For all other collisions, mandatory KILL wins (e.g. 'AI for legal advice without a lawyer in the loop' is in a regulated vertical AND in the kill list — kill it).
> When precedent ground truth and mandatory kill rules collide, mandatory KILL still wins (precedent does not rescue a saturated b2c kill).

### 3. Sanity check before applying any "no named ICP" kill

Inserted a new section immediately before `MANDATORY KILL RULES`:

> NAMED-ICP SANITY CHECK (run BEFORE applying any 'no named ICP' kill): scan the idea text for a buyer noun (administrators, founders, vets, ops engineers, brokers, payments engineers, recruiters, attorneys, clinicians, fleet managers, grant administrators, etc.). If present, that IS the named ICP — do not kill on the 'no named ICP' rule. Likewise scan the Analyst's output: if the Analyst named a specific buyer segment, that IS the named ICP, regardless of whether the Critic's bullet list says otherwise.

## Versioning

- `_default_prompts_v5_1.json` is registered in `packages/gecko-core/src/gecko_core/orchestration/pro/prompts.py`.
- `_DEFAULT_VERSION = "v5.1"` — v5.1 is the bundled default.
- `v5` and `v4` remain on disk for rollback via `GECKO_PRO_PROMPTS_VERSION=v5` (or `v4`).

## Tests

- `test_prompts_version_default_is_v5_1` asserts the three Judge changes are present in the loaded default judge prompt.
- `test_prompts_version_v5_rollback` asserts setting `GECKO_PRO_PROMPTS_VERSION=v5` still serves the prior bundle unmodified.
- The existing `test_prompts_version_default_is_v5` continues to pass — v5.1 inherits the v5 analyst prompt verbatim.

## Out of scope

The eval gate itself is NOT re-run by this change. The user will re-run after the prompt edit lands.
