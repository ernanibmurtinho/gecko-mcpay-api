"""Mongo write paths for the chunk store (S18-MONGO-WRITE-01).

Mirrors the four chunk-write methods on ``SessionStore``:
- ``insert_chunks`` → :func:`insert_chunks_mongo`
- ``insert_chunks_write_audit`` → :func:`insert_chunks_write_audit_mongo`
- ``get_chunk_cache`` / ``_evict_chunk_cache`` → :func:`get_chunk_cache_mongo` /
  :func:`evict_chunk_cache_mongo`
- ``put_chunk_cache`` → :func:`put_chunk_cache_mongo`

Why the simpler shape vs Supabase:

- No payload halving. Mongo's 16 MB BSON document limit is far above any
  individual chunk; the supabase ``toast_limit`` shed-and-retry path was
  needed because Postgres TOASTing on 1536-dim vectors crossed httpx
  read timeouts. Mongo bulk inserts split client-side; we use
  ``ordered=False`` so duplicate-key errors on
  ``(source_id, chunk_index)`` are skipped instead of aborting the batch.
- No background eviction race. The supabase eviction is a delete on a
  pgvector index that can fight readers; in Mongo, deleteMany on the
  unique-PK is tiny and atomic.
- No model fingerprint default trick. The PK is
  ``(url_hash, chunk_index, embed_model)``. Caller must pass the model
  string explicitly; we don't carry the legacy ``None → DEFAULT`` ergonomic
  because a dim-mismatch is much louder than a silent default swap.

Read paths (``match_chunks``, ``match_chunks_windowed``,
``match_chunks_hybrid``) live in M4 — see ``gecko_core.rag.query`` once
that ticket lands.
"""

from __future__ import annotations

import logging
import warnings
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from gecko_core.db.mongo import (
    audit_collection,
    cache_collection,
    chunks_collection,
)
from gecko_core.knowledge.taxonomy import (
    CATEGORIES,
    KNOWLEDGE_SOURCES,
    LEGACY_CATEGORY,
    VERTICALS,
    Category,
    KnowledgeSource,
    Vertical,
)

if TYPE_CHECKING:
    from gecko_core.sources.types import ContentKind, FreshnessTier, ProviderKind

logger = logging.getLogger(__name__)

# Canonical Mongo Atlas Vector Search dim. Doctor (S19-MONGO-INDEX-DIM-CHECK-01)
# imports this to verify the live `chunks_vector` index `numDimensions` matches.
# `EMBED_DIM` is kept as a back-compat alias for the existing chunk validators.
MONGO_VECTOR_DIM_EXPECTED = 1024
EMBED_DIM = MONGO_VECTOR_DIM_EXPECTED

# S20-A2 — compound index for categorized retrieval pre-filter. Vector search
# in Atlas is fed a `(vertical, category, project_id)` $match before the kNN
# stage; this btree index speeds the equivalent client-side analytics queries
# and metadata listings. Index name is exported so tests / doctor can assert
# it exists. Index creation itself happens in Atlas console (Atlas Search /
# btree indexes are managed externally) — see TODO below.
CHUNKS_VERTICAL_CATEGORY_INDEX = "vertical_category_compound"
# TODO(S20-A2): create the compound index on the chunks collection in Atlas:
#   db.chunks.createIndex(
#     { vertical: 1, category: 1, project_id: 1, captured_at: -1 },
#     { name: "vertical_category_compound" }
#   )
# Doctor will fail the chunk_store check until this exists once the
# corresponding doctor probe lands (separate ticket).


class _MongoUnavailable(RuntimeError):
    """Raised when a Mongo write is attempted but the client isn't configured.

    Surfaced by ``insert_chunks_mongo`` / ``put_chunk_cache_mongo`` when
    ``GECKO_CHUNK_STORE=mongo`` is set but ``MONGODB_URI`` is missing.
    Caller (SessionStore) should treat this the same as any other write
    failure — the pipeline's audit row will record ``unknown`` and the
    request fails loudly. Better than a silent no-op that ships zero
    chunks under a green status code.
    """


