# S31-#48 — Pattern A finishing sweep (chunk-store env-var contract)

**Branch:** `s31/pattern-a-finishing` (cut from `s30/pattern-a-sweep`)
**Scope:** 2 file migrations + 2 light unit tests + 1 canonical-accessor enhancement.
**Budget:** $0 — refactor only.
**Status:** shipped.

## Context

#42 swept inline `MONGODB_CHUNK_DB` lookups across three chunk-store
scripts. The defensive scan that closed #42 surfaced two remaining
violations of the Pattern A rule ("one canonical accessor per shared
concept" — CLAUDE.md `### Recurring patterns → Pattern A`):

1. `packages/gecko-mcp/src/gecko_mcp/doctor.py::check_chunk_store` —
   inlined both `MONGODB_URI`/`MONGO_URI` lookup AND
   `MONGODB_CHUNK_DB` default. Three env-var contract decisions
   declared inline.
2. `scripts/mongo/s20_rag02_filterable_index.py` — `--mongodb-uri`
   default inlined `os.environ.get("MONGODB_URI") or
   os.environ.get("MONGO_URI")`, redeclaring the dual-alias contract
   that the canonical accessor already encapsulates.

## What changed

### `packages/gecko-core/src/gecko_core/db/mongo.py` (canonical accessor enhancement)

`mongo_uri()` and `chunk_db_name()` now accept an optional `env`
mapping. Doctor passes its `environ=` test override directly to the
accessor instead of redeclaring the env-var contract inline. Same
shape as `os.environ.get` — the accessor reads from the passed
mapping when provided, falls back to `os.environ` otherwise.

This is the same enhancement pattern #42 used to expand
`chunk_db_name()` from a 1-line function into the single source of
truth. Now the URI side matches.

### `packages/gecko-mcp/src/gecko_mcp/doctor.py::check_chunk_store`

Before:

```python
uri = env.get("MONGODB_URI") or env.get("MONGO_URI")
if not uri or uri == "__unset__":
    ...
db_name = env.get("MONGODB_CHUNK_DB", "gecko_rag")
```

After:

```python
from gecko_core.db.mongo import chunk_db_name, mongo_uri

uri = mongo_uri(env)
db_name = chunk_db_name(env)
if not uri:
    ...
```

The `__unset__` SSM sentinel + the `MONGODB_CHUNK_DB` default both
move into the canonical accessor.

### `scripts/mongo/s20_rag02_filterable_index.py`

Imports `mongo_uri` from `gecko_core.db.mongo` and uses it as the
`--mongodb-uri` argparse default. Removes the inline dual-alias
lookup. Removes the now-unused `import os`. Adds a module-docstring
note under "S31-#48 Pattern A note (env var consolidation)"
documenting the canonical-accessor contract.

Error-message text updated from "MONGODB_URI / MONGO_URI" to
"MONGODB_URI" only — operators should prefer the canonical name. The
accessor still tolerates the `MONGO_URI` alias for cross-store
back-compat (cache/mongo + orchestration/transcripts both still read
it), but new code references only the canonical name.

### `tests/scripts/test_pattern_a_finishing.py`

Four tests, two per fix:

- `test_doctor_chunk_store_routes_uri_through_canonical_accessor` —
  default path: missing URI under `GECKO_CHUNK_STORE=mongo` produces
  the canonical red row. Asserts the accessor symbols are importable.
- `test_doctor_chunk_store_db_override_respects_env_param` — override
  path: `__unset__` sentinel honored by the accessor; `MONGODB_CHUNK_DB`
  override propagates through `chunk_db_name(env)`.
- `test_s20_rag02_uri_default_routes_through_canonical_accessor` —
  default path: `MONGODB_URI` set → `mod.mongo_uri()` returns it;
  switching to `MONGO_URI` alias still works through same accessor.
- `test_s20_rag02_uri_explicit_override_still_works` — override path:
  unset env → default is `None`; CLI `--mongodb-uri` overrides win.

## Verification

```
unset VIRTUAL_ENV
uv run pytest tests/scripts/test_pattern_a_finishing.py -v
# 4 passed in 1.39s

uv run pytest tests/scripts/ tests/mcp/test_doctor_mongo_filters.py tests/mcp/test_doctor_mongo_dim.py
# 67 passed, no regressions

uv run mypy scripts/mongo/s20_rag02_filterable_index.py tests/scripts/test_pattern_a_finishing.py
# Success: no issues found in 2 source files

uv run mypy packages/gecko-core/src/gecko_core/db/mongo.py
# (pre-existing errors in doctor.py at lines 275/287/578, unrelated to edit zone 787-810)

uv run ruff check packages/gecko-core/src/gecko_core/db/mongo.py \
                  scripts/mongo/s20_rag02_filterable_index.py \
                  tests/scripts/test_pattern_a_finishing.py
# All checks passed!
# (Pre-existing ruff error in doctor.py at line 1016 — `secret_redact` if/else
#  — confirmed by `git stash` baseline. Not in our edit zone.)

uv run gecko-mcp doctor   # 3.3s end-to-end
# [PASS] chunk_store:mongo:ping db=gecko_rag
# [PASS] chunk_store:mongo:index:chunks_vector ready
# [PASS] chunk_store:mongo:index:chunks_text ready
# [PASS] chunk_store:mongo:index:chunks_vector:dim dim=1024 ok similarity=cosine
# [PASS] chunk_store:mongo:index:chunks_vector:filters ...
```

The doctor smoke confirms the canonical accessor is wired correctly
end-to-end against the live Atlas cluster.

## 3rd-tier defensive sweep — additional Pattern A violations

The grep that closed #48 surfaced these remaining inline lookups.
**Not** fixed in this PR (out of scope; founder should triage):

- `packages/gecko-core/src/gecko_core/observability/events.py:43` —
  inline `MONGODB_URI` / `MONGO_URI`. Low-risk; same alias contract
  as the canonical accessor. Future cleanup.
- `packages/gecko-core/src/gecko_core/orchestration/transcripts.py:230`
  — inline dual-alias lookup. **Note:** uses `MONGODB_DB` (not
  `MONGODB_CHUNK_DB`) for the cache DB — semantically distinct from
  the chunk store, so the env-var split is intentional, not drift.
- `packages/gecko-core/src/gecko_core/cache/mongo.py:33` — same shape
  as transcripts.py. Same intentional cache-DB split.
- `packages/gecko-core/src/gecko_core/trade_agent/cli.py:48,518` —
  trade-agent runtime inlines the dual-alias lookup. Could route
  through `mongo_uri()` cheaply. Candidate for S31-#49 or rollup.
- `scripts/mongo/s20_legacy_revoke.py:254` — `default=os.environ.get(
  "MONGODB_URI")` drops the alias entirely. Inconsistent with the
  canonical accessor's contract. One-line fix: route through
  `mongo_uri()`. Candidate for next sweep.
- `scripts/evict_academic_gecko_chunks.py:169` — same shape as
  s20_legacy_revoke. Same fix.
- `scripts/bazaar/validate_solana_only.py:98` — error message
  references `MONGODB_DB` as the DB env var for a chunk-store script.
  **Potential drift** — chunks should use `MONGODB_CHUNK_DB`. Worth a
  closer read in a follow-up.
- `scripts/data/purge_garbage_chunks.py:306` — references `MONGODB_URI`
  in an error string; verify the actual env lookup is canonical.
- `scripts/mongo/bootstrap_trade_agent.py:184` — inline dual-alias.

**Recommendation:** roll up the remaining script-side inline lookups
into a single S31-#49 PR ("route remaining chunk-store scripts +
trade-agent CLI through `mongo_uri()`"). The canonical-accessor
infrastructure is now in place; the rest is mechanical.

## Hard constraints honored

- Branch: `s31/pattern-a-finishing` (worktree-isolated) — cut from
  `s30/pattern-a-sweep`.
- Budget: $0 — no network calls; tests use fakes.
- No new env-var aliases introduced. The `MONGO_URI` legacy alias
  retained only in the canonical accessor (one place); new code
  references `MONGODB_URI` only.
- Pre-commit checklist: ruff format, ruff check, mypy, pytest on
  changed files — all clean (pre-existing failures in unrelated
  zones documented above).
