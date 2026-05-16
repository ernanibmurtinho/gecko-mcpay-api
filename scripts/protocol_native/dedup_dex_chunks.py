"""S33-#80 — one-shot dedup pass over the ``dex`` vertical.

The S33 retrieval-quality diagnosis (§3) verified that 1,807 of 9,063
``dex`` chunks (19.9%) are byte-identical duplicate text. Root cause: the
``protocol_native`` ingest keyed ``source_id`` on a day bucket, so each
daily re-ingest appended a fresh full copy instead of replacing the prior
day's. A live kamino probe returned 9 of 15 top slots as copies of one
chunk — when most citations are the same string, ``citation_relevance``
is mathematically capped.

The ingest-path fix (stable ``source_id`` + replace-before-insert) is in
``ingest_protocol_native.py`` + ``delete_chunks_for_source_mongo`` — it
stops NEW duplicates. This script cleans the EXISTING ones.

Dedup rule: within ``vertical="dex"``, group by exact ``text``. For each
group with >1 doc, KEEP the newest by ``as_of_date`` (string day bucket,
lexicographically sortable) then ``captured_at``, and DELETE the rest.
This is loss-free by definition — duplicates carry identical text; the
only thing dropped is the stale copy.

Run:
    set -a; source .env; set +a   # MONGODB_URI
    uv run python scripts/protocol_native/dedup_dex_chunks.py --dry-run
    uv run python scripts/protocol_native/dedup_dex_chunks.py --apply

``--dry-run`` (default) reports before/after counts without writing.
``--apply`` performs the deletes. Idempotent: a second --apply run finds
zero duplicates and deletes nothing.
"""

from __future__ import annotations

import logging
import sys

import click

log = logging.getLogger("protocol_native.dedup")


def _sort_key(doc: dict[str, object]) -> tuple[str, str]:
    """Newest-first sort key: (as_of_date, captured_at) as strings.

    ``as_of_date`` is a ``YYYY-MM-DD`` string (lexicographically ordered);
    ``captured_at`` is a datetime — ``str()`` of it is ISO-ordered too.
    Missing fields sort oldest (empty string).
    """
    as_of = doc.get("as_of_date")
    captured = doc.get("captured_at")
    return (
        as_of if isinstance(as_of, str) else "",
        str(captured) if captured is not None else "",
    )


def _dedup(*, apply: bool) -> int:
    """Run the dedup pass. Returns process exit code."""
    try:
        import os

        from pymongo import MongoClient
    except ImportError as exc:  # pragma: no cover — env guard
        log.error("pymongo not importable: %s", exc)
        return 1

    uri = os.environ.get("MONGODB_URI")
    if not uri:
        log.error("MONGODB_URI unset — `set -a; source .env; set +a` first")
        return 1

    client: MongoClient[dict[str, object]] = MongoClient(uri)
    coll = client.get_database("gecko_rag")["chunks"]

    before = coll.count_documents({"vertical": "dex"})
    log.info("dex chunks before: %d", before)

    # Group by exact text; only groups with >1 member are duplicate sets.
    pipeline: list[dict[str, object]] = [
        {"$match": {"vertical": "dex"}},
        {"$group": {"_id": "$text", "ids": {"$push": "$_id"}, "n": {"$sum": 1}}},
        {"$match": {"n": {"$gt": 1}}},
    ]
    dup_groups = list(coll.aggregate(pipeline, allowDiskUse=True))
    redundant_total = sum(int(g["n"]) - 1 for g in dup_groups)  # type: ignore[call-overload]
    log.info(
        "duplicate text groups: %d — redundant copies to remove: %d",
        len(dup_groups),
        redundant_total,
    )

    if not dup_groups:
        log.info("no duplicates — nothing to do")
        return 0

    delete_ids: list[object] = []
    for group in dup_groups:
        ids = group["ids"]
        # Fetch the docs in this group to pick the newest as the keeper.
        docs = list(
            coll.find(
                {"_id": {"$in": ids}},
                projection={"_id": 1, "as_of_date": 1, "captured_at": 1},
            )
        )
        docs.sort(key=_sort_key, reverse=True)  # newest first
        keeper = docs[0]["_id"]
        delete_ids.extend(d["_id"] for d in docs if d["_id"] != keeper)

    log.info("docs flagged for deletion: %d", len(delete_ids))

    if not apply:
        after = before - len(delete_ids)
        log.info(
            "DRY-RUN — would delete %d; dex would go %d -> %d. "
            "Re-run with --apply to perform the deletes.",
            len(delete_ids),
            before,
            after,
        )
        return 0

    # Delete in batches to bound the query document size.
    batch = 1000
    deleted = 0
    for start in range(0, len(delete_ids), batch):
        chunk_ids = delete_ids[start : start + batch]
        result = coll.delete_many({"_id": {"$in": chunk_ids}})
        deleted += int(getattr(result, "deleted_count", 0))
    after = coll.count_documents({"vertical": "dex"})
    log.info("APPLIED — deleted %d; dex %d -> %d", deleted, before, after)

    # Verify: a second pass should now find zero duplicate groups.
    residual = list(coll.aggregate(pipeline, allowDiskUse=True))
    if residual:
        log.warning("VERIFY — %d duplicate groups still present (re-run)", len(residual))
    else:
        log.info("VERIFY — zero duplicate text groups remain in dex")
    return 0


@click.command()
@click.option(
    "--apply",
    "apply_changes",
    is_flag=True,
    default=False,
    help="Perform the deletes. Without this flag the script only reports.",
)
@click.option("--verbose", "-v", is_flag=True, default=False)
def main(apply_changes: bool, verbose: bool) -> None:
    """S33-#80 — dedup the dex vertical's exact-text duplicates."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s %(message)s",
    )
    sys.exit(_dedup(apply=apply_changes))


if __name__ == "__main__":
    main()
