"""End-to-end ingestion pipeline.

For each approved `SourceCandidate`:
  1. Insert a source row (idempotent on sha256(url)).
  2. Extract text via the right adapter (YouTube captions / web scrape).
  3. Chunk + embed.
  4. Bulk-insert chunks; update source.chunk_count.

Concurrency is capped at 5 in-flight sources via `asyncio.Semaphore`.
Failures are isolated per-source: one bad URL doesn't block the others.
Idempotency: re-running with the same `(session_id, url)` set produces zero
duplicate rows.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from uuid import UUID

from gecko_core.models import SourceCandidate
from gecko_core.sessions.store import SessionStore

from . import web as web_extractor
from . import youtube as youtube_extractor
from .chunker import chunk as chunk_text
from .embedder import embed as embed_texts
from .types import IngestionResult, SourceOutcome

logger = logging.getLogger(__name__)

MAX_CONCURRENT_SOURCES = 5


def url_hash(url: str) -> str:
    """sha256 of the URL — used as the idempotency key."""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


async def _extract(candidate: SourceCandidate) -> tuple[str | None, float, float]:
    """Returns (text, deepgram_seconds_billed, tavily_extract_cost_usd).

    YouTube sources may bill Deepgram seconds (audio fallback). Web sources
    may bill a Tavily Extract call (bot-wall fallback). Most successful
    fetches bill nothing beyond the embedding + Tavily-discovery cost
    already accounted for upstream.
    """
    url = str(candidate.url)
    if candidate.type == "youtube":
        text, dg_seconds = await youtube_extractor.extract(url)
        return text, dg_seconds, 0.0
    # V2 URL-pattern adapters (Reddit thread / GitHub / PDF) take
    # priority over the generic web scraper when their pattern matches.
    # Imported lazily and via the submodule (not the parent package) so
    # the gecko_core.sources package is not pulled in at
    # gecko_core.ingestion package init time — that load order shadows
    # the ``gecko_core.sources`` package attribute behind ``list_sources``
    # (re-exported from gecko_core.workflows) and breaks legacy tests
    # that monkeypatch ``gecko_core.sources.reddit.<name>``.
    from gecko_core.sources.dispatcher import discover_adapter

    adapter = discover_adapter(url)
    if adapter is not None:
        _name, fn = adapter
        text_or_empty, _cost = await fn(url)
        return text_or_empty or None, 0.0, 0.0
    text, tavily_cost = await web_extractor.extract(url)
    return text, 0.0, tavily_cost


async def _process_one(
    session_id: UUID,
    candidate: SourceCandidate,
    store: SessionStore,
    sem: asyncio.Semaphore,
) -> SourceOutcome:
    url = str(candidate.url)
    async with sem:
        try:
            source_id = await store.insert_source(
                session_id=session_id,
                url=url,
                url_hash=url_hash(url),
                type_=candidate.type,
            )
            if source_id is None:
                # Duplicate — already ingested for this session.
                return SourceOutcome(
                    url=url, type=candidate.type, status="skipped", reason="duplicate"
                )

            text, deepgram_seconds, tavily_extract_cost = await _extract(candidate)
            if tavily_extract_cost > 0:
                # Charged whether or not the fallback found content — Tavily
                # bills per attempt. Recording before the early return below.
                await store.add_cost(session_id, "tavily", tavily_extract_cost)
            if not text:
                return SourceOutcome(
                    url=url,
                    type=candidate.type,
                    status="skipped",
                    reason="no_content",
                )
            if deepgram_seconds > 0:
                from .transcript import DeepgramTranscriptProvider as _Dg

                await store.add_cost(
                    session_id, "deepgram", deepgram_seconds * _Dg.NOVA_3_USD_PER_SECOND
                )

            pieces = chunk_text(text)
            if not pieces:
                return SourceOutcome(
                    url=url,
                    type=candidate.type,
                    status="skipped",
                    reason="empty_after_chunk",
                )

            embeddings, embed_tokens = await embed_texts(pieces)
            rows = list(zip(range(len(pieces)), pieces, embeddings, strict=True))
            inserted = await store.insert_chunks(session_id, source_id, rows)
            await store.set_source_chunk_count(source_id, inserted)
            if embed_tokens > 0:
                from .embedder import estimate_embed_cost_usd
                from .settings import get_ingestion_settings as _get_ingest

                embed_cost = estimate_embed_cost_usd(_get_ingest().embed_model, embed_tokens)
                await store.add_cost(session_id, "embed", embed_cost)

            return SourceOutcome(
                url=url,
                type=candidate.type,
                status="indexed",
                chunk_count=inserted,
            )
        except Exception as exc:  # per-source isolation
            # Don't leak API keys / payloads — just the type + message.
            logger.warning("ingestion failed for %s: %s", url, exc.__class__.__name__)
            return SourceOutcome(
                url=url,
                type=candidate.type,
                status="failed",
                reason=f"{exc.__class__.__name__}: {exc}",
            )


async def ingest(
    session_id: UUID,
    sources: list[SourceCandidate],
    store: SessionStore,
    *,
    max_concurrent: int = MAX_CONCURRENT_SOURCES,
) -> IngestionResult:
    """Run the full pipeline over a batch of approved sources."""
    sem = asyncio.Semaphore(max_concurrent)
    outcomes = await asyncio.gather(
        *(_process_one(session_id, c, store, sem) for c in sources),
        return_exceptions=False,
    )

    indexed = sum(1 for o in outcomes if o.status == "indexed")
    skipped = sum(1 for o in outcomes if o.status == "skipped")
    failed = sum(1 for o in outcomes if o.status == "failed")
    total_chunks = sum(o.chunk_count for o in outcomes)

    return IngestionResult(
        session_id=str(session_id),
        indexed=indexed,
        skipped=skipped,
        failed=failed,
        total_chunks=total_chunks,
        outcomes=list(outcomes),
    )


__all__ = ["MAX_CONCURRENT_SOURCES", "ingest", "url_hash"]
