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
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

import openai

from gecko_core.models import SourceCandidate
from gecko_core.sessions.store import SessionStore

from . import web as web_extractor
from . import youtube as youtube_extractor
from .audit import ErrorKind, classify_exception
from .chunker import chunk as chunk_text
from .embedder import embed as embed_texts
from .types import IngestionResult, ProviderChunk, SourceOutcome

if TYPE_CHECKING:  # ProviderKind is only used as a type hint here.
    # Imported lazily to avoid a top-level cycle: ``gecko_core/__init__.py``
    # rebinds the ``sources`` attribute on the ``gecko_core`` package to a
    # workflow function, so anything that pulls ``gecko_core.sources`` in
    # mid-``__init__`` ends up shadowing the package attribute and
    # downstream ``import gecko_core.sources.X`` calls fail with
    # ``cannot import name 'X' from 'list_sources'`` (see S17-WEDGE-WIRE-02
    # debugging notes). Deferring the import keeps the live attribute
    # binding intact.
    from gecko_core.sources.types import ProviderKind

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


async def _call_cache_get(
    cache_get: Any,
    url_hash: str,
    indices: list[int],
    embed_model: str | None,
) -> dict[int, list[float]]:
    """Call `store.get_chunk_cache` with `embed_model` when supported.

    S16-INGEST-03 — older stores / fakes in tests may not yet accept
    the kwarg. Try the new signature first, fall back to the legacy one
    on a TypeError. Avoids a hard breakage for FakeStore implementations
    in the test suite that haven't been updated.
    """
    try:
        return cast(
            dict[int, list[float]],
            await cache_get(url_hash, indices, embed_model=embed_model),
        )
    except TypeError:
        return cast(dict[int, list[float]], await cache_get(url_hash, indices))


async def _call_cache_put(
    cache_put: Any,
    url_hash: str,
    rows: list[tuple[int, str, list[float]]],
    embed_model: str | None,
) -> None:
    """Mirror of `_call_cache_get` for the put path. Same legacy fallback."""
    try:
        await cache_put(url_hash, rows, embed_model=embed_model)
    except TypeError:
        await cache_put(url_hash, rows)


async def _emit_audit(
    store: SessionStore,
    *,
    session_id: UUID,
    source_id: UUID | None,
    batch_size: int,
    succeeded: int,
    failed: int,
    error_kind: ErrorKind,
    embed_model: str | None,
) -> None:
    """Best-effort audit emission. Tolerates stores that don't implement
    `insert_chunks_write_audit` (FakeStore in tests, older deployments
    pre-migration). Never raises — observability must not mask the real
    outcome."""
    fn = getattr(store, "insert_chunks_write_audit", None)
    if fn is None:
        return
    try:
        await fn(
            session_id=session_id,
            source_id=source_id,
            batch_size=batch_size,
            succeeded=succeeded,
            failed=failed,
            error_kind=error_kind,
            embed_model=embed_model,
        )
    except Exception as exc:  # pragma: no cover — audit is best-effort
        logger.info(
            "ingest.audit.emit_failed exc=%s",
            exc.__class__.__name__,
            extra={"error_kind": error_kind, "session_id": str(session_id)},
        )


