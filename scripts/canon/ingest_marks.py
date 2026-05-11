"""Ingest Howard Marks / Oaktree memos into the corpus.

Free + public-domain investor-canon. Reuses the existing
``gecko_core.ingestion.web.extract`` + ``chunker`` + ``embedder`` +
``insert_chunks_mongo`` stack so the chunks land on the same Atlas
Vector Search index as paysh/bazaar content. ``provider_kind="canon_marks"``
gates retrieval per the canonical Literal (see
``gecko_core.sources.types``).

Idempotent: same memo URL re-ingested = 0 net new chunks via the
``(source_id, chunk_index)`` unique index on Mongo. Run as often as you
like — the curated URL list expands as new Marks memos publish.

Run:
    set -a; source .env; set +a   # MONGODB_URI + embedder keys
    uv run python scripts/canon/ingest_marks.py
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import click

# Make ``packages/gecko-core/src`` importable when run from the repo root.
_REPO = Path(__file__).resolve().parents[2]
_GC_SRC = _REPO / "packages" / "gecko-core" / "src"
if str(_GC_SRC) not in sys.path:
    sys.path.insert(0, str(_GC_SRC))

from gecko_core.db.mongo_chunks import insert_chunks_mongo  # noqa: E402
from gecko_core.ingestion.chunker import chunk  # noqa: E402
from gecko_core.ingestion.embedder import embed  # noqa: E402
from gecko_core.ingestion.web import extract  # noqa: E402
from gecko_core.sources.canon_marks import memo_urls  # noqa: E402

log = logging.getLogger("canon.marks")

# One UUID5 namespace for the canon-marks corpus — every memo's source_id
# is derived from its URL within this namespace. Same URL → same id, so
# re-runs are idempotent.
_CANON_MARKS_SESSION = uuid5(NAMESPACE_URL, "gecko.canon.marks.session.v1")


async def ingest_one(memo_url: str, slug: str, *, dry_run: bool) -> dict[str, int]:
    """Fetch + chunk + embed + insert ONE memo. Return stats."""
    source_id = uuid5(NAMESPACE_URL, memo_url)
    try:
        text, fetch_seconds = await extract(memo_url)
    except Exception as exc:  # noqa: BLE001
        log.warning("FAIL %s fetch_error=%s", slug, exc)
        return {"chunks": 0, "fetched_bytes": 0, "skipped": 1}

    chunks_text = chunk(text)
    if not chunks_text:
        log.warning("EMPTY %s (extract returned blank text)", slug)
        return {"chunks": 0, "fetched_bytes": len(text), "skipped": 1}

    log.info(
        "FETCH %-40s bytes=%d  chunks=%d  fetch_s=%.2f",
        slug, len(text), len(chunks_text), fetch_seconds,
    )

    if dry_run:
        return {"chunks": len(chunks_text), "fetched_bytes": len(text), "skipped": 0}

    # One embed call per memo (batched). Voyage handles up to 128 in one
    # request comfortably.
    vectors, _tokens = await embed(chunks_text)

    rows: list[tuple[int, str, list[float]]] = [
        (i, chunks_text[i], list(vectors[i])) for i in range(len(chunks_text))
    ]

    inserted = await insert_chunks_mongo(
        session_id=_CANON_MARKS_SESSION,
        source_id=source_id,
        chunks=rows,
        category="investment_signals",
        vertical="dex",  # trade-vertical retrieval surface
        source="web",
        provider_kind="canon_marks",
        source_url=memo_url,
        freshness_tier="static",
        protocol=(),  # canon docs are protocol-agnostic
        content_kind="mechanism",
    )
    log.info("INSERT %-40s new_chunks=%d", slug, inserted)
    return {"chunks": inserted, "fetched_bytes": len(text), "skipped": 0}


async def amain(*, dry_run: bool, limit: int | None, sleep_seconds: float) -> int:
    memos = memo_urls()
    if limit is not None:
        memos = memos[:limit]
    log.info("=== canon-marks ingest: %d memos (dry_run=%s) ===", len(memos), dry_run)

    total_chunks = 0
    total_skipped = 0
    total_bytes = 0
    for i, memo in enumerate(memos, start=1):
        log.info("[%d/%d] %s", i, len(memos), memo.slug)
        stats = await ingest_one(memo.url, memo.slug, dry_run=dry_run)
        total_chunks += stats["chunks"]
        total_skipped += stats["skipped"]
        total_bytes += stats["fetched_bytes"]
        if i < len(memos):
            await asyncio.sleep(sleep_seconds)

    log.info(
        "=== DONE: %d memos, %d chunks written (or planned), %d skipped, %d bytes fetched ===",
        len(memos), total_chunks, total_skipped, total_bytes,
    )
    return 0


@click.command()
@click.option(
    "--dry-run", is_flag=True, default=False,
    help="Fetch + chunk only; no embed, no Mongo writes.",
)
@click.option(
    "--limit", type=int, default=None,
    help="Process at most this many memos (smoke testing).",
)
@click.option(
    "--sleep-seconds", type=float, default=1.0, show_default=True,
    help="Politeness delay between memo fetches.",
)
@click.option(
    "--verbose", "-v", is_flag=True, default=False,
)
def main(dry_run: bool, limit: int | None, sleep_seconds: float, verbose: bool) -> None:
    """Ingest Howard Marks / Oaktree memos into the corpus."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s %(message)s",
    )
    rc = asyncio.run(amain(
        dry_run=dry_run, limit=limit, sleep_seconds=sleep_seconds,
    ))
    sys.exit(rc)


if __name__ == "__main__":
    main()
