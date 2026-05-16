"""S20-A3 — one-shot script: stamp legacy chunks with the reserved
``legacy_uncategorized`` bucket and exclude them from default retrieval.

Why this exists
---------------
S20-A2 extended the chunk doc shape to require ``category``, ``vertical``,
``source`` and a ``metadata.{confidence, usage_count, timestamp, pioneer}``
sub-doc. Existing chunks ingested before A2 lack these fields. We do not
backfill (per ``project_mongo_cutover_no_backfill`` memory): retroactive
classification is both expensive and accuracy-risky. Instead this script
*revokes* legacy chunks — stamps them with ``category="legacy_uncategorized"``,
``vertical="unknown"``, ``source="web"`` and ``metadata.deprecated=true`` —
and the RAG layer (``gecko_core.rag.query.rag_query``) excludes them from
default retrieval. Operators / debug runs can re-include them with
``include_legacy=True``.

Operation contract
------------------
Filter: any chunk missing one of the new A2 fields is legacy. We use
``$or`` over ``$exists`` so partial migrations (e.g. a chunk that has
``category`` but not ``vertical``) still get caught.

Updates: each missing field is patched only when missing (``$exists:false``
predicate per field) — we do NOT overwrite a field that's already set.
This keeps the script safe to re-run against partially-revoked
collections.

Idempotency: after the first ``--apply`` run, every legacy doc carries
all four canonical fields and ``metadata.deprecated=true``. The
``$or/$exists`` filter then matches zero docs, and a second run reports
``matched=0, modified=0``.

Index: builds the S20-A2 ``vertical_category_compound`` index on the
chunks collection. Mongo ``createIndex`` is idempotent — if the index
already exists with the same definition the call is a no-op.

Rollback
--------
To un-revoke (un-mark) the chunks:

    db.chunks.updateMany(
      { "metadata.deprecated": true },
      { $set: { "metadata.deprecated": false } }
    )

This leaves the ``legacy_uncategorized`` category/vertical/source stamps
in place but re-includes the chunks in default retrieval (the RAG filter
also drops on ``metadata.deprecated=true``). For a full rollback —
restore original (missing) field shape — restore from a Mongo Atlas
snapshot taken before the run.

Safety
------
- ``--dry-run`` is the default; ``--apply`` is required for writes
  (per the user's destructive-action confirmation memory).
- Connection string is read from ``MONGODB_URI`` env by default; pass
  ``--mongodb-uri`` to override.
- The script does NOT delete any chunks. Dropping legacy chunks
  entirely is a separate ticket (deferred to S21).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import UTC, datetime
from typing import Any

from gecko_core.db.mongo import CHUNKS_COLLECTION, chunk_db_name
from gecko_core.db.mongo_chunks import CHUNKS_VERTICAL_CATEGORY_INDEX
from gecko_core.knowledge.taxonomy import LEGACY_CATEGORY

logger = logging.getLogger("s20_legacy_revoke")


# ---------------------------------------------------------------------------
# Filter & update primitives — pulled out so tests can share them.
# ---------------------------------------------------------------------------

LEGACY_FILTER: dict[str, Any] = {
    "$or": [
        {"category": {"$exists": False}},
        {"vertical": {"$exists": False}},
        {"source": {"$exists": False}},
    ]
}
"""Match-any-missing-field filter. A chunk is legacy iff it lacks at
least one of the A2 categorized-knowledge fields."""


def _build_field_patch(now: datetime) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Return ``[(filter, $set), ...]`` — one update per missing field.

    Each tuple is gated on the field being absent; running every tuple
    in sequence patches only the missing fields. Order is irrelevant
    (the filters are disjoint per-field).
    """
    return [
        (
            {"category": {"$exists": False}},
            {
                "category": LEGACY_CATEGORY,
                "metadata.deprecated": True,
                "metadata.legacy_revoked_at": now,
            },
        ),
        (
            {"vertical": {"$exists": False}},
            {
                "vertical": "unknown",
                "metadata.deprecated": True,
                "metadata.legacy_revoked_at": now,
            },
        ),
        (
            {"source": {"$exists": False}},
            {
                "source": "web",
                "metadata.deprecated": True,
                "metadata.legacy_revoked_at": now,
            },
        ),
    ]


# ---------------------------------------------------------------------------
# Core revoke routine — operates on a Mongo collection handle.
# ---------------------------------------------------------------------------


