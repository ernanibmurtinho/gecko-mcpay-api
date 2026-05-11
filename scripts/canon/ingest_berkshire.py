"""Ingest Berkshire Hathaway shareholder letters into the corpus.

Free + public-domain. HTML for 1977-2003, PDF for 2004-2024. Dispatches
to ``gecko_core.ingestion.web.extract`` or ``gecko_core.sources.pdf.extract``
based on URL suffix. Chunks land with ``provider_kind="canon_berkshire"``.

Run:
    set -a; source .env; set +a
    uv run python scripts/canon/ingest_berkshire.py
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import click

_REPO = Path(__file__).resolve().parents[2]
_GC_SRC = _REPO / "packages" / "gecko-core" / "src"
if str(_GC_SRC) not in sys.path:
    sys.path.insert(0, str(_GC_SRC))

from gecko_core.db.mongo_chunks import insert_chunks_mongo  # noqa: E402
from gecko_core.ingestion.chunker import chunk  # noqa: E402
from gecko_core.ingestion.embedder import embed  # noqa: E402
from gecko_core.ingestion.web import extract as web_extract  # noqa: E402
from gecko_core.sources.canon_berkshire import letter_urls  # noqa: E402
from gecko_core.sources.pdf import extract as pdf_extract  # noqa: E402

log = logging.getLogger("canon.berkshire")

_CANON_BRK_SESSION = uuid5(NAMESPACE_URL, "gecko.canon.berkshire.session.v1")


async def fetch_text(url: str, fmt: str) -> tuple[str, float]:
    """Dispatch to web or PDF extractor based on format. Returns (text, fetch_s)."""
    if fmt == "pdf":
        return await pdf_extract(url)
    return await web_extract(url)


async def ingest_one(letter_url: str, slug: str, fmt: str, *, dry_run: bool) -> dict[str, int]:
    source_id = uuid5(NAMESPACE_URL, letter_url)
    try:
        text, fetch_seconds = await fetch_text(letter_url, fmt)
    except Exception as exc:  # noqa: BLE001
        log.warning("FAIL %s fetch_error=%s", slug, exc)
        return {"chunks": 0, "fetched_bytes": 0, "skipped": 1}

    if not text:
        log.warning("EMPTY %s (extract returned blank text — scanned PDF?)", slug)
        return {"chunks": 0, "fetched_bytes": 0, "skipped": 1}

    chunks_text = chunk(text)
    if not chunks_text:
        log.warning("EMPTY %s (chunker produced 0 chunks)", slug)
        return {"chunks": 0, "fetched_bytes": len(text), "skipped": 1}

    log.info(
        "FETCH %-20s fmt=%-4s bytes=%6d  chunks=%3d  fetch_s=%.2f",
        slug, fmt, len(text), len(chunks_text), fetch_seconds,
    )

    if dry_run:
        return {"chunks": len(chunks_text), "fetched_bytes": len(text), "skipped": 0}

    vectors, _tokens = await embed(chunks_text)
    rows: list[tuple[int, str, list[float]]] = [
        (i, chunks_text[i], list(vectors[i])) for i in range(len(chunks_text))
    ]
    inserted = await insert_chunks_mongo(
        session_id=_CANON_BRK_SESSION,
        source_id=source_id,
        chunks=rows,
        category="investment_signals",
        vertical="dex",
        source="web",
        provider_kind="canon_berkshire",
        source_url=letter_url,
        freshness_tier="static",
        protocol=(),
        content_kind="mechanism",
    )
    log.info("INSERT %-20s new_chunks=%d", slug, inserted)
    return {"chunks": inserted, "fetched_bytes": len(text), "skipped": 0}


async def amain(*, dry_run: bool, limit: int | None, only_fmt: str | None, sleep_seconds: float) -> int:
    letters = letter_urls()
    if only_fmt:
        letters = [letter for letter in letters if letter.fmt == only_fmt]
    if limit is not None:
        letters = letters[:limit]
    log.info("=== canon-berkshire ingest: %d letters (dry_run=%s) ===", len(letters), dry_run)

    total_chunks = 0
    total_skipped = 0
    total_bytes = 0
    for i, letter in enumerate(letters, start=1):
        log.info("[%d/%d] %s", i, len(letters), letter.slug)
        stats = await ingest_one(letter.url, letter.slug, letter.fmt, dry_run=dry_run)
        total_chunks += stats["chunks"]
        total_skipped += stats["skipped"]
        total_bytes += stats["fetched_bytes"]
        if i < len(letters):
            await asyncio.sleep(sleep_seconds)

    log.info(
        "=== DONE: %d letters, %d chunks (written or planned), %d skipped, %d bytes ===",
        len(letters), total_chunks, total_skipped, total_bytes,
    )
    return 0


@click.command()
@click.option("--dry-run", is_flag=True, default=False, help="Fetch + chunk only; no Mongo writes.")
@click.option("--limit", type=int, default=None, help="Process at most this many letters.")
@click.option("--only-fmt", type=click.Choice(["html", "pdf"]), default=None,
              help="Restrict to one format (useful when PDFs are slow).")
@click.option("--sleep-seconds", type=float, default=1.0, show_default=True,
              help="Politeness delay between fetches.")
@click.option("--verbose", "-v", is_flag=True, default=False)
def main(dry_run: bool, limit: int | None, only_fmt: str | None, sleep_seconds: float, verbose: bool) -> None:
    """Ingest Berkshire Hathaway shareholder letters."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s %(message)s",
    )
    rc = asyncio.run(amain(
        dry_run=dry_run, limit=limit, only_fmt=only_fmt, sleep_seconds=sleep_seconds,
    ))
    sys.exit(rc)


if __name__ == "__main__":
    main()
