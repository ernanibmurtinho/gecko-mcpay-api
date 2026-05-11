"""Mongo read paths for the chunk store (S18-MONGO-READ-01).

Mirrors the three Postgres RPCs used by ``rag/query.py`` and
``SessionStore``:

- ``match_chunks(p_session_id, query_embedding, match_count)`` →
  :func:`match_chunks_mongo` — pure cosine via ``$vectorSearch`` with
  ``session_id`` filter.
- ``match_chunks_windowed(query_embedding, window_days, p_project_id, match_count)`` →
  :func:`match_chunks_windowed_mongo` — adds ``project_id`` and
  ``captured_at >= now() - window_days`` filters as a
  ``$vectorSearch.filter`` clause (all those fields are declared as
  ``filter`` paths on the M1 ``chunks_vector`` index).
- ``match_chunks_hybrid(p_session_id, query_embedding, match_count, per_kind_quota)`` →
  :func:`match_chunks_hybrid_mongo` — over-fetches a candidate pool, then
  enforces per-provider quota UNION global top-N in app code. Mirrors
  the Postgres SQL ``UNION ranked + global_rank`` shape from
  ``20260502130000_match_chunks_hybrid.sql``.

Why the per-kind quota lives in Python and not the aggregation pipeline:

- Mongo's ``$vectorSearch`` returns at most ``numCandidates`` rows from
  ANN; you can't run window functions over a vector-search stage's
  output cheaply. Two parallel ``$vectorSearch`` (one global, one per
  kind) plus ``$unionWith`` would work but would double the ANN cost
  per query. Doing the quota in Python over a single 50–80-row pool
  is essentially free at our scale.
- The Postgres function does the same conceptually (CTE + ROW_NUMBER);
  app-side gives identical semantics with simpler debug ergonomics.

Returns dict rows shaped to match the Postgres RPC's return columns
(``source_id``, ``source_url``, ``chunk_index``, ``text``, ``similarity``,
``provider_kind``) so ``RagChunk.model_validate`` works on either store
without a translation layer.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from gecko_core.db.mongo import (
    VECTOR_INDEX_NAME,
    chunks_collection,
)
from gecko_core.knowledge.taxonomy import _coerce_legacy_source

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tunables — over-fetch knobs for the hybrid pool. Mongo's $vectorSearch
# requires numCandidates >= limit, and recommends 10x. We set both so the
# candidate pool is large enough for the per-kind quota math to find
# minority-provider chunks even when web prose dominates.
# ---------------------------------------------------------------------------
_HYBRID_POOL_OVERFETCH = 5  # multiplier on match_count for the candidate pool
_HYBRID_NUM_CANDIDATES_OVERFETCH = 15  # ANN candidate width


def _row_from_doc(doc: dict[str, Any], score: float) -> dict[str, Any]:
    """Translate a Mongo chunk doc + $meta score to the RPC return shape."""
    # S22-N1 — coerce legacy ``pay_sh`` source values on read so old chunks
    # surface as ``paysh_live`` to all downstream consumers. # S23 cleanup.
    raw_source = doc.get("source")
    raw_provider_kind = doc.get("provider_kind", "web")
    return {
        "id": str(doc.get("_id", "")),
        "source_id": doc.get("source_id"),
        "source_url": doc.get("source_url"),
        "chunk_index": doc.get("chunk_index"),
        "text": doc.get("text"),
        # Atlas $vectorSearch with cosine returns a similarity score in
        # [0, 1] already (the `searchScore` is normalised). The Postgres
        # path returns ``1 - cosine_distance``, which is in the same
        # range, so callers receive identical semantics.
        "similarity": float(score),
        "source": _coerce_legacy_source(raw_source) if raw_source is not None else None,
        "provider_kind": _coerce_legacy_source(raw_provider_kind),
        # S26-CITE-03 — per-chunk provider metadata (e.g. tweet_url).
        "metadata": doc.get("metadata") or {},
    }


# ---------------------------------------------------------------------------
# match_chunks (pure vector, session-scoped)
# ---------------------------------------------------------------------------


async def match_chunks_mongo(
    *,
    session_id: UUID,
    query_embedding: list[float],
    match_count: int = 8,
    extra_filter: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Top-K cosine match. Empty list if Mongo unconfigured.

    ``session_id`` is retained in the signature for caller-compat and
    provenance logging only — it is NOT used to filter retrieval. See
    docs/strategy/2026-05-11-retrieval-wedge-sprint.md: chunks ingested
    in one session must be retrievable from any other session (knowledge-
    as-commodity wedge).

    ``extra_filter`` (S20-A3) is merged into the ``$vectorSearch.filter``
    clause — used to exclude ``category=legacy_uncategorized`` /
    ``metadata.deprecated=true`` from default retrieval.
    """
    coll = chunks_collection()
    if coll is None:
        return []

    vs_filter: dict[str, Any] = {}
    if extra_filter:
        vs_filter.update(extra_filter)

    vector_search: dict[str, Any] = {
        "index": VECTOR_INDEX_NAME,
        "path": "embedding",
        "queryVector": query_embedding,
        "numCandidates": max(50, match_count * 10),
        "limit": match_count,
    }
    if vs_filter:
        vector_search["filter"] = vs_filter

    pipeline: list[dict[str, Any]] = [
        {"$vectorSearch": vector_search},
        {
            "$project": {
                "_id": 1,
                "source_id": 1,
                "source_url": 1,
                "chunk_index": 1,
                "text": 1,
                "source": 1,
                "provider_kind": 1,
                "metadata": 1,
                "score": {"$meta": "vectorSearchScore"},
            }
        },
    ]

    rows: list[dict[str, Any]] = []
    async for doc in coll.aggregate(pipeline):
        rows.append(_row_from_doc(doc, doc.get("score", 0.0)))
    return rows