# ---------------------------------------------------------------------------
# chunks (insert)
# ---------------------------------------------------------------------------


async def insert_chunks_mongo(
    session_id: UUID,
    source_id: UUID,
    chunks: list[tuple[int, str, list[float]]],
    *,
    category: Category | None = None,
    subcategory: str | None = None,
    vertical: Vertical | None = None,
    source: KnowledgeSource | None = None,
    confidence: float = 0.0,
    provider_kind: ProviderKind | None = None,
    project_id: UUID | None = None,
    source_url: str | None = None,
    deprecated: bool = False,
    freshness_tier: FreshnessTier = "static",
    protocol: list[str] | tuple[str, ...] = (),
    content_kind: ContentKind = "unknown",
    is_stale: bool = False,
    as_of_date: str | None = None,
    metadata_extra: dict[str, Any] | None = None,
) -> int:
    """Bulk-insert chunks. Returns count of *new* documents written.

    Uses ``insert_many(ordered=False)`` so duplicate-key errors on the
    ``(source_id, chunk_index)`` unique index are tolerated — a re-run of
    the same source produces zero net new docs but doesn't raise.
    Pre-flight validation mirrors the Supabase path.

    S20-A2 — every chunk now carries a ``(category, vertical, source)``
    triple plus a ``metadata`` sub-document. The new keyword args are
    REQUIRED for new callers; ``provider_kind`` is the deprecated alias
    for ``source`` and emits a ``DeprecationWarning`` for one sprint
    (S21 removes it).

    S33-#68 — ``as_of_date`` carries the data's as-of date (a string day
    bucket, e.g. ``"2026-05-16"``), distinct from ``captured_at`` (the
    ingest wall-clock). ``None`` when the caller doesn't supply it.
    """
    if not chunks:
        return 0

    from gecko_core.ingestion.exceptions import ChunkValidationError

    # ------------------------------------------------------------------
    # S20-A2 backwards-compat shim: alias provider_kind -> source.
    # ------------------------------------------------------------------
    legacy_path = source is None and provider_kind is not None
    if legacy_path:
        warnings.warn(
            "insert_chunks_mongo: `provider_kind` is deprecated — pass "
            "`source` (and `category`, `vertical`) explicitly. The alias "
            "will be removed in S21.",
            DeprecationWarning,
            stacklevel=2,
        )
        # provider_kind values overlap with KnowledgeSource for the common
        # cases; downstream validator checks the enum, so fall back if not.
        source = (
            provider_kind  # type: ignore[assignment]
            if provider_kind in KNOWLEDGE_SOURCES
            else "web"
        )

    # Backwards-compat: pre-S20 callers (no category/vertical AND only
    # provider_kind, or nothing at all) get safe defaults plus a
    # deprecation warning. New-style callers that pass `source` MUST also
    # pass category and vertical — partial-new is treated as a bug.
    pre_s20_call = (
        source is None and provider_kind is None and category is None and vertical is None
    ) or legacy_path
    if pre_s20_call and (category is None or vertical is None):
        warnings.warn(
            "insert_chunks_mongo: missing `source`/`category`/`vertical` — "
            "defaulting to ('web','market_intelligence','unknown'). New "
            "callers MUST pass these explicitly; defaults will be removed "
            "in S21.",
            DeprecationWarning,
            stacklevel=2,
        )
        if source is None:
            source = "web"
        if category is None:
            category = "market_intelligence"
        if vertical is None:
            vertical = "unknown"

    # ------------------------------------------------------------------
    # Required-field validation — mirror ChunkValidationError contract.
    # ------------------------------------------------------------------
    if category is None:
        raise ChunkValidationError(
            "missing required field: category",
            kind="missing_category",
        )
    if vertical is None:
        raise ChunkValidationError(
            "missing required field: vertical",
            kind="missing_vertical",
        )
    if source is None:
        raise ChunkValidationError(
            "missing required field: source",
            kind="missing_source",
        )
    if category not in CATEGORIES:
        raise ChunkValidationError(
            f"invalid category={category!r}; expected one of {CATEGORIES}",
            kind="invalid_category",
        )
    # S20-A3 — legacy_uncategorized is reserved for the revoke script.
    # New ingestions must NOT land in this bucket; the only way to write
    # `legacy_uncategorized` is to also flip metadata.deprecated=True
    # (the revoke path's contract). This prevents quiet drift where an
    # ingestion bug silently dumps real chunks into the legacy bucket and
    # they then get filtered out of default retrieval.
    if category == LEGACY_CATEGORY and not deprecated:
        raise ChunkValidationError(
            "category=legacy_uncategorized is reserved for the A3 revoke "
            "script; new ingests must use one of the canonical 7 categories.",
            kind="invalid_category",
        )
    if vertical not in VERTICALS:
        raise ChunkValidationError(
            f"invalid vertical={vertical!r}; expected one of {VERTICALS}",
            kind="invalid_vertical",
        )
    if source not in KNOWLEDGE_SOURCES:
        raise ChunkValidationError(
            f"invalid source={source!r}; expected one of {KNOWLEDGE_SOURCES}",
            kind="invalid_source",
        )

    for idx, text, embedding in chunks:
        if not text or not text.strip():
            raise ChunkValidationError(
                f"chunk_index={idx} has empty/whitespace text",
                kind="empty_text",
            )
        if len(embedding) != EMBED_DIM:
            raise ChunkValidationError(
                f"chunk_index={idx} embedding dim {len(embedding)} != expected {EMBED_DIM}",
                kind="dim_mismatch",
            )

    # S29-#32 — chunk-quality invariant. Per founder principle:
    # re-ingestion is cheap; garbage chunks are the real liability. The
    # gate is a single function call per chunk and runs AFTER the cheap
    # dim/empty validators so structural errors still raise loudly.
    # Failed chunks are filtered out + counted; the batch as a whole is
    # NOT failed (re-ingestion of a 1000-doc PDF should not be wasted by
    # one binary page). Quality skips surface via structured log alongside
    # the existing pioneer_call signal — keeping the int return contract
    # stable per the documented "structured-log over return-type churn"
    # convention earlier in this file.
    from gecko_core.ingestion.chunk_quality import assess_chunk_quality

    quality_skips: dict[str, int] = {}
    surviving_chunks: list[tuple[int, str, list[float]]] = []
    for idx, text, embedding in chunks:
        verdict = assess_chunk_quality(text)
        if verdict.is_substantive:
            surviving_chunks.append((idx, text, embedding))
            continue
        quality_skips[verdict.reason] = quality_skips.get(verdict.reason, 0) + 1
        logger.warning(
            "mongo.insert_chunks.quality_skip",
            extra={
                "event": "mongo.insert_chunks.quality_skip",
                "session_id": str(session_id),
                "source_id": str(source_id),
                "chunk_index": idx,
                "reason": verdict.reason,
                "text_len": len(text),
            },
        )
    chunks = surviving_chunks
    if not chunks:
        logger.warning(
            "mongo.insert_chunks.all_skipped",
            extra={
                "event": "mongo.insert_chunks.all_skipped",
                "session_id": str(session_id),
                "source_id": str(source_id),
                "quality_skips": quality_skips,
            },
        )
        return 0

    coll = chunks_collection()
    if coll is None:
        raise _MongoUnavailable(
            "chunks_collection is None — MONGODB_URI unset or motor missing",
        )

    now = datetime.now(UTC)
    # source_url is denormalized onto the chunk doc so M4 read paths don't
    # need a Supabase round-trip per query — sources stay on Supabase, but
    # chunks own their own URL for citation rendering.
    # captured_at is kept (legacy field) AND mirrored into metadata.timestamp
    # for one sprint so existing analytics keep working while readers migrate.
    metadata: dict[str, Any] = {
        "confidence": float(confidence),
        "usage_count": 0,
        "timestamp": now,
        "pioneer": False,
    }
    # S20-A3 — only set deprecated when truthy. Default chunks never carry
    # the key; the revoke script's path stamps it True.
    if deprecated:
        metadata["deprecated"] = True
    # S31-#50 — caller-supplied additive metadata (e.g. {"subkind":
    # "mev_tip_data"}). Merged AFTER the canonical fields so callers cannot
    # clobber confidence / usage_count / timestamp / pioneer / deprecated;
    # any collision is silently dropped to preserve the invariant.
    if metadata_extra:
        for k, v in metadata_extra.items():
            if k in metadata:
                continue
            metadata[k] = v
    docs: list[dict[str, Any]] = [
        {
            "session_id": str(session_id),
            "source_id": str(source_id),
            "source_url": source_url,
            "chunk_index": idx,
            "text": text,
            "embedding": embedding,
            # S20-A2 categorized-knowledge fields.
            "category": category,
            "subcategory": subcategory or "",
            "vertical": vertical,
            "source": source,
            # Legacy alias kept for one sprint (S21 drops it).
            "provider_kind": provider_kind if provider_kind is not None else source,
            "project_id": str(project_id) if project_id is not None else None,
            "captured_at": now,
            # S33-#68 — the data's as-of date (e.g. a protocol_native day
            # bucket "2026-05-16"), distinct from captured_at (ingest
            # wall-clock). None for callers that don't supply it so
            # existing chunk shapes are unchanged.
            "as_of_date": as_of_date,
            # Trading-oracle ingest axis (Pattern A: mirrors SQL CHECK in
            # 20260508130000_chunk_freshness_tier.sql). Defaults to 'static'
            # so existing callers are unaffected.
            "freshness_tier": freshness_tier,
            # Trading-oracle ingest axes (Pattern A: protocol is free-form;
            # content_kind mirrors SQL CHECK in
            # 20260508140000_chunk_protocol_content_kind.sql). Defaults
            # preserve existing behavior for non-trading-oracle callers.
            "protocol": list(protocol),
            "content_kind": content_kind,
            "is_stale": is_stale,
            "metadata": dict(metadata),
        }
        for idx, text, embedding in chunks
    ]

    # S20-A7 — pioneer-cell signal. Mark the batch BEFORE the insert so
    # the cell-density count reflects pre-call state (the chunks we're
    # about to insert haven't landed yet). The decision is per-batch, not
    # per-chunk: every doc here gets the same pioneer flag.
    #
    # Why structured-log over return-type churn: the existing `int` return
    # contract is depended on by SessionStore and the ingestion pipeline.
    # Surfacing pioneer_call via a `mongo.insert_chunks.pioneer` INFO log
    # keeps the call signature stable; the future B3 dispatcher reads the
    # signal from telemetry, not the return value. If we ever need it
    # in-band, the cleanest follow-up is the optional `out` kwarg path.
    from gecko_core.knowledge.pioneer import mark_pioneer_chunks

    docs = await mark_pioneer_chunks(docs, vertical, category, collection=coll)
    pioneer_call = bool(docs and docs[0].get("metadata", {}).get("pioneer"))

    try:
        result = await coll.insert_many(docs, ordered=False)
        inserted = len(result.inserted_ids)
    except Exception as exc:
        # pymongo raises BulkWriteError on duplicate keys with ordered=False;
        # the partial result lives on exc.details. Treat duplicates as
        # "already durable" and count them as inserted from the caller's POV.
        details = getattr(exc, "details", None)
        if isinstance(details, dict):
            n_inserted = int(details.get("nInserted", 0))
            write_errors = details.get("writeErrors", [])
            non_dup = [e for e in write_errors if e.get("code") != 11000]
            if non_dup:
                logger.warning(
                    "mongo.insert_chunks.partial_failure",
                    extra={
                        "session_id": str(session_id),
                        "source_id": str(source_id),
                        "n_inserted": n_inserted,
                        "n_dup_skipped": len(write_errors) - len(non_dup),
                        "n_real_errors": len(non_dup),
                        "first_error_code": non_dup[0].get("code"),
                    },
                )
                raise
            inserted = len(docs)
        else:
            raise

    logger.info(
        "mongo.insert_chunks.done",
        extra={
            "session_id": str(session_id),
            "source_id": str(source_id),
            "n_inbound": len(docs),
            "n_inserted": inserted,
            "category": category,
            "vertical": vertical,
            "source": source,
            # S29-#32 — surface the quality-gate counter so callers (and
            # the audit script) know how many were rejected pre-write.
            "quality_skips": quality_skips,
            "quality_skips_total": sum(quality_skips.values()),
        },
    )
    # S20-A7 — emit the pioneer_call signal as a structured INFO row so
    # the upstream dispatcher (B3) can consume it via log telemetry
    # without an API surface change here.
    logger.info(
        "mongo.insert_chunks.pioneer",
        extra={
            "session_id": str(session_id),
            "source_id": str(source_id),
            "vertical": vertical,
            "category": category,
            "pioneer_call": pioneer_call,
            "n_marked": len(docs) if pioneer_call else 0,
        },
    )
    return inserted


