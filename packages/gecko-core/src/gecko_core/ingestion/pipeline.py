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
from typing import Any
from uuid import UUID

import openai

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


def _filter_embeddable(pieces: list[str]) -> list[str]:
    """Drop empty / whitespace-only chunks before sending to OpenAI.

    OpenAI's embeddings endpoint rejects empty strings with a 400
    BadRequestError (subclass of openai.APIError) and the failure mode in
    Sprint 7 dogfood was the entire batch crashing on a single empty piece.
    Chunker can produce all-whitespace decoded slices when the source is
    markdown-heavy with code fences / tables. Cheap to filter here.
    """
    return [p for p in pieces if p and p.strip()]


def _openai_error_detail(exc: BaseException) -> str:
    """Best-effort structured detail from an openai SDK exception.

    Pulls status, code, type, and the response message body when present.
    Never includes API keys (the openai SDK redacts headers in repr).
    """
    parts: list[str] = []
    status = getattr(exc, "status_code", None)
    if status is not None:
        parts.append(f"status={status}")
    code = getattr(exc, "code", None)
    if code:
        parts.append(f"code={code}")
    body: Any = getattr(exc, "body", None)
    if isinstance(body, dict):
        err = body.get("error") or {}
        if isinstance(err, dict):
            msg = err.get("message")
            etype = err.get("type")
            if etype:
                parts.append(f"type={etype}")
            if msg:
                parts.append(f"message={msg!r}")
    if not parts:
        parts.append(str(exc))
    return " ".join(parts)


async def _process_one(
    session_id: UUID,
    candidate: SourceCandidate,
    store: SessionStore,
    sem: asyncio.Semaphore,
) -> SourceOutcome:
    url = str(candidate.url)
    uhash = url_hash(url)
    async with sem:
        try:
            logger.info(
                "ingest.start url=%s type=%s session=%s",
                url,
                candidate.type,
                session_id,
            )
            source_id = await store.insert_source(
                session_id=session_id,
                url=url,
                url_hash=uhash,
                type_=candidate.type,
            )
            if source_id is None:
                # Duplicate — already ingested for this session.
                logger.info("ingest.dedup url=%s reason=session_duplicate", url)
                return SourceOutcome(
                    url=url, type=candidate.type, status="skipped", reason="duplicate"
                )

            text, deepgram_seconds, tavily_extract_cost = await _extract(candidate)
            logger.info(
                "ingest.extracted url=%s text_chars=%d tavily_cost_usd=%.4f dg_seconds=%.2f",
                url,
                len(text) if text else 0,
                tavily_extract_cost,
                deepgram_seconds,
            )
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

            raw_pieces = chunk_text(text)
            pieces = _filter_embeddable(raw_pieces)
            logger.info(
                "ingest.chunked url=%s raw_chunks=%d embeddable_chunks=%d",
                url,
                len(raw_pieces),
                len(pieces),
            )
            if not pieces:
                return SourceOutcome(
                    url=url,
                    type=candidate.type,
                    status="skipped",
                    reason="empty_after_chunk",
                )

            # S8-INGEST-03 — cross-session embedding cache. When a chunk for
            # this url_hash + chunk_index has been embedded before, reuse it
            # and skip the OpenAI round-trip entirely. Cache miss → call
            # embed() only on the missing indices.
            cached_lookup: dict[int, list[float]] = {}
            cache_get = getattr(store, "get_chunk_cache", None)
            if cache_get is not None:
                try:
                    cached_lookup = await cache_get(uhash, list(range(len(pieces))))
                except Exception as exc:  # cache is best-effort
                    logger.info(
                        "ingest.cache_lookup_failed url=%s err=%s",
                        url,
                        exc.__class__.__name__,
                    )
                    cached_lookup = {}

            missing_idxs = [i for i in range(len(pieces)) if i not in cached_lookup]
            embed_tokens = 0
            if missing_idxs:
                missing_texts = [pieces[i] for i in missing_idxs]
                logger.info(
                    "ingest.embed.start url=%s cache_hits=%d to_embed=%d",
                    url,
                    len(cached_lookup),
                    len(missing_texts),
                )
                try:
                    new_embeddings, embed_tokens = await embed_texts(missing_texts)
                except openai.OpenAIError as exc:
                    detail = _openai_error_detail(exc)
                    logger.warning(
                        "ingest.embed.failed url=%s exc=%s detail=%s n_inputs=%d",
                        url,
                        exc.__class__.__name__,
                        detail,
                        len(missing_texts),
                    )
                    raise
                if len(new_embeddings) != len(missing_texts):
                    raise RuntimeError(
                        f"embedder returned {len(new_embeddings)} vectors for "
                        f"{len(missing_texts)} inputs (shape mismatch)"
                    )
                for idx, vec in zip(missing_idxs, new_embeddings, strict=True):
                    cached_lookup[idx] = vec
                cache_put = getattr(store, "put_chunk_cache", None)
                if cache_put is not None:
                    try:
                        await cache_put(
                            uhash,
                            [(i, pieces[i], cached_lookup[i]) for i in missing_idxs],
                        )
                    except Exception as exc:  # cache write is best-effort
                        logger.info(
                            "ingest.cache_store_failed url=%s err=%s",
                            url,
                            exc.__class__.__name__,
                        )
            else:
                logger.info(
                    "ingest.embed.skipped url=%s reason=full_cache_hit chunks=%d",
                    url,
                    len(pieces),
                )

            embeddings = [cached_lookup[i] for i in range(len(pieces))]
            rows = list(zip(range(len(pieces)), pieces, embeddings, strict=True))
            try:
                inserted = await store.insert_chunks(session_id, source_id, rows)
            except Exception as exc:
                logger.warning(
                    "ingest.upsert.failed url=%s exc=%s msg=%s n_rows=%d vector_dim=%d",
                    url,
                    exc.__class__.__name__,
                    str(exc)[:200],
                    len(rows),
                    len(embeddings[0]) if embeddings else 0,
                )
                raise
            await store.set_source_chunk_count(source_id, inserted)
            if embed_tokens > 0:
                from .embedder import estimate_embed_cost_usd
                from .settings import get_ingestion_settings as _get_ingest

                embed_cost = estimate_embed_cost_usd(_get_ingest().embed_model, embed_tokens)
                await store.add_cost(session_id, "embed", embed_cost)

            logger.info(
                "ingest.indexed url=%s chunks=%d embed_tokens=%d",
                url,
                inserted,
                embed_tokens,
            )
            return SourceOutcome(
                url=url,
                type=candidate.type,
                status="indexed",
                chunk_count=inserted,
            )
        except Exception as exc:  # per-source isolation
            # Don't leak API keys / payloads — just the type + summary.
            logger.warning(
                "ingest.failed url=%s exc=%s msg=%s",
                url,
                exc.__class__.__name__,
                str(exc)[:200],
            )
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