async def _process_one(
    session_id: UUID,
    candidate: SourceCandidate,
    store: SessionStore,
    sem: asyncio.Semaphore,
) -> SourceOutcome:
    url = str(candidate.url)
    uhash = url_hash(url)
    # S16-INGEST-01 — audit accumulators. Populated as the pipeline
    # progresses; emitted once at exit (success OR failure).
    source_id_for_audit: UUID | None = None
    batch_size_for_audit = 0
    embed_model_for_audit: str | None = None
    # Final audit error_kind — overwritten as the pipeline progresses.
    # `none` for clean success; classifier output otherwise.
    audit_error_kind: ErrorKind = "none"
    audit_succeeded = 0
    audit_failed = 0
    audit_skip = False

    async with sem:
        try:
            logger.info(
                "ingest.start url=%s type=%s session=%s",
                url,
                candidate.type,
                session_id,
                extra={"session_id": str(session_id), "url": url},
            )
            source_id = await store.insert_source(
                session_id=session_id,
                url=url,
                url_hash=uhash,
                type_=candidate.type,
            )
            if source_id is None:
                # Duplicate — already ingested for this session. Audit row
                # is uninteresting (skip).
                logger.info("ingest.dedup url=%s reason=session_duplicate", url)
                audit_skip = True
                return SourceOutcome(
                    url=url, type=candidate.type, status="skipped", reason="duplicate"
                )
            source_id_for_audit = source_id

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
                audit_skip = True
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
                audit_skip = True
                return SourceOutcome(
                    url=url,
                    type=candidate.type,
                    status="skipped",
                    reason="empty_after_chunk",
                )
            batch_size_for_audit = len(pieces)

            # S8-INGEST-03 — cross-session embedding cache. When a chunk for
            # this url_hash + chunk_index has been embedded before, reuse it
            # and skip the OpenAI round-trip entirely. Cache miss → call
            # embed() only on the missing indices.
            #
            # S16-INGEST-03 — resolve the active embed model BEFORE the
            # cache lookup so we filter on the matching PK fingerprint
            # (cache rows for a stale model don't shadow a fresh embed).
            try:
                from .settings import get_ingestion_settings as _get_ingest

                active_embed_model: str | None = _get_ingest().embed_model
            except Exception:
                # Settings may not resolve in tests / stub mode. Falling
                # back to None means "no model filter on the cache" —
                # legacy behaviour, accepted only when settings are
                # genuinely unavailable.
                active_embed_model = None
            embed_model_for_audit = active_embed_model

            cached_lookup: dict[int, list[float]] = {}
            cache_get = getattr(store, "get_chunk_cache", None)
            if cache_get is not None:
                try:
                    cached_lookup = await _call_cache_get(
                        cache_get,
                        uhash,
                        list(range(len(pieces))),
                        active_embed_model,
                    )
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
                        extra={
                            "session_id": str(session_id),
                            "source_id": str(source_id),
                            "batch_size": len(missing_texts),
                            "error_kind": classify_exception(exc),
                        },
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
                        await _call_cache_put(
                            cache_put,
                            uhash,
                            [(i, pieces[i], cached_lookup[i]) for i in missing_idxs],
                            active_embed_model,
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

            # `embed_model_for_audit` is already populated above (resolved
            # alongside the cache filter so a single source of truth flows
            # through both the lookup and the audit emission). S16-INGEST-03.
            embeddings = [cached_lookup[i] for i in range(len(pieces))]
            rows = list(zip(range(len(pieces)), pieces, embeddings, strict=True))
            try:
                inserted = await store.insert_chunks(session_id, source_id, rows, source_url=url)
            except Exception as exc:
                logger.warning(
                    "ingest.upsert.failed url=%s exc=%s msg=%s n_rows=%d vector_dim=%d",
                    url,
                    exc.__class__.__name__,
                    str(exc)[:200],
                    len(rows),
                    len(embeddings[0]) if embeddings else 0,
                    extra={
                        "session_id": str(session_id),
                        "source_id": str(source_id),
                        "batch_size": len(rows),
                        "error_kind": classify_exception(exc),
                    },
                )
                raise
            # S16-INGEST-02 — `insert_chunks` is now transactional + idempotent:
            # either the whole batch commits or it raises. A no-exception
            # short-write outcome (the old FM-1 path) is unreachable, so
            # the audit error_kind is unconditionally `none` here.
            audit_succeeded = inserted
            audit_failed = max(0, len(rows) - inserted)
            audit_error_kind = "none"
            await store.set_source_chunk_count(source_id, inserted)
            if embed_tokens > 0:
                from .embedder import estimate_embed_cost_usd

                # Reuse the model name resolved alongside the cache filter
                # — single source of truth per S16-INGEST-03. Falls back
                # to "" if settings were unavailable; the cost lookup
                # treats unknown models as $0/M tokens (defensive on dev).
                embed_cost = estimate_embed_cost_usd(active_embed_model or "", embed_tokens)
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
            audit_error_kind = classify_exception(exc)
            audit_failed = batch_size_for_audit or 0
            audit_succeeded = 0
            # S17-INGEST-FALLBACK-01 — when the PDF parser fallback chain
            # exhausts (UnparseablePDFError), the source is genuinely
            # broken (image-only scan, encrypted, corrupt header). That's
            # a clean SKIP, not a failure: log at info level with a
            # structured ErrorKind, mark the source as skipped, no chunks
            # written. Audit row still emits so the histogram can count
            # how often this happens in the wild.
            if audit_error_kind == "unparseable_pdf":
                logger.info(
                    "ingest.skipped.unparseable_pdf url=%s exc=%s msg=%s",
                    url,
                    exc.__class__.__name__,
                    str(exc)[:200],
                    extra={
                        "session_id": str(session_id),
                        "source_id": (str(source_id_for_audit) if source_id_for_audit else None),
                        "error_kind": audit_error_kind,
                    },
                )
                return SourceOutcome(
                    url=url,
                    type=candidate.type,
                    status="skipped",
                    reason="unparseable_pdf",
                )
            logger.warning(
                "ingest.failed url=%s exc=%s msg=%s",
                url,
                exc.__class__.__name__,
                str(exc)[:200],
                extra={
                    "session_id": str(session_id),
                    "source_id": str(source_id_for_audit) if source_id_for_audit else None,
                    "batch_size": batch_size_for_audit,
                    "error_kind": audit_error_kind,
                },
            )
            return SourceOutcome(
                url=url,
                type=candidate.type,
                status="failed",
                reason=f"{exc.__class__.__name__}: {exc}",
            )
        finally:
            # S16-INGEST-01 — one audit row per source-batch exit. Skipped
            # for the "this candidate never reached the chunk-write path"
            # cases (duplicate / no_content / empty_after_chunk) — those
            # have nothing useful to bucket.
            if not audit_skip:
                await _emit_audit(
                    store,
                    session_id=session_id,
                    source_id=source_id_for_audit,
                    batch_size=batch_size_for_audit,
                    succeeded=audit_succeeded,
                    failed=audit_failed,
                    error_kind=audit_error_kind,
                    embed_model=embed_model_for_audit,
                )


async def ingest(
    session_id: UUID,
    sources: list[SourceCandidate],
    store: SessionStore,
    *,
    max_concurrent: int = MAX_CONCURRENT_SOURCES,
    degraded_sources: list[str] | None = None,
) -> IngestionResult:
    """Run the full pipeline over a batch of approved sources.

    S12-PROVIDER-01 — `degraded_sources` is forwarded into the result
    when the upstream dispatcher had a provider time out or fail
    health. The pipeline doesn't manufacture entries here; it
    propagates what the dispatcher reports. Per-URL fetch failures
    continue to land on `outcomes` with status="failed" (per-source
    isolation is unchanged).
    """
    # S17-INGEST-FALLBACK-01 — bind a per-session Tavily Extract fallback
    # budget. Threaded through web.extract() via ContextVar so the public
    # signature stays stable. Budget is consumed only on live (non-cache)
    # Tavily Extract calls; the cap protects against a single bad session
    # burning the credit budget on a wall of broken URLs.
    web_extractor.bind_tavily_extract_budget()

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
        degraded_sources=list(degraded_sources or []),
    )