# ---------------------------------------------------------------------------
# chunks (replace-before-insert — S33-#80)
# ---------------------------------------------------------------------------


async def delete_chunks_for_source_mongo(
    *,
    provider_kind: str,
    source_url: str,
    protocol: str | None = None,
) -> int:
    """Delete every chunk for one ingest source. Returns the deleted count.

    S33-#80 — daily re-ingest idempotency. The ``protocol_native`` ingest
    keyed ``source_id`` on ``uuid5(url + day_bucket)``, so a re-run on a
    new calendar day minted a fresh ``source_id`` and the
    ``(source_id, chunk_index)`` unique index let every chunk be inserted
    AGAIN — the corpus accreted one full duplicate set per ingest day
    (verified: 1,807 redundant copies = 19.9% of the dex corpus).

    The fix is replace-before-insert: the ingest calls this with the
    endpoint's ``(provider_kind, source_url, protocol)`` to drop the prior
    day's chunks, then inserts the fresh set. The match is on the stable
    ``source_url`` (NOT ``source_id``, which still varies day-to-day) so a
    re-ingest is a true replace regardless of how ``source_id`` is minted.

    No-op (returns 0) when the chunks collection is unavailable — the
    caller treats that the same as "nothing to delete" and the insert that
    follows will surface the real Mongo-unavailable error.
    """
    coll = chunks_collection()
    if coll is None:
        return 0

    query: dict[str, Any] = {
        "provider_kind": provider_kind,
        "source_url": source_url,
    }
    if protocol is not None:
        # protocol is stored as a list; match the single-element list the
        # protocol_native ingest writes (one protocol slug per endpoint).
        query["protocol"] = [protocol]

    result = await coll.delete_many(query)
    deleted = int(getattr(result, "deleted_count", 0))
    logger.info(
        "mongo.delete_chunks_for_source.done",
        extra={
            "event": "mongo.delete_chunks_for_source.done",
            "provider_kind": provider_kind,
            "source_url": source_url,
            "protocol": protocol,
            "deleted_count": deleted,
        },
    )
    return deleted


