# S31-#55 — Canonical Literal Drift Test (handoff)

**Branch:** `s31/literal-drift-test`
**File:** `tests/test_canonical_literal_drift.py` (~370 LOC, four `test_*` functions)
**Runtime:** <1s. Included in `make test-fast` (no markers; default selection).

## Why this test exists

Three silent bugs this session shared one root cause: canonical `Literal`
definitions drifted from sibling artifacts (fixtures, retrieval admit-lists,
ad-hoc env-var lookups). Per CLAUDE.md "Pattern A — parallel `Literal`
redeclarations", the fix is a single CI-time guard.

This is the Pattern A enforcement layer for non-SQL drift surfaces — the
existing `test_payment_mode_consistency.py` and `test_provider_kind_consistency.py`
cover Python↔SQL; this file covers Python↔JSON-fixtures and Python↔scripts.

## Four dimensions covered

| # | Test function | Catches | Bug ref |
|---|---|---|---|
| 1 | `test_fixture_vertical_in_canonical_literal` | Eval-suite fixture `vertical` not in `Vertical` Literal | #54 (`infra`) |
| 2 | `test_fixture_provider_kinds_in_canonical_literal` | Fixture `must_cite_provider_kinds` entries not in `ProviderKind` | first surfaced S31-#55 (`market_data`) |
| 3 | `test_vector_search_admit_list_covers_provider_kinds` | `$vectorSearch.filter.provider_kind.$in` missing canonical kinds | #43 (`bazaar_manifest`) |
| 4 | `test_scripts_mongo_env_vars_are_canonical` | `scripts/` using Mongo env-var aliases | #38, #42, #48 |

## Canonical Mongo env-var set (Dimension 4)

The test's `CANONICAL_MONGO_ENV_VARS`:

- `MONGODB_URI` — connection string
- `MONGODB_CHUNK_DB` — vector-store database name
- `MONGODB_TRADE_DB` — trade-agent runtime database (separate by design,
  per `project_mongo_cutover_no_backfill`)

Any other `MONGO*` / `MONGODB*` key referenced in `scripts/` triggers a
named alias error. Known aliases are pre-listed in `_KNOWN_MONGO_ALIASES`
so the error message says "this is `MONGO_URI` — replace with `MONGODB_URI`"
rather than "unknown name".

## First-run drift caught (must fix before merge to main)

The test FAILS on first run, exactly as designed. Surfaces:

**Dimension 1 (5 violations, 1 file):**
- `defi_trade_rubric_suite.json` — `lending` (1), `perps` (2), `infra` (1), `lst` (1)
- Either expand `Vertical` Literal to model defi sub-verticals, OR remap
  fixture entries to canonical values (e.g. `lending` → `dex` or `unknown`).
- **Follow-up ticket: S31-#56** (taxonomy expansion vs. fixture remap decision).

**Dimension 2 (10 violations, 1 file):**
- `defi_trade_rubric_suite.json` — every case cites `market_data` which is
  not in `ProviderKind`. The `market_data.py` adapter lives on a sibling
  worktree; this is genuinely missing from this branch.
- **Follow-up ticket: S31-#57** (land `market_data` ProviderKind + adapter, OR
  remove from fixtures if the adapter is being abandoned).

**Dimension 3 (0 violations):**
- Trade panel's `$vectorSearch.filter` currently has no `provider_kind.$in`
  admit-list — it filters on `vertical` + `metadata.deprecated` only and
  trusts the downstream `$match` stage for protocol cross-cutting. The test
  is **vacuous-pass** in this state. The instant an admit-list is added
  anywhere in `trade_panel/__init__.py`, the test enforces completeness.

**Dimension 4 (3 violations, 3 files):**
- `scripts/evict_academic_gecko_chunks.py:165` — `MONGODB_DB_NAME`
- `scripts/mongo/bootstrap_trade_agent.py:184` — `MONGO_URI`
- `scripts/mongo/s20_rag02_filterable_index.py:292` — `MONGO_URI`
- All are 1-line edits. Replace with `MONGODB_CHUNK_DB` / `MONGODB_URI`.
- **Follow-up ticket: S31-#58** (3-line script renames + verify the affected
  scripts still wire to the right DB after the rename).

## Extending for a new shared Literal

Pattern is uniform across dimensions 1+2 — replicate either function:

1. Add the new canonical tuple import at the top of the test module.
2. Add a `test_fixture_<field>_in_canonical_literal` function that walks
   `_iter_fixture_items()`, reads the new fixture field, formats an
   `"X is in <fixture>[<idx>] but not in <Literal>"` message.
3. If the new concept needs a `KNOWN_*_EXCEPTIONS` escape hatch, add a
   dict next to the existing two with a comment naming the follow-up
   ticket; keep it **empty by default** — exceptions hide drift.

For new admit-list shapes (Dimension 3), the AST walker `_extract_provider_kind_admit_lists`
hard-codes `$vectorSearch.filter.<key>.$in`. To add a new admit-list field,
copy that function and adjust the key chain. Don't generalize prematurely —
each admit-list has slightly different semantics (post-filter `$match`,
projection-time guard, etc.) and a generic walker would lie about coverage.

For new env-var families (Dimension 4), add a parallel `CANONICAL_<FAMILY>_ENV_VARS`
frozenset + a sibling `test_scripts_<family>_env_vars_are_canonical` that
gates on the family prefix. Don't merge into the Mongo test — each family
has its own canonicality regime.

## Hard constraints honored

- Branch: `s31/literal-drift-test`. No push to main.
- Budget: $0. Pure AST + JSON parsing, no live spend.
- Failure mode: loud — every violation names the file, line/index, the
  offending value, and the canonical set it should have matched.
- Light tests: 4 functions, no fixtures, no mocks, no monkeypatch. ~5s wall.