async def dispatch_providers(
    idea: str,
    providers: list[object] | None = None,
) -> tuple[list[SourceCandidate], list[str]]:
    """Fan out across SourceProviders; return (candidates, degraded_names).

    S12-PROVIDER-01 — the seam that lets S13+ wire BazaarProvider,
    ParagraphProvider, etc. without touching `discover()` or `ingest()`.
    Today the only registered provider is the default FreeProvider, so
    this function is a thin wrapper around `discover()` — but it
    establishes the contract: per-provider timeouts go here, the
    `degraded_sources` accounting goes here, the budget enforcement
    will go here.

    `providers=None` → use the module-level DEFAULT_FREE_PROVIDER. Pass
    an explicit list to swap in alternates (tests, vertical suites).
    """
    # Lazy import: providers/free_provider re-imports from this package
    # for `discover`, so importing it at module top-level would create
    # a partial-init cycle.
    from .providers.free_provider import DEFAULT_FREE_PROVIDER

    selected: list[object] = list(providers) if providers is not None else [DEFAULT_FREE_PROVIDER]

    results: list[SourceCandidate] = []
    degraded: list[str] = []

    for provider in selected:
        name = getattr(provider, "name", provider.__class__.__name__)
        try:
            chunks = await provider.fetch(idea)  # type: ignore[attr-defined]
        except Exception as exc:  # provider-level isolation
            logger.warning(
                "dispatch.provider_failed name=%s exc=%s msg=%s",
                name,
                exc.__class__.__name__,
                str(exc)[:200],
            )
            degraded.append(name)
            continue
        results.extend(c.candidate for c in chunks)

    return results, degraded