# ---------------------------------------------------------------------------
# chunks_write_audit
# ---------------------------------------------------------------------------


async def insert_chunks_write_audit_mongo(
    *,
    session_id: UUID,
    source_id: UUID | None,
    batch_size: int,
    succeeded: int,
    failed: int,
    error_kind: str,
    embed_model: str | None,
) -> None:
    """Best-effort audit insert. Errors are swallowed by design.

    Audit rows must NEVER fail an ingestion request. The classifier already
    surfaced the real outcome via the caller's logger; losing the audit
    document is observability debt, not data loss.
    """
    coll = audit_collection()
    if coll is None:
        return

    doc = {
        "session_id": str(session_id),
        "source_id": str(source_id) if source_id is not None else None,
        "batch_size": batch_size,
        "succeeded": succeeded,
        "failed": failed,
        "error_kind": error_kind,
        "embed_model": embed_model,
        "captured_at": datetime.now(UTC),
    }
    import contextlib

    with contextlib.suppress(Exception):
        await coll.insert_one(doc)


async def chunks_write_audit_rollup_recent_mongo(*, days: int = 7) -> list[dict[str, Any]]:
    """Return [{error_kind, count}] for the last ``days``. Empty on no data."""
    coll = audit_collection()
    if coll is None:
        return []

    from datetime import timedelta

    since = datetime.now(UTC) - timedelta(days=days)
    pipeline: list[dict[str, Any]] = [
        {"$match": {"captured_at": {"$gte": since}}},
        {"$group": {"_id": "$error_kind", "count": {"$sum": 1}}},
        {"$project": {"_id": 0, "error_kind": "$_id", "count": 1}},
    ]
    rows: list[dict[str, Any]] = []
    async for r in coll.aggregate(pipeline):
        rows.append(r)
    rows.sort(key=lambda r: (r["error_kind"] != "none", r["error_kind"]))
    return rows


