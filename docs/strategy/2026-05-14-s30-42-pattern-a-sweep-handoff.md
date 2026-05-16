# S30-#42 — Pattern A cleanup sweep (handoff)

**Branch:** `s30/pattern-a-sweep` (cut from `s30/audit-defaults-fix`)
**Predecessor:** S30-#38 (`7f0f507`) routed `scripts/data/audit_existing_chunks.py` through `gecko_core.db.mongo.chunks_collection()`. This sweep mirrors that fix into the three sibling scripts flagged in the ticket.

## What changed

| Script | Before | After |
|---|---|---|
| `scripts/mongo/s20_legacy_revoke.py` | `--db default=os.environ.get("MONGODB_CHUNK_DB", "gecko_rag")`, `--collection default="chunks"` | `--db default=None`, `--collection default=None`; runtime resolves via `chunk_db_name()` / `CHUNKS_COLLECTION` |
| `scripts/mongo/s20_rag02_filterable_index.py` | Same inline literal | Same canonical-accessor fix |
| `scripts/evict_academic_gecko_chunks.py` | Invented `MONGODB_DB_NAME` env alongside `MONGODB_CHUNK_DB` (two aliases for one concept) | `MONGODB_DB_NAME` alias removed; defaults route through `chunk_db_name()` only |

## Env var landscape

**Before sweep:**
- `MONGODB_CHUNK_DB` — honored by all four chunk-touching scripts (and `gecko_core.db.mongo`).
- `MONGODB_DB_NAME` — honored *only* by `evict_academic_gecko_chunks.py`, with `MONGODB_CHUNK_DB` as a fallback alias. Pure Pattern A drift.
- `MONGO_DB` — was honored by the previous (pre-S30-#38) audit script. Already killed in `7f0f507`.

**After sweep:**
- `MONGODB_CHUNK_DB` — the single source of truth, read exclusively by `gecko_core.db.mongo.chunk_db_name()`. Every chunk-touching script now defers to that accessor.
- `MONGODB_DB_NAME` — dead. Documented breaking change in the `evict_academic_gecko_chunks.py` docstring.
- `MONGO_DB` — dead (already).

`MONGODB_URI` is unchanged — still read directly via `os.environ.get` in each script, since that's the connection string (orthogonal to chunk-DB Pattern A).

## Was `evict_academic_gecko_chunks.py` actually Pattern A?

Yes, unambiguously. The script's operation contract is "delete chunks whose `source_url` matches BLOCKLIST_PATTERNS" — it operates on the same `chunks` collection that production reads/writes. The `MONGODB_DB_NAME` alias was a drift introduced when the script was added; there is no legacy academic-Gecko separate DB. The canonical accessor applies cleanly and the env alias was killed.

## Tests

- 4 new tests in `tests/scripts/test_pattern_a_db_defaults.py` (one per script + one canonical-accessor contract check). 5 test functions total, all under 3 fixtures each per the lighter-tests convention.
- 21/21 pass when run together with the existing `test_s20_legacy_revoke.py`, `test_s20_rag02_filterable_index.py`, `test_evict_academic_gecko_chunks.py` suites — none of the prior tests asserted defaults, so nothing broke.
- `mypy` + `ruff check` + `ruff format` clean on all touched files.

## Other Pattern A violations spotted while sweeping

Quick `grep -rn 'MONGODB_CHUNK_DB' scripts/ packages/` audit done during the sweep — no other inline `os.environ.get("MONGODB_CHUNK_DB", ...)` redeclarations remain in `scripts/` after this commit. The canonical accessor in `gecko_core.db.mongo` is the only reader. (`MONGODB_URI` reads in script CLIs are intentional — different concept, connection string not DB name.)

One adjacent observation worth a follow-up ticket (NOT part of this sweep): `scripts/mongo/s20_rag02_filterable_index.py` also accepts `MONGO_URI` as a fallback alongside `MONGODB_URI`. That's a connection-string alias, not a chunk-DB alias, so it's out of scope for Pattern A here — but the same "two env names for one concept" smell exists. Recommend a follow-up to consolidate on `MONGODB_URI` only.