async def ingest_provider_chunks(
    *,
    session_id: UUID,
    provider_kind: ProviderKind,
    resource_id: str,
    synthetic_uri: str,
    chunks: list[ProviderChunk],
    store: SessionStore,
) -> int:
    """Embed provider-supplied chunks and insert into the ``chunks`` table.

    S17-WEDGE-WIRE-02. Reuses the same embedder, embedding cache, and
    audit path as Tavily ingestion (``_process_one``). The intent is
    explicit: dispatched provider payloads (Bazaar / Arxiv / twit.sh)
    end up in the same retrieval index Tavily web chunks do, so
    ``match_chunks`` can return them as first-class citations.

    Design notes (see ``docs/strategy/2026-05-02-wedge-wire-path-b-design.md``):
      §1.4  Synthetic URI per (session, provider, resource):
              bazaar: ``bazaar://<service>/<resource>``
              arxiv:  real ``https://arxiv.org/abs/<id>`` URL
              twitsh: ``twitsh://session/<session_id>``
      §2     One source row per ``synthetic_uri``; idempotent on
              ``(session_id, url_hash)``. Cache lookup mirrors Tavily.
      §2.4   A single chunk failing to embed must not fail the session;
              we let the embedder's existing retry/halving cover the
              transient layer and emit an audit row on persistent failure.

    Args:
        session_id:       The session that owns these chunks.
        provider_kind:    Canonical chunks-table column value.
        resource_id:      Stable identifier of the upstream resource
                          (used in logs + the in-memory grouping pass).
        synthetic_uri:    Source row's ``url`` value. Idempotency key.
        chunks:           Pre-rendered ProviderChunk records. Empty
                          ``text`` entries are filtered before embedding.
        store:            The SessionStore implementation.

    Returns:
        Number of chunks the store reported as durable. Returns ``0``
        on duplicate-source dedup, empty input, or full embed failure.
    """
    if not chunks:
        return 0

    # Strip empties up-front — same contract as ``_filter_embeddable``.
    embeddable = [c for c in chunks if c.text and c.text.strip()]
    if not embeddable:
        return 0

    uhash = hashlib.sha256(synthetic_uri.encode("utf-8")).hexdigest()

    # Insert (or look up) the synthetic source row. ``type_="provider"``
    # was added to the existing CHECK constraint by S17-WEDGE-DATA-01.
    try:
        source_id = await store.insert_source(
            session_id=session_id,
            url=synthetic_uri,
            url_hash=uhash,
            type_="provider",
            provider_kind=provider_kind,
        )
    except Exception as exc:
        logger.warning(
            "ingest_provider_chunks.insert_source_failed provider=%s uri=%s exc=%s msg=%s",
            provider_kind,
            synthetic_uri,
            exc.__class__.__name__,
            str(exc)[:200],
            extra={"session_id": str(session_id), "provider_kind": provider_kind},
        )
        return 0

    if source_id is None:
        # Duplicate (session_id, url_hash). Re-ingestion is a no-op:
        # the chunks are already there from a prior run.
        logger.info(
            "ingest_provider_chunks.dedup provider=%s uri=%s reason=duplicate_source",
            provider_kind,
            synthetic_uri,
        )
        return 0

    # Resolve the active embed model so cache filtering uses the right
    # fingerprint (mirrors S16-INGEST-03 in ``_process_one``).
    try:
        from .settings import get_ingestion_settings as _get_ingest

        active_embed_model: str | None = _get_ingest().embed_model
    except Exception:
        active_embed_model = None

    # The chunks table's ``(source_id, chunk_index)`` UNIQUE constraint
    # requires distinct indices under the synthetic source. The adapters
    # produce per-resource indices (e.g. each Bazaar service starts at 0);
    # re-key here so cross-resource collisions don't drop rows on upsert.
    pieces: list[str] = [c.text for c in embeddable]
    indices = list(range(len(pieces)))

    # Cache lookup — same shape as the Tavily path. Best-effort: any
    # cache failure degrades to "embed everything fresh".
    cached_lookup: dict[int, list[float]] = {}
    cache_get = getattr(store, "get_chunk_cache", None)
    if cache_get is not None:
        try:
            cached_lookup = await _call_cache_get(
                cache_get,
                uhash,
                indices,
                active_embed_model,
            )
        except Exception as exc:
            logger.info(
                "ingest_provider_chunks.cache_lookup_failed provider=%s uri=%s err=%s",
                provider_kind,
                synthetic_uri,
                exc.__class__.__name__,
            )
            cached_lookup = {}

    missing_idxs = [i for i in indices if i not in cached_lookup]
    embed_tokens = 0
    audit_error_kind: ErrorKind = "none"
    audit_succeeded = 0
    audit_failed = 0
    batch_size_for_audit = len(pieces)

    try:
        if missing_idxs:
            missing_texts = [pieces[i] for i in missing_idxs]
            try:
                new_embeddings, embed_tokens = await embed_texts(missing_texts)
            except openai.OpenAIError as exc:
                # Persistent embed failure across the whole missing set.
                # Surface the audit row + degrade — no partial write.
                audit_error_kind = classify_exception(exc)
                audit_failed = batch_size_for_audit
                logger.warning(
                    "ingest_provider_chunks.embed_failed provider=%s uri=%s exc=%s",
                    provider_kind,
                    synthetic_uri,
                    exc.__class__.__name__,
                    extra={
                        "session_id": str(session_id),
                        "source_id": str(source_id),
                        "batch_size": len(missing_texts),
                        "error_kind": audit_error_kind,
                    },
                )
                return 0
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
                    await _call_cache_put(
                        cache_put,
                        uhash,
                        [(i, pieces[i], cached_lookup[i]) for i in missing_idxs],
                        active_embed_model,
                    )
                except Exception as exc:  # cache write is best-effort
                    logger.info(
                        "ingest_provider_chunks.cache_store_failed provider=%s uri=%s err=%s",
                        provider_kind,
                        synthetic_uri,
                        exc.__class__.__name__,
                    )
        else:
            logger.info(
                "ingest_provider_chunks.embed.skipped provider=%s uri=%s "
                "reason=full_cache_hit chunks=%d",
                provider_kind,
                synthetic_uri,
                len(pieces),
            )

        embeddings = [cached_lookup[i] for i in indices]
        rows = list(zip(indices, pieces, embeddings, strict=True))
        try:
            inserted = await store.insert_chunks(
                session_id,
                source_id,
                rows,
                provider_kind=provider_kind,
                source_url=synthetic_uri,
            )
        except Exception as exc:
            audit_error_kind = classify_exception(exc)
            audit_failed = len(rows)
            logger.warning(
                "ingest_provider_chunks.upsert_failed provider=%s uri=%s exc=%s msg=%s",
                provider_kind,
                synthetic_uri,
                exc.__class__.__name__,
                str(exc)[:200],
                extra={
                    "session_id": str(session_id),
                    "source_id": str(source_id),
                    "batch_size": len(rows),
                    "error_kind": audit_error_kind,
                },
            )
            return 0

        try:
            await store.set_source_chunk_count(source_id, inserted)
        except Exception as exc:  # pragma: no cover — best-effort
            logger.info(
                "ingest_provider_chunks.chunk_count_set_failed provider=%s err=%s",
                provider_kind,
                exc.__class__.__name__,
            )

        if embed_tokens > 0:
            from .embedder import estimate_embed_cost_usd

            try:
                cost = estimate_embed_cost_usd(active_embed_model or "", embed_tokens)
                await store.add_cost(session_id, "embed", cost)
            except Exception as exc:  # pragma: no cover
                logger.info(
                    "ingest_provider_chunks.add_cost_failed provider=%s err=%s",
                    provider_kind,
                    exc.__class__.__name__,
                )

        audit_succeeded = inserted
        audit_error_kind = "none"
        logger.info(
            "ingest_provider_chunks.indexed provider=%s uri=%s resource=%s "
            "chunks=%d embed_tokens=%d",
            provider_kind,
            synthetic_uri,
            resource_id,
            inserted,
            embed_tokens,
        )
        return inserted
    finally:
        await _emit_audit(
            store,
            session_id=session_id,
            source_id=source_id,
            batch_size=batch_size_for_audit,
            succeeded=audit_succeeded,
            failed=audit_failed,
            error_kind=audit_error_kind,
            embed_model=active_embed_model,
        )


__all__ = [
    "MAX_CONCURRENT_SOURCES",
    "dispatch_providers",
    "ingest",
    "ingest_provider_chunks",
    "url_hash",
]