# ---------------------------------------------------------------------------
# chunk_embedding_cache
# ---------------------------------------------------------------------------


async def get_chunk_cache_mongo(
    url_hash: str,
    indices: list[int],
    *,
    embed_model: str | None = None,
) -> dict[int, list[float]]:
    """Return ``{chunk_index: embedding}`` for cached rows. Empty on miss."""
    if not indices:
        return {}

    coll = cache_collection()
    if coll is None:
        return {}

    query: dict[str, Any] = {
        "url_hash": url_hash,
        "chunk_index": {"$in": indices},
    }
    if embed_model is not None:
        query["embed_model"] = embed_model

    out: dict[int, list[float]] = {}
    evict_indices: list[int] = []
    async for doc in coll.find(query, projection={"chunk_index": 1, "embedding": 1}):
        idx = doc.get("chunk_index")
        emb = doc.get("embedding")
        if idx is None or not isinstance(emb, list):
            continue
        vec = [float(v) for v in emb]
        if len(vec) != EMBED_DIM:
            logger.warning(
                "mongo.cache.dim_mismatch_evict",
                extra={
                    "url_hash": url_hash,
                    "chunk_index": int(idx),
                    "got_dim": len(vec),
                    "expected_dim": EMBED_DIM,
                    "error_kind": "dim_mismatch",
                },
            )
            evict_indices.append(int(idx))
            continue
        out[int(idx)] = vec

    if evict_indices:
        try:
            await evict_chunk_cache_mongo(url_hash, evict_indices)
        except Exception as exc:  # pragma: no cover — best-effort
            logger.info(
                "mongo.cache.evict_failed url_hash=%s err=%s",
                url_hash,
                exc.__class__.__name__,
            )
    return out