# ---------------------------------------------------------------------------
# match_chunks_windowed (project + temporal filter)
# ---------------------------------------------------------------------------


async def match_chunks_windowed_mongo(
    *,
    query_embedding: list[float],
    window_days: int | None,
    project_id: UUID | None,
    match_count: int = 8,
) -> list[dict[str, Any]]:
    """Vector match scoped to ``(project_id, captured_at >= now-window_days)``.

    ``window_days=None`` or ``<=0`` disables the temporal cap;
    ``project_id=None`` matches across projects (rare).
    """
    coll = chunks_collection()
    if coll is None:
        return []

    filter_clause: dict[str, Any] = {}
    if project_id is not None:
        filter_clause["project_id"] = str(project_id)
    if window_days is not None and window_days > 0:
        from datetime import UTC, datetime, timedelta

        since = datetime.now(UTC) - timedelta(days=window_days)
        filter_clause["captured_at"] = {"$gte": since}

    vector_search: dict[str, Any] = {
        "index": VECTOR_INDEX_NAME,
        "path": "embedding",
        "queryVector": query_embedding,
        "numCandidates": max(50, match_count * 10),
        "limit": match_count,
    }
    if filter_clause:
        vector_search["filter"] = filter_clause

    pipeline: list[dict[str, Any]] = [
        {"$vectorSearch": vector_search},
        {
            "$project": {
                "_id": 1,
                "source_id": 1,
                "source_url": 1,
                "chunk_index": 1,
                "text": 1,
                "captured_at": 1,
                "source": 1,
                "provider_kind": 1,
                "metadata": 1,
                "score": {"$meta": "vectorSearchScore"},
            }
        },
    ]

    rows: list[dict[str, Any]] = []
    async for doc in coll.aggregate(pipeline):
        row = _row_from_doc(doc, doc.get("score", 0.0))
        # Windowed RPC includes captured_at in its return shape.
        row["captured_at"] = doc.get("captured_at")
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# match_chunks_hybrid (per-provider quota UNION global top-N)
# ---------------------------------------------------------------------------