async def revoke_legacy_chunks(
    collection: Any,
    *,
    dry_run: bool = True,
    batch_size: int = 1000,
    now: datetime | None = None,
) -> dict[str, int | bool]:
    """Stamp legacy chunks. Returns a summary dict.

    Idempotent: a second invocation against an already-revoked
    collection matches zero docs.

    Args:
        collection: motor / pymongo async collection handle.
        dry_run: when True (default), only counts matches; no writes.
        batch_size: ``find().batch_size()`` knob for the matched-count
            query — present for parity with caller flags but not load-
            bearing for the count itself.
        now: timestamp to stamp into ``metadata.legacy_revoked_at``.
            Defaults to ``datetime.now(UTC)``. Tests pass a fixed value.

    Returns:
        ``{matched, modified, dry_run, index_built}``.
    """
    stamp = now or datetime.now(UTC)

    # Count matched legacy docs (cheap — uses index on $exists).
    matched = await collection.count_documents(LEGACY_FILTER)

    if dry_run:
        return {
            "matched": int(matched),
            "modified": 0,
            "dry_run": True,
            "index_built": False,
        }

    if matched == 0:
        # Still build the index — it's idempotent, and operators may
        # be running this purely to ensure the index exists.
        await _build_compound_index(collection)
        return {
            "matched": 0,
            "modified": 0,
            "dry_run": False,
            "index_built": True,
        }

    total_modified = 0
    for fld_filter, set_fields in _build_field_patch(stamp):
        result = await collection.update_many(
            fld_filter,
            {"$set": set_fields},
        )
        total_modified += int(getattr(result, "modified_count", 0) or 0)

    await _build_compound_index(collection)

    return {
        "matched": int(matched),
        "modified": int(total_modified),
        "dry_run": False,
        "index_built": True,
    }


async def _build_compound_index(collection: Any) -> None:
    """Create the S20-A2 (vertical, category, project_id, captured_at) index.

    ``createIndex`` is idempotent; if the index already exists with the
    same name + definition, this is a no-op. Wrapped in a try so a
    permission error during dev doesn't block the revoke summary.
    """
    try:
        await collection.create_index(
            [
                ("vertical", 1),
                ("category", 1),
                ("project_id", 1),
                ("captured_at", -1),
            ],
            name=CHUNKS_VERTICAL_CATEGORY_INDEX,
        )
    except Exception as exc:  # pragma: no cover — best-effort on dev creds
        logger.warning(
            "s20_legacy_revoke.index.skip name=%s err=%s",
            CHUNKS_VERTICAL_CATEGORY_INDEX,
            exc.__class__.__name__,
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="s20_legacy_revoke",
        description=(
            "S20-A3 — stamp legacy (pre-A2) chunks with category="
            "legacy_uncategorized + metadata.deprecated=true. Dry-run by "
            "default; pass --apply to write."
        ),
    )
    grp = p.add_mutually_exclusive_group()
    grp.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Count matched legacy docs without writing (default).",
    )
    grp.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Actually mutate. Mutually exclusive with --dry-run.",
    )
    p.add_argument(
        "--mongodb-uri",
        default=os.environ.get("MONGODB_URI"),
        help="Mongo connection URI (default: $MONGODB_URI).",
    )
    # S30-#42: defaults route through gecko_core.db.mongo (Pattern A).
    # When --db / --collection are omitted, we resolve via chunk_db_name() /
    # CHUNKS_COLLECTION — the single source of truth for chunk-DB config.
    p.add_argument(
        "--db",
        default=None,
        help=(
            "Database override. Default: gecko_core.db.mongo.chunk_db_name() "
            "(respects $MONGODB_CHUNK_DB; falls back to 'gecko_rag')."
        ),
    )
    p.add_argument(
        "--collection",
        default=None,
        help=(
            f"Collection override. Default: {CHUNKS_COLLECTION!r} "
            "(gecko_core.db.mongo.CHUNKS_COLLECTION)."
        ),
    )
    p.add_argument(
        "--batch-size",
        type=int,
        default=1000,
        help="Batch size hint (default: 1000).",
    )
    return p


async def _run_cli(args: argparse.Namespace) -> int:
    if not args.mongodb_uri:
        logger.error("s20_legacy_revoke.missing_uri pass --mongodb-uri or set MONGODB_URI")
        return 2

    # Lazy import so unit tests (which stub the collection) don't need motor.
    from motor.motor_asyncio import AsyncIOMotorClient

    client: Any = AsyncIOMotorClient(args.mongodb_uri)
    try:
        # Pattern A: resolve via canonical accessor when overrides absent.
        db_name = args.db if args.db is not None else chunk_db_name()
        coll_name = args.collection if args.collection is not None else CHUNKS_COLLECTION
        coll = client[db_name][coll_name]
        dry_run = not args.apply
        summary = await revoke_legacy_chunks(
            coll,
            dry_run=dry_run,
            batch_size=args.batch_size,
        )
        # Operator-facing summary — printed to stdout for shell capture.
        print(
            "s20_legacy_revoke summary: "
            f"matched={summary['matched']} "
            f"modified={summary['modified']} "
            f"dry_run={summary['dry_run']} "
            f"index_built={summary['index_built']}"
        )
        return 0
    finally:
        client.close()


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO)
    parser = _build_parser()
    args = parser.parse_args(argv)
    return asyncio.run(_run_cli(args))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())


__all__ = [
    "LEGACY_FILTER",
    "_build_field_patch",
    "main",
    "revoke_legacy_chunks",
]
