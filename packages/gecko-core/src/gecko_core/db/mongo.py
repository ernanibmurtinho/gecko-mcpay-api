"""MongoDB Atlas client for the chunk store (S18-MONGO-DRIVER-01).

Distinct from ``gecko_core.cache.mongo`` (TTL'd provider caches in
``gecko_cache`` DB) and ``gecko_core.orchestration.transcripts`` (judge
transcripts in ``gecko_cache.judge_transcripts``). This module owns the
``gecko_rag`` database: ``chunks``, ``chunk_embedding_cache``,
``chunks_write_audit``.

Why a separate module: the chunk store has a different lifecycle (no TTL,
vector + Atlas Search indexes, schema-drift test against Python types),
different DB name, and is feature-flagged behind ``GECKO_CHUNK_STORE``.
Mixing it with the cache module would couple them and make the M1→M5
flag flip risk a regression in the cache path.

Connection strategy:
- Reuses ``MONGODB_URI`` from the existing transcript/cache config.
- Database default is ``gecko_rag`` (override via ``MONGODB_CHUNK_DB``).
- One ``AsyncIOMotorClient`` per process, lru_cached so coroutines share
  the connection pool. Pool sizing follows motor defaults; tune in S20+
  per the ``mongodb:mongodb-connection`` skill if pool exhaustion shows up
  under concurrent ingestion.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from motor.motor_asyncio import (
        AsyncIOMotorClient,
        AsyncIOMotorCollection,
        AsyncIOMotorDatabase,
    )

CHUNK_DB_DEFAULT = "gecko_rag"
CHUNKS_COLLECTION = "chunks"
CACHE_COLLECTION = "chunk_embedding_cache"
AUDIT_COLLECTION = "chunks_write_audit"

VECTOR_INDEX_NAME = "chunks_vector"
SEARCH_INDEX_NAME = "chunks_text"

# S20-RAG-02 — canonical list of filter-field paths declared on the Atlas
# Search vector index ``chunks_vector``. Single source of truth: the
# index-management script (``scripts/mongo/s20_rag02_filterable_index.py``)
# builds the index from this tuple, and the doctor probe asserts the live
# index advertises these exact paths. Add a path here, the migration script
# picks it up on the next ``--apply --rebuild`` run.
#
# Why these specifically:
#   - ``vertical`` / ``category`` — pre-filter the categorized cell before
#     ANN. The wedge of S20's RAG architecture (multi-tenant by vertical).
#   - ``source`` — KnowledgeSource (web, bazaar, twit_sh…). Lets callers
#     scope to paid vs free providers without a post-ANN $match.
#   - ``provider_kind`` — legacy-compatible provider attribution
#     (web/youtube/bazaar/arxiv/twitsh/hn/reddit/gecko_precedent). Kept
#     filterable for hybrid retrieval that pre-filters by provider class.
#   - ``metadata.deprecated`` — the soft-delete flag for legacy chunks
#     (the A3 ``include_legacy=False`` default excludes these).
#
# NB: ``session_id`` and ``project_id`` are also declared filterable on the
# index for the legacy match_chunks paths; they predate S20 and live in
# ``CHUNKS_VECTOR_LEGACY_FILTER_FIELDS`` to keep the new compound list
# auditable on its own.
CHUNKS_VECTOR_FILTER_FIELDS: tuple[str, ...] = (
    "vertical",
    "category",
    "source",
    "provider_kind",
    "metadata.deprecated",
)

CHUNKS_VECTOR_LEGACY_FILTER_FIELDS: tuple[str, ...] = (
    "session_id",
    "project_id",
    "captured_at",
)


def mongo_uri(env: dict[str, str] | None = None) -> str | None:
    """Return the configured Mongo URI or None if unset.

    Mirrors ``gecko_core.cache.mongo._mongo_uri`` so both stores respect
    the same SSM ``__unset__`` sentinel.

    ``env`` lets callers (e.g. ``gecko-mcp doctor`` which takes an
    ``environ=`` override for tests) route through the canonical accessor
    instead of redeclaring the env-var contract inline. S31-#48 Pattern A.
    """
    src = env if env is not None else os.environ
    uri = src.get("MONGODB_URI") or src.get("MONGO_URI")
    if not uri or uri == "__unset__":
        return None
    return uri


def is_chunk_store_configured() -> bool:
    """True iff the chunk-store flag is ``mongo`` AND a URI is reachable."""
    if os.environ.get("GECKO_CHUNK_STORE", "supabase").lower() != "mongo":
        return False
    return mongo_uri() is not None


def chunk_db_name(env: dict[str, str] | None = None) -> str:
    """Return the chunk-store DB name, honoring ``MONGODB_CHUNK_DB``.

    Accepts an optional env mapping for the same reason as ``mongo_uri()``.
    """
    src = env if env is not None else os.environ
    return src.get("MONGODB_CHUNK_DB", CHUNK_DB_DEFAULT)


@lru_cache(maxsize=1)
def _client() -> AsyncIOMotorClient[Any] | None:
    uri = mongo_uri()
    if not uri:
        return None
    try:
        from motor.motor_asyncio import AsyncIOMotorClient

        return AsyncIOMotorClient(uri)
    except ImportError:
        return None


def _db() -> AsyncIOMotorDatabase[Any] | None:
    client = _client()
    if client is None:
        return None
    return client[chunk_db_name()]


def chunks_collection() -> AsyncIOMotorCollection[Any] | None:
    db = _db()
    return None if db is None else db[CHUNKS_COLLECTION]


def cache_collection() -> AsyncIOMotorCollection[Any] | None:
    db = _db()
    return None if db is None else db[CACHE_COLLECTION]


def audit_collection() -> AsyncIOMotorCollection[Any] | None:
    db = _db()
    return None if db is None else db[AUDIT_COLLECTION]


__all__ = [
    "AUDIT_COLLECTION",
    "CACHE_COLLECTION",
    "CHUNKS_COLLECTION",
    "CHUNKS_VECTOR_FILTER_FIELDS",
    "CHUNKS_VECTOR_LEGACY_FILTER_FIELDS",
    "CHUNK_DB_DEFAULT",
    "SEARCH_INDEX_NAME",
    "VECTOR_INDEX_NAME",
    "audit_collection",
    "cache_collection",
    "chunk_db_name",
    "chunks_collection",
    "is_chunk_store_configured",
    "mongo_uri",
]