async def match_chunks_hybrid_mongo(
    *,
    session_id: UUID,
    query_embedding: list[float],
    match_count: int = 12,
    per_kind_quota: int = 2,
    extra_filter: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Hybrid retrieval — quota per provider_kind UNION global top-N.

    Algorithm (mirrors ``match_chunks_hybrid`` SQL exactly):

    1. Over-fetch a candidate pool of size ~match_count * 5 by raw
       cosine via $vectorSearch with session filter.
    2. Group candidates by ``provider_kind``, rank each group by
       similarity desc, keep the top ``per_kind_quota`` of each kind.
    3. Union with the global top-``match_count`` from the same pool.
    4. Dedupe by chunk ``_id``, sort by similarity desc, return up to
       ``match_count`` rows.
    """
    coll = chunks_collection()
    if coll is None:
        return []

    pool_size = max(match_count * _HYBRID_POOL_OVERFETCH, 30)
    num_candidates = max(pool_size * _HYBRID_NUM_CANDIDATES_OVERFETCH, 200)

    # `session_id` is no longer a retrieval filter — see
    # docs/strategy/2026-05-11-retrieval-wedge-sprint.md.
    vs_filter: dict[str, Any] = {}
    if extra_filter:
        vs_filter.update(extra_filter)

    vector_search: dict[str, Any] = {
        "index": VECTOR_INDEX_NAME,
        "path": "embedding",
        "queryVector": query_embedding,
        "numCandidates": num_candidates,
        "limit": pool_size,
    }
    if vs_filter:
        vector_search["filter"] = vs_filter

    pipeline: list[dict[str, Any]] = [
        {"$vectorSearch": vector_search},
        {
            "$project": {
                "_id": 1,
                "source_id": 1,
                "source_url": 1,
                "chunk_index": 1,
                "text": 1,
                "source": 1,
                "provider_kind": 1,
                "metadata": 1,
                "score": {"$meta": "vectorSearchScore"},
            }
        },
    ]

    pool: list[dict[str, Any]] = []
    async for doc in coll.aggregate(pipeline):
        pool.append(doc)

    if not pool:
        return []

    # Stable global order: similarity desc.
    pool.sort(key=lambda d: float(d.get("score", 0.0)), reverse=True)

    # 1) Per-kind quota — best `per_kind_quota` per provider_kind.
    quota_picked: dict[str, list[dict[str, Any]]] = {}
    for d in pool:
        kind = d.get("provider_kind", "web")
        bucket = quota_picked.setdefault(kind, [])
        if len(bucket) < max(1, per_kind_quota):
            bucket.append(d)
    quota_docs = [d for bucket in quota_picked.values() for d in bucket]

    # 2) Global top match_count.
    global_docs = pool[:match_count]

    # 3) Union by _id.
    seen_ids: set[Any] = set()
    union: list[dict[str, Any]] = []
    for d in quota_docs + global_docs:
        doc_id = d.get("_id")
        if doc_id in seen_ids:
            continue
        seen_ids.add(doc_id)
        union.append(d)

    # 4) Sort by similarity desc, cap at match_count.
    union.sort(key=lambda d: float(d.get("score", 0.0)), reverse=True)
    capped = union[:match_count]
    return [_row_from_doc(d, d.get("score", 0.0)) for d in capped]


# ---------------------------------------------------------------------------
# match_chunks_filterable (S20-RAG-02 — filterable compound index)
# ---------------------------------------------------------------------------


async def match_chunks_filterable_mongo(
    *,
    query_embedding: list[float],
    vertical: str | None = None,
    categories: list[str] | None = None,
    sources: list[str] | None = None,
    match_count: int = 8,
    include_legacy: bool = False,
) -> list[dict[str, Any]]:
    """S20-RAG-02 — vector match using the filterable compound index.

    Filters are pushed into the ``$vectorSearch.filter`` clause so Atlas
    pre-filters BEFORE the ANN graph traversal. This is the read-side
    twin of the migration script ``scripts/mongo/s20_rag02_filterable_index.py``.
    """
    coll = chunks_collection()
    if coll is None:
        return []

    pipeline = build_filterable_pipeline(
        query_embedding=query_embedding,
        vertical=vertical,
        categories=categories,
        sources=sources,
        match_count=match_count,
        include_legacy=include_legacy,
    )
    pipeline.append(
        {
            "$project": {
                "_id": 1,
                "source_id": 1,
                "source_url": 1,
                "chunk_index": 1,
                "text": 1,
                "vertical": 1,
                "category": 1,
                "source": 1,
                "provider_kind": 1,
                "metadata": 1,
                "score": {"$meta": "vectorSearchScore"},
            }
        }
    )

    rows: list[dict[str, Any]] = []
    async for doc in coll.aggregate(pipeline):
        rows.append(_row_from_doc(doc, doc.get("score", 0.0)))
    return rows


def build_filterable_pipeline(
    *,
    query_embedding: list[float],
    vertical: str | None = None,
    categories: list[str] | None = None,
    sources: list[str] | None = None,
    match_count: int = 8,
    include_legacy: bool = False,
) -> list[dict[str, Any]]:
    """Pure builder mirror of :func:`match_chunks_filterable_mongo` for tests.

    Returns the ``$vectorSearch`` aggregation pipeline (without the
    ``$project`` tail) that would be sent to Atlas, with no I/O.
    Useful for asserting the filter shape without standing up a fake
    Mongo collection.

    Filter semantics:
      - ``vertical=None`` → no vertical filter (cross-vertical retrieval).
      - ``categories`` empty / None → no category filter.
      - ``sources`` empty / None → no source filter.
      - ``include_legacy=False`` (default) → adds ``metadata.deprecated:
        {$ne: true}`` so soft-deleted/legacy chunks are excluded.
      - ``include_legacy=True`` → no deprecated filter.
    """
    filter_clause: dict[str, Any] = {}
    if vertical is not None:
        filter_clause["vertical"] = {"$eq": vertical}
    if categories:
        filter_clause["category"] = {"$in": list(categories)}
    if sources:
        filter_clause["source"] = {"$in": list(sources)}
    if not include_legacy:
        filter_clause["metadata.deprecated"] = {"$ne": True}

    vector_search: dict[str, Any] = {
        "index": VECTOR_INDEX_NAME,
        "path": "embedding",
        "queryVector": query_embedding,
        "numCandidates": max(200, match_count * 20),
        "limit": match_count,
        "exact": False,
    }
    if filter_clause:
        vector_search["filter"] = filter_clause
    return [{"$vectorSearch": vector_search}]


__all__ = [
    "build_filterable_pipeline",
    "match_chunks_filterable_mongo",
    "match_chunks_hybrid_mongo",
    "match_chunks_mongo",
    "match_chunks_windowed_mongo",
]
