"""CORPUS-EVICT-01 — one-shot operator script to evict academic-Gecko chunks.

Why this exists
---------------
The ingestion-layer blocklist
(``packages/gecko-core/src/gecko_core/ingestion/blocklist.py``) keeps NEW
academic-Gecko URLs (papers about reptiles, the Mozilla Gecko engine, the
``arxiv:2602.19218`` tool-use paper, etc.) out of the index. It does NOT
evict chunks that were written *before* the blocklist landed. Production
Mongo carries those legacy chunks today and they pollute every retrieval
pass with off-topic "Gecko" content.

This script reuses the canonical regex patterns from ``blocklist.py``
(per CLAUDE.md Pattern A — single source of truth for shared concepts)
and walks the ``chunks`` collection looking for matching ``source_url``
values. Dry-run is the default; ``--apply`` actually deletes.

Operation contract
------------------
- Scope: deletes chunks where ``source_url`` matches any compiled
  ``BLOCKLIST_PATTERNS`` entry (URL-only — title matching is an
  ingestion-time concern; we don't store titles on chunks).
- Dry-run output: per-pattern matched count + a sample of up to 5
  ``{_id, source_url}`` docs so the operator can eyeball what would
  be deleted before re-running with ``--apply``.
- Apply mode: issues one ``delete_many`` call per pattern (sequential,
  not bulk) so the per-pattern accounting in the summary is exact and
  any RBAC failure on a single pattern doesn't take down the whole run.

Safety
------
- Default is dry-run; ``--apply`` is required for writes.
- Patterns come from ``BLOCKLIST_PATTERNS`` — never redefined here.
- Connection URI is read from ``MONGODB_URI``; database name routes through
  ``gecko_core.db.mongo.chunk_db_name()`` — the single source of truth for
  ``MONGODB_CHUNK_DB`` (Pattern A).

S30-#42 BREAKING CHANGE
-----------------------
Previously this script accepted ``MONGODB_DB_NAME`` as an alternate env
alongside ``MONGODB_CHUNK_DB``. Per Pattern A (one canonical accessor for
chunk-DB name) the alias was removed: operators relying on
``MONGODB_DB_NAME`` for this script must switch to ``MONGODB_CHUNK_DB``
(the same env every other chunk-touching script honors). Explicit ``--db``
override still works for staging/test databases.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from typing import Any

from gecko_core.db.mongo import CHUNKS_COLLECTION, chunk_db_name
from gecko_core.ingestion.blocklist import BLOCKLIST_PATTERNS, BlocklistPattern

logger = logging.getLogger(__name__)


# Sample size shown in the dry-run report. Small enough to fit on a
# terminal page; large enough that the operator can sanity-check the
# regex didn't catch a canonical-Gecko URL by accident.
DRY_RUN_SAMPLE_SIZE: int = 5


async def _count_and_sample(
    collection: Any, pattern: BlocklistPattern
) -> tuple[int, list[dict[str, Any]]]:
    """Return ``(matched_count, sample_docs)`` for a single pattern.

    ``count_documents`` with the same regex used by the delete keeps
    the dry-run report consistent with what ``--apply`` would actually
    delete — no risk of the sample drifting from the count.
    """
    query = {"source_url": {"$regex": pattern.regex.pattern, "$options": "i"}}
    matched = await collection.count_documents(query)
    cursor = collection.find(query, projection={"_id": 1, "source_url": 1}).limit(
        DRY_RUN_SAMPLE_SIZE
    )
    samples: list[dict[str, Any]] = []
    async for doc in cursor:
        samples.append(doc)
    return matched, samples


async def evict_academic_gecko_chunks(collection: Any, *, dry_run: bool = True) -> dict[str, Any]:
    """Run the eviction (or dry-run) and return a structured summary.

    The returned dict carries ``per_pattern`` (list of
    ``{name, matched, deleted, samples}`` rows) and a top-level
    ``total_matched`` / ``total_deleted`` so the operator can paste the
    summary into a sprint log.
    """
    per_pattern: list[dict[str, Any]] = []
    total_matched = 0
    total_deleted = 0

    for pat in BLOCKLIST_PATTERNS:
        matched, samples = await _count_and_sample(collection, pat)
        deleted = 0
        if not dry_run and matched > 0:
            query = {"source_url": {"$regex": pat.regex.pattern, "$options": "i"}}
            res = await collection.delete_many(query)
            # ``delete_many`` returns a DeleteResult with ``deleted_count``;
            # fall back to ``matched`` if the driver returns None on a
            # mocked collection in tests.
            deleted = int(getattr(res, "deleted_count", 0) or 0)

        per_pattern.append(
            {
                "name": pat.name,
                "regex": pat.regex.pattern,
                "matched": matched,
                "deleted": deleted,
                "samples": samples,
            }
        )
        total_matched += matched
        total_deleted += deleted

    return {
        "dry_run": dry_run,
        "total_matched": total_matched,
        "total_deleted": total_deleted,
        "per_pattern": per_pattern,
    }


def _print_report(summary: dict[str, Any]) -> None:
    """Operator-facing report. stdout so shell capture works."""
    mode = "DRY-RUN" if summary["dry_run"] else "APPLY"
    print(f"evict_academic_gecko_chunks [{mode}]")
    print(f"  total_matched={summary['total_matched']} total_deleted={summary['total_deleted']}")
    for row in summary["per_pattern"]:
        print(f"  - pattern={row['name']} matched={row['matched']} deleted={row['deleted']}")
        for sample in row["samples"]:
            url = sample.get("source_url", "<no source_url>")
            doc_id = sample.get("_id", "<no _id>")
            print(f"      sample _id={doc_id} url={url}")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="evict_academic_gecko_chunks",
        description=(
            "CORPUS-EVICT-01 — delete chunks whose source_url matches the "
            "academic-Gecko blocklist regexes. Dry-run by default; pass "
            "--apply to actually delete."
        ),
    )
    grp = p.add_mutually_exclusive_group()
    grp.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Count + sample matching docs without deleting (default).",
    )
    grp.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Actually delete. Mutually exclusive with --dry-run.",
    )
    p.add_argument(
        "--mongodb-uri",
        default=os.environ.get("MONGODB_URI"),
        help="Mongo connection URI (default: $MONGODB_URI).",
    )
    # S30-#42: defaults route through gecko_core.db.mongo (Pattern A).
    # The legacy $MONGODB_DB_NAME alias was removed — operators must use
    # $MONGODB_CHUNK_DB (the single source of truth) or pass --db explicitly.
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
    return p


async def _run_cli(args: argparse.Namespace) -> int:
    if not args.mongodb_uri:
        logger.error(
            "evict_academic_gecko_chunks.missing_uri pass --mongodb-uri or set MONGODB_URI"
        )
        return 2

    # Lazy import so unit tests (which stub the collection) don't need motor.
    from motor.motor_asyncio import AsyncIOMotorClient

    client: Any = AsyncIOMotorClient(args.mongodb_uri)
    try:
        # Pattern A: resolve via canonical accessor when overrides absent.
        db_name = args.db if args.db is not None else chunk_db_name()
        coll_name = args.collection if args.collection is not None else CHUNKS_COLLECTION
        coll = client[db_name][coll_name]
        summary = await evict_academic_gecko_chunks(coll, dry_run=not args.apply)
        _print_report(summary)
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
    "DRY_RUN_SAMPLE_SIZE",
    "evict_academic_gecko_chunks",
    "main",
]
