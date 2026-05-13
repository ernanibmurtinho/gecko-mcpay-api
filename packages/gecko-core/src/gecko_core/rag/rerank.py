"""Cohere Rerank (S28 #25) — post-retrieval semantic re-scoring for the trade panel.

Pipeline composition (see ``orchestration.trade_panel.retrieve_trade_corpus_chunks``)::

    Atlas $vectorSearch (wide pool, ~180 candidates)
      -> _apply_retrieval_boosts (provider/protocol/date heuristics)
      -> cohere_rerank           (THIS MODULE — semantic top_n=5)
      -> _provider_quota_floor    (NOT reached at top_n=5; quota becomes a no-op)
      -> _format_chunks -> panel

Why this exists: at top_k=15 the panel saw too many tangentially-relevant chunks
and the citation rubric (citRel) plateaued at 0.45. Path D's wide pool + a
strong semantic reranker (Cohere ``rerank-v3.5``) lets us serve only the chunks
the panel actually needs to reason. K drops 15 -> 5; citRel target >=0.7.

Graceful degrade: missing ``COHERE_API_KEY`` -> empty result -> caller falls
through. The wedge survives the unset-key case (matches the Path D baseline).

Cost: $1 / 1000 rerank calls + $0.20 / 1000 reranked docs. At 180 docs/call
the unit cost is ~$0.0012/call — i.e. ~$1.20 / 1000 verdicts. The Mongo cache
TTL'd at 5 min is a defensive screen against duplicate calls within a tight
re-verdict loop (basic-tier refresh cadence etc.), not the primary cost lever.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)


# Constants. Caller may override `top_n`; the rest are pinned for predictable
# cost and to keep the eval comparison stable (Pattern E: every "we tuned X"
# claim needs a fixed comparison point).
RERANK_MODEL: str = "rerank-v3.5"
RERANK_MAX_DOCS: int = 1000          # Cohere API hard cap per docs[]
RERANK_TIMEOUT_S: float = 10.0
RERANK_RETRY_429_BACKOFF_S: float = 0.5
RERANK_CACHE_TTL_S: int = 300        # 5 min — defensive cache against tight re-verdict loops
RERANK_CACHE_COLLECTION: str = "rerank_cache"

# Cost telemetry coefficients (Cohere production pricing 2026-05).
_COHERE_CALL_USD: float = 1.0 / 1000.0          # $1 / 1000 calls
_COHERE_PER_DOC_USD: float = 0.20 / 1000.0      # $0.20 / 1000 reranked docs

# Module-level once-per-process latch for the "no key" warning so logs
# don't spam at high QPS.
_NO_KEY_WARNED: bool = False


def _hash_query(query: str) -> str:
    return hashlib.sha256(query.encode("utf-8")).hexdigest()[:16]


def _hash_documents(documents: list[str]) -> str:
    # Order-sensitive hash — Cohere returns indices into the input array, so
    # reusing a cached result against a different ordering would return wrong
    # indices.
    h = hashlib.sha256()
    for d in documents:
        h.update(d.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()[:24]


def _rerank_cache_coll() -> Any:
    """Resolve the rerank cache collection lazily; ``None`` if Mongo not configured."""
    try:
        from gecko_core.db.mongo import _db  # noqa: PLC2701 — intentional internal access
    except Exception:
        return None
    db = _db()
    if db is None:
        return None
    return db[RERANK_CACHE_COLLECTION]


async def _cache_lookup(cache_key: str) -> list[tuple[int, float]] | None:
    """Best-effort Mongo cache read. Any failure -> miss."""
    coll = _rerank_cache_coll()
    if coll is None:
        return None
    try:
        doc = await coll.find_one({"_id": cache_key})
    except Exception as exc:
        logger.debug("rerank.cache.read_skip err=%s", exc)
        return None
    if not doc:
        return None
    inserted_at = doc.get("inserted_at", 0.0)
    if (time.time() - float(inserted_at)) > RERANK_CACHE_TTL_S:
        return None
    pairs = doc.get("pairs") or []
    out: list[tuple[int, float]] = []
    for p in pairs:
        try:
            out.append((int(p[0]), float(p[1])))
        except (TypeError, ValueError, IndexError):
            continue
    return out or None


async def _cache_write(cache_key: str, pairs: list[tuple[int, float]]) -> None:
    """Best-effort Mongo cache write. Any failure is swallowed."""
    coll = _rerank_cache_coll()
    if coll is None:
        return
    try:
        await coll.update_one(
            {"_id": cache_key},
            {
                "$set": {
                    "_id": cache_key,
                    "pairs": [[int(i), float(s)] for i, s in pairs],
                    "inserted_at": time.time(),
                }
            },
            upsert=True,
        )
    except Exception as exc:
        logger.debug("rerank.cache.write_skip err=%s", exc)


def _emit_event(event: str, **fields: Any) -> None:
    """Structured event log — single line, key=value, grep-friendly."""
    payload = " ".join(f"{k}={v}" for k, v in fields.items())
    logger.info("%s %s", event, payload)


async def cohere_rerank(
    query: str,
    documents: list[str],
    top_n: int = 5,
    model: str = RERANK_MODEL,
) -> list[tuple[int, float]]:
    """Re-score ``documents`` against ``query`` with Cohere ``rerank-v3.5``.

    Returns ``list[(original_index, relevance_score)]`` in rank order, length
    up to ``top_n``. Returns ``[]`` on any soft failure (no key, timeout,
    empty input, exception) so the caller can fall through to its existing
    slate. Never raises for environmental reasons; programmer errors
    (e.g. ``top_n < 1``) still raise.

    Contract:

    * No ``COHERE_API_KEY`` -> emits ``rerank.skipped`` once per process and
      returns ``[]``.
    * Empty ``documents`` -> returns ``[]`` (no API call).
    * Cohere caps ``documents`` at 1000/call — we hard-truncate before send.
    * 10s timeout, one retry on HTTP 429 with 0.5s backoff.
    * Cost telemetry: emits ``rerank.call`` with ``n_docs``, ``top_n``,
      ``cost_estimate_usd``.
    * Mongo cache by ``(query_hash, documents_hash, model, top_n)``,
      5 min TTL.
    """
    if top_n < 1:
        raise ValueError(f"top_n must be >= 1 (got {top_n})")
    if not documents:
        return []

    global _NO_KEY_WARNED
    api_key = (os.environ.get("COHERE_API_KEY") or "").strip()
    if not api_key:
        if not _NO_KEY_WARNED:
            _emit_event("rerank.skipped", reason="no_key")
            _NO_KEY_WARNED = True
        return []

    # Hard-cap docs/call per Cohere API limit.
    if len(documents) > RERANK_MAX_DOCS:
        documents = documents[:RERANK_MAX_DOCS]

    cache_key = f"{_hash_query(query)}:{_hash_documents(documents)}:{model}:{top_n}"
    cached = await _cache_lookup(cache_key)
    if cached is not None:
        _emit_event(
            "rerank.cache_hit",
            n_docs=len(documents),
            top_n=top_n,
            cache_key=cache_key,
        )
        return cached[:top_n]

    try:
        import cohere  # type: ignore[import-not-found]
    except Exception as exc:  # pragma: no cover - import guard
        logger.warning("rerank.import_failed err=%s", exc)
        return []

    started = time.perf_counter()
    client = cohere.AsyncClient(api_key=api_key)
    last_err: Exception | None = None
    raw_result: Any = None
    for attempt in (1, 2):
        try:
            raw_result = await asyncio.wait_for(
                client.rerank(
                    query=query,
                    documents=documents,
                    top_n=min(top_n, len(documents)),
                    model=model,
                ),
                timeout=RERANK_TIMEOUT_S,
            )
            break
        except TimeoutError:
            _emit_event("rerank.fallback", reason="timeout", attempt=attempt)
            return []
        except Exception as exc:
            last_err = exc
            msg = str(exc).lower()
            is_429 = "429" in msg or "rate" in msg
            if is_429 and attempt == 1:
                await asyncio.sleep(RERANK_RETRY_429_BACKOFF_S)
                continue
            _emit_event(
                "rerank.fallback",
                reason="exception",
                attempt=attempt,
                err=type(exc).__name__,
            )
            return []

    if raw_result is None:
        _emit_event(
            "rerank.fallback",
            reason="no_result",
            err=type(last_err).__name__ if last_err else "unknown",
        )
        return []

    cohere_results = getattr(raw_result, "results", None) or []
    pairs: list[tuple[int, float]] = []
    for r in cohere_results:
        idx = getattr(r, "index", None)
        score = getattr(r, "relevance_score", None)
        if not isinstance(idx, int) or idx < 0 or idx >= len(documents):
            continue
        try:
            pairs.append((idx, float(score) if score is not None else 0.0))
        except (TypeError, ValueError):
            continue
        if len(pairs) >= top_n:
            break

    wall_ms = int((time.perf_counter() - started) * 1000)
    cost_usd = _COHERE_CALL_USD + (_COHERE_PER_DOC_USD * len(documents))
    _emit_event(
        "rerank.call",
        n_docs=len(documents),
        top_n=top_n,
        returned=len(pairs),
        wall_ms=wall_ms,
        cost_estimate_usd=f"{cost_usd:.6f}",
    )

    if pairs:
        await _cache_write(cache_key, pairs)
    return pairs


__all__ = [
    "RERANK_MODEL",
    "RERANK_MAX_DOCS",
    "RERANK_TIMEOUT_S",
    "RERANK_CACHE_TTL_S",
    "cohere_rerank",
]