async def evict_chunk_cache_mongo(url_hash: str, indices: list[int]) -> None:
    """Delete poisoned cache rows. Mirrors ``_evict_chunk_cache`` semantics."""
    coll = cache_collection()
    if coll is None:
        return
    await coll.delete_many({"url_hash": url_hash, "chunk_index": {"$in": indices}})


async def put_chunk_cache_mongo(
    url_hash: str,
    rows: list[tuple[int, str, list[float]]],
    *,
    embed_model: str | None = None,
) -> None:
    """Upsert cache rows. First-writer-wins on the unique PK.

    Always pass an explicit ``embed_model``. The Mongo schema does NOT
    apply the legacy ``text-embedding-3-small`` default — a None passed
    through here writes ``None`` and the unique key
    ``(url_hash, chunk_index, embed_model=None)`` will collide weirdly
    with later explicit-model writes. Callers (the pipeline) already pass
    the active model from ``IngestionSettings().embed_model``.
    """
    if not rows:
        return
    coll = cache_collection()
    if coll is None:
        return

    model = embed_model or "voyage-3"
    now = datetime.now(UTC)
    operations: list[Any] = []
    from pymongo import UpdateOne

    for idx, text, embedding in rows:
        operations.append(
            UpdateOne(
                {
                    "url_hash": url_hash,
                    "chunk_index": idx,
                    "embed_model": model,
                },
                {
                    "$setOnInsert": {
                        "url_hash": url_hash,
                        "chunk_index": idx,
                        "text": text,
                        "embedding": embedding,
                        "embed_model": model,
                        "cached_at": now,
                    }
                },
                upsert=True,
            )
        )
    if operations:
        await coll.bulk_write(operations, ordered=False)


