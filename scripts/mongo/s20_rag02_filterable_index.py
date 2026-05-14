"""S20-RAG-02 — migrate ``chunks_vector`` to a filterable compound index.

What this index does
--------------------
Atlas Search's ``vectorSearch`` index type accepts a ``filter`` array of
field paths declared at index-build time. Queries can then push an
equality / membership / not-equal filter clause into the ``$vectorSearch``
stage's ``filter`` field, and Atlas filters those documents *before* the
ANN graph traversal — i.e. the vector-search step only walks the subset
of vectors matching the filter, instead of running ANN over everything
and dropping non-matches afterward.

Why filterable beats post-filter
--------------------------------
Without filterable fields, the only way to scope a vector search by
``(vertical, category, source)`` is a ``$match`` stage *after*
``$vectorSearch``. That post-filter:

1. Wastes ANN budget. ``$vectorSearch`` returns ``limit`` rows total
   across all tenants; if you wanted top-K within neobank but 90% of
   the rows are dex, you get a near-empty slate.
2. Has no recovery. Increasing ``numCandidates`` to compensate is
   superlinear in cost and still doesn't guarantee enough surviving
   matches for the requested ``limit``.

Pre-filtering past the ANN stage is purpose-built for the multi-tenant
shape S20 ships (single collection, ~11 verticals × 8 categories =
~88 logical cells; queries scope to one cell).

Idempotency
-----------
Re-running this script is safe. With ``--apply`` and no ``--rebuild``:

- if the live index has identical filter fields → no-op, exit 0.
- if the live index is missing filter fields → reject with non-zero
  exit and instruct the operator to pass ``--rebuild`` (which drops +
  re-creates the index, briefly blocking reads).

Dry-run is the default. ``--apply`` is required to issue any Atlas
mutations.

Rollback
--------
The index is reversible — the previous (unfiltered) definition is
checked into git history at ``infra/atlas/index_definitions/`` (when
that directory ships) or recoverable from Atlas's index history. To
roll back: drop ``chunks_vector`` and re-create with the old definition
shape (no ``filter``-typed entries).

pymongo / Atlas API
-------------------
Modern pymongo (>= 4.5) ships ``collection.create_search_index`` /
``update_search_index`` / ``list_search_indexes``. If the workspace
runs an older pymongo, equivalent Atlas Admin API curl invocations
exist — see the Atlas docs section "Manage Atlas Search Indexes" for
the REST shape. We rely on the pymongo helpers here.

S31-#48 Pattern A note (env var consolidation)
----------------------------------------------
The ``--mongodb-uri`` default and the ``--db`` default route through
``gecko_core.db.mongo.mongo_uri()`` and ``chunk_db_name()`` — the
single source of truth for the chunk-store env-var contract. The
canonical accessor tolerates both ``MONGODB_URI`` (preferred) and
``MONGO_URI`` (legacy alias retained for cross-store back-compat with
``cache.mongo`` + ``orchestration.transcripts``); adding or removing
an alias is now a one-file edit. Error messages and help text refer to
``MONGODB_URI`` only — operators should prefer the canonical name.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass
from typing import Any

from gecko_core.db.mongo import (
    CHUNKS_COLLECTION,
    CHUNKS_VECTOR_FILTER_FIELDS,
    CHUNKS_VECTOR_LEGACY_FILTER_FIELDS,
    SEARCH_INDEX_NAME,
    VECTOR_INDEX_NAME,
    chunk_db_name,
    mongo_uri,
)
from gecko_core.db.mongo_chunks import MONGO_VECTOR_DIM_EXPECTED

logger = logging.getLogger("scripts.mongo.s20_rag02_filterable_index")


# ---------------------------------------------------------------------------
# Index definitions — built from the canonical filter-field tuple in
# gecko_core.db.mongo so doctor reads from the same source. Pattern A.
# ---------------------------------------------------------------------------


def build_vector_index_definition(
    *,
    dim: int = MONGO_VECTOR_DIM_EXPECTED,
    similarity: str = "cosine",
) -> dict[str, Any]:
    """Atlas Search ``vectorSearch`` index definition with filterable fields.

    The legacy filter fields (``session_id``, ``project_id``,
    ``captured_at``) are kept so the existing match_chunks paths still
    pre-filter at the ANN stage.
    """
    fields: list[dict[str, Any]] = [
        {
            "type": "vector",
            "path": "embedding",
            "numDimensions": dim,
            "similarity": similarity,
        }
    ]
    for path in (*CHUNKS_VECTOR_FILTER_FIELDS, *CHUNKS_VECTOR_LEGACY_FILTER_FIELDS):
        fields.append({"type": "filter", "path": path})
    return {
        "name": VECTOR_INDEX_NAME,
        "type": "vectorSearch",
        "definition": {"fields": fields},
    }


def build_text_index_definition() -> dict[str, Any]:
    """Atlas Search ``search`` (BM25) index definition.

    Atlas Search text indexes have a different filter surface than
    vector indexes — fields are declared as part of ``mappings.fields``
    with type ``token`` (exact match) or ``string`` (analyzed). Token
    fields are filterable inside ``$search.compound.filter``.
    """
    field_defs: dict[str, Any] = {
        "text": {"type": "string"},
    }
    for path in (*CHUNKS_VECTOR_FILTER_FIELDS, *CHUNKS_VECTOR_LEGACY_FILTER_FIELDS):
        # ``metadata.deprecated`` is dotted — Atlas accepts dotted keys
        # for nested fields in mappings.
        field_defs[path] = {"type": "token"}
    return {
        "name": SEARCH_INDEX_NAME,
        "type": "search",
        "definition": {
            "mappings": {
                "dynamic": False,
                "fields": field_defs,
            }
        },
    }


# ---------------------------------------------------------------------------
# Diff / idempotency
# ---------------------------------------------------------------------------


def _existing_filter_paths(idx_def: dict[str, Any]) -> set[str]:
    """Pull the set of ``filter``-typed paths out of a live index def."""
    for key in ("latestDefinition", "definition"):
        defn = idx_def.get(key)
        if not isinstance(defn, dict):
            continue
        fields = defn.get("fields")
        if not isinstance(fields, list):
            continue
        paths: set[str] = set()
        for f in fields:
            if isinstance(f, dict) and f.get("type") == "filter":
                p = f.get("path")
                if isinstance(p, str):
                    paths.add(p)
        return paths
    return set()


@dataclass(frozen=True)
class IndexDiff:
    """Difference between live and desired index definitions."""

    name: str
    exists: bool
    live_filters: set[str]
    desired_filters: set[str]

    @property
    def up_to_date(self) -> bool:
        return self.exists and self.desired_filters.issubset(self.live_filters)

    @property
    def needs_rebuild(self) -> bool:
        # The index exists but its filter set is wrong — we can only
        # change filterable fields by dropping + re-creating.
        return self.exists and not self.up_to_date

    def render(self) -> str:
        if not self.exists:
            return f"[{self.name}] missing — will create"
        if self.up_to_date:
            return f"[{self.name}] up-to-date"
        missing = sorted(self.desired_filters - self.live_filters)
        extra = sorted(self.live_filters - self.desired_filters)
        bits = []
        if missing:
            bits.append(f"missing={missing}")
        if extra:
            bits.append(f"extra={extra}")
        return f"[{self.name}] drift: {' '.join(bits)} — requires --rebuild"


def diff_index(
    coll: Any,
    *,
    name: str,
    desired_filters: set[str],
) -> IndexDiff:
    live = {i["name"]: i for i in coll.list_search_indexes()}
    if name not in live:
        return IndexDiff(
            name=name, exists=False, live_filters=set(), desired_filters=desired_filters
        )
    return IndexDiff(
        name=name,
        exists=True,
        live_filters=_existing_filter_paths(live[name]),
        desired_filters=desired_filters,
    )


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


def _drop_index(coll: Any, name: str) -> None:
    # pymongo >= 4.5
    coll.drop_search_index(name)


def _create_index(coll: Any, definition: dict[str, Any]) -> None:
    coll.create_search_index(definition)


def apply_index(
    coll: Any,
    *,
    definition: dict[str, Any],
    rebuild: bool,
) -> str:
    """Reconcile a single Atlas Search index. Returns a human-readable status."""
    name = definition["name"]
    desired = {
        f["path"]
        for f in definition["definition"].get("fields", [])
        if isinstance(f, dict) and f.get("type") == "filter"
    } | {
        # text-index path — pull token fields too (only for type=search defs).
        path
        for path, field in (
            definition["definition"].get("mappings", {}).get("fields", {}) or {}
        ).items()
        if isinstance(field, dict) and field.get("type") == "token"
    }
    diff = diff_index(coll, name=name, desired_filters=desired)
    if diff.up_to_date:
        return f"{name}: up-to-date"
    if not diff.exists:
        _create_index(coll, definition)
        return f"{name}: created"
    if not rebuild:
        raise RuntimeError(
            f"{name}: drift detected ({sorted(desired - diff.live_filters)} missing) "
            "— pass --rebuild to drop + re-create"
        )
    _drop_index(coll, name)
    _create_index(coll, definition)
    return f"{name}: rebuilt"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _open_collection(uri: str, db_name: str, coll_name: str) -> Any:
    from pymongo import MongoClient

    client: MongoClient[dict[str, Any]] = MongoClient(uri, serverSelectionTimeoutMS=5000)
    return client[db_name][coll_name]


def run(argv: list[str] | None = None, *, collection: Any = None) -> int:
    """Entry point. ``collection`` injectable for tests."""
    parser = argparse.ArgumentParser(
        prog="s20_rag02_filterable_index",
        description="Apply the filterable compound vector index for chunks_vector.",
    )
    grp = parser.add_mutually_exclusive_group()
    grp.add_argument("--dry-run", action="store_true", default=True)
    grp.add_argument("--apply", action="store_true", default=False)
    parser.add_argument("--rebuild", action="store_true", default=False)
    # S31-#48 Pattern A — route the URI default through the canonical
    # accessor in ``gecko_core.db.mongo``. The accessor still tolerates the
    # ``MONGO_URI`` alias for cross-store back-compat, but the contract
    # lives in one place — adding/removing an alias touches one file.
    parser.add_argument(
        "--mongodb-uri",
        default=mongo_uri(),
    )
    # S30-#42: defaults None — route through gecko_core.db.mongo (Pattern A).
    parser.add_argument(
        "--db",
        default=None,
        help=(
            "Database override. Default: gecko_core.db.mongo.chunk_db_name() "
            "(respects $MONGODB_CHUNK_DB; falls back to 'gecko_rag')."
        ),
    )
    parser.add_argument(
        "--collection",
        default=None,
        help=(
            f"Collection override. Default: {CHUNKS_COLLECTION!r} "
            "(gecko_core.db.mongo.CHUNKS_COLLECTION)."
        ),
    )
    args = parser.parse_args(argv)

    apply_mode = bool(args.apply)
    # argparse default of dry_run=True is fine — apply flips us out.

    vector_def = build_vector_index_definition()
    text_def = build_text_index_definition()

    desired_vector_filters = set(CHUNKS_VECTOR_FILTER_FIELDS) | set(
        CHUNKS_VECTOR_LEGACY_FILTER_FIELDS
    )
    desired_text_filters = set(CHUNKS_VECTOR_FILTER_FIELDS) | set(
        CHUNKS_VECTOR_LEGACY_FILTER_FIELDS
    )

    if not apply_mode:
        print("=== DRY RUN — no Atlas API calls will be made ===")
        print(f"vector index '{VECTOR_INDEX_NAME}' definition:")
        print(json.dumps(vector_def, indent=2, default=str))
        print(f"text index '{SEARCH_INDEX_NAME}' definition:")
        print(json.dumps(text_def, indent=2, default=str))
        if collection is not None:
            v_diff = diff_index(
                collection, name=VECTOR_INDEX_NAME, desired_filters=desired_vector_filters
            )
            t_diff = diff_index(
                collection, name=SEARCH_INDEX_NAME, desired_filters=desired_text_filters
            )
            print("--- diff vs live ---")
            print(v_diff.render())
            print(t_diff.render())
        else:
            print(
                "(no collection injected — run with --apply against a real cluster to see live diff)"
            )
        return 0

    # Apply mode.
    if collection is None:
        if not args.mongodb_uri:
            print(
                "ERROR: --apply requires MONGODB_URI in env or --mongodb-uri",
                file=sys.stderr,
            )
            return 2
        # Pattern A: resolve via canonical accessor when overrides absent.
        db_name = args.db if args.db is not None else chunk_db_name()
        coll_name = args.collection if args.collection is not None else CHUNKS_COLLECTION
        collection = _open_collection(args.mongodb_uri, db_name, coll_name)

    try:
        v_status = apply_index(collection, definition=vector_def, rebuild=args.rebuild)
        t_status = apply_index(collection, definition=text_def, rebuild=args.rebuild)
    except RuntimeError as exc:
        print(f"REJECTED: {exc}", file=sys.stderr)
        return 3
    print(v_status)
    print(t_status)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(run())