# ---------------------------------------------------------------------------
# usage_count bump (S20-A6 / S20-A-USAGE-COUNT-01)
# ---------------------------------------------------------------------------


async def bump_usage_counts(chunk_ids: list[str]) -> int:
    """Best-effort ``metadata.usage_count += 1`` for cited chunks.

    REACHABILITY:
      basic.generate / workflows._run_pro_debate
        -> bump_usage_counts(result.cited_doc_ids)  [fire-and-forget task]
        -> chunks_collection().update_many({_id: $in [ObjectId, ...]},
                                           {$inc: {metadata.usage_count: 1}})
        -> Mongo doc's metadata.usage_count is incremented.

    Per S20-A6 contract: usage_count is a CITATION-COUNT signal, not a
    retrieval-count. The caller MUST pass only the chunk_ids that landed
    in the synth's ``cited_doc_ids`` (already validated subset of
    rag_context); we don't filter further here.

    Returns the count of docs Mongo reports as modified. Empty input is a
    cheap zero (no DB call). Mongo errors are caught + logged; the function
    NEVER raises — usage_count is a side-effect on the success path and
    must not crash the user-facing synth response.
    """
    if not chunk_ids:
        return 0

    # Defensively convert each chunk_id string to an ObjectId. Anything that
    # doesn't parse is logged + skipped — keeps a one-bad-id in the list from
    # blanking the whole bump batch.
    from bson import ObjectId
    from bson.errors import InvalidId

    valid_oids: list[ObjectId] = []
    for cid in chunk_ids:
        try:
            valid_oids.append(ObjectId(cid))
        except (InvalidId, TypeError, ValueError):
            redacted = (cid[:8] + "...") if isinstance(cid, str) and len(cid) > 8 else repr(cid)
            logger.warning(
                "mongo.bump_usage.invalid_id",
                extra={"event": "mongo.bump_usage.invalid_id", "chunk_id_redacted": redacted},
            )

    if not valid_oids:
        return 0

    coll = chunks_collection()
    if coll is None:
        logger.warning(
            "mongo.bump_usage.failed",
            extra={
                "event": "mongo.bump_usage.failed",
                "reason": "chunks_collection_unavailable",
                "requested_count": len(chunk_ids),
            },
        )
        return 0

    import time

    t0 = time.monotonic()
    try:
        result = await coll.update_many(
            {"_id": {"$in": valid_oids}},
            {"$inc": {"metadata.usage_count": 1}},
        )
        modified = int(getattr(result, "modified_count", 0))
    except Exception as exc:
        logger.warning(
            "mongo.bump_usage.failed",
            extra={
                "event": "mongo.bump_usage.failed",
                "reason": exc.__class__.__name__,
                "requested_count": len(chunk_ids),
            },
        )
        return 0

    ms_elapsed = (time.monotonic() - t0) * 1000.0
    logger.info(
        "mongo.bump_usage.done",
        extra={
            "event": "mongo.bump_usage.done",
            "requested_count": len(chunk_ids),
            "modified_count": modified,
            "ms_elapsed": round(ms_elapsed, 2),
        },
    )
    return modified


__all__ = [
    "CHUNKS_VERTICAL_CATEGORY_INDEX",
    "EMBED_DIM",
    "MONGO_VECTOR_DIM_EXPECTED",
    "bump_usage_counts",
    "chunks_write_audit_rollup_recent_mongo",
    "delete_chunks_for_source_mongo",
    "evict_chunk_cache_mongo",
    "get_chunk_cache_mongo",
    "insert_chunks_mongo",
    "insert_chunks_write_audit_mongo",
    "put_chunk_cache_mongo",
]
