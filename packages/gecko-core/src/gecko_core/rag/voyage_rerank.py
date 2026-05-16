"""Voyage AI post-retrieval reranker (S19-VOYAGE-RERANK-01).

Pipeline composition (see `rag.query.rag_query`):

    Mongo $vectorSearch / Postgres match_chunks_hybrid  (over-fetch top 2*K)
        -> _rerank_by_provider (provider boost + per-kind quota rescue)
        -> voyage_rerank        (this module — semantic re-scoring)
        -> top_n returned to synthesizer

Why two reranks: the per-provider quota stage is *structural* — it guarantees
wedge providers (Bazaar, twit.sh, Arxiv) survive into the slate even when
they would lose on raw cosine. Voyage's `rerank-2` is *semantic* — given the
slate is already shape-balanced, Voyage re-orders by query relevance. The
order matters: if Voyage runs before quota rescue, structurally-relevant
provider chunks can be evicted before the rescue gets a chance.

Flag-gated (`GECKO_RERANKER=none|voyage`, default `none`). Lazy-imports
`voyageai` so the legacy install path doesn't require the optional extra.
Graceful degrade on any failure: timeouts, 5xx, missing key, missing
package — all return the input slate truncated to `top_n`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gecko_core.rag.query import RagChunk

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Knobs. These are constants (not env-tunable) on purpose — the S19 plan
# pinned K=20 and N=8 explicitly so the A/B in R3 has a fixed comparison
# point. Operators retune via the surrounding rag_query parameters, not
# these.
# ---------------------------------------------------------------------------
RERANK_MODEL = "rerank-2"
RERANK_TOP_K_INPUT = 20  # ceiling sent to Voyage; bounds latency + cost
RERANK_TOP_N_OUTPUT = 8  # default returned slate when caller doesn't override
RERANK_TIMEOUT_S = 2.5  # graceful-degrade boundary; see plan §3 risk #1


def _flag_enabled() -> bool:
    """Return True iff `GECKO_RERANKER` env var equals 'voyage' (case-insensitive)."""
    return (os.environ.get("GECKO_RERANKER") or "none").strip().lower() == "voyage"


async def voyage_rerank(
    query: str,
    chunks: list[RagChunk],
    top_n: int = RERANK_TOP_N_OUTPUT,
) -> list[RagChunk]:
    """Re-score `chunks` against `query` using Voyage `rerank-2`, return top_n.

    Contract:

    * If `GECKO_RERANKER` != "voyage": no-op, returns `chunks[:top_n]`.
    * If `VOYAGE_API_KEY` unset: warn, returns `chunks[:top_n]`.
    * If `voyageai` package not installed: warn, returns `chunks[:top_n]`.
    * If the API call times out (>2.5s) or raises: warn, returns `chunks[:top_n]`.
    * On success: returns up to `top_n` chunks ordered by Voyage's
      `relevance_score`, with each surviving chunk's `rerank_score`
      populated. **`similarity` is preserved as-is** so the downstream
      citation-grounding floor still reads on the cosine scale.

    Input is capped at `RERANK_TOP_K_INPUT=20` (top of the input list)
    before Voyage is called — the plan explicitly bounds this for latency.
    """
    if not chunks:
        return []
    if top_n <= 0:
        return []
    if not _flag_enabled():
        return chunks[:top_n]

    api_key = os.environ.get("VOYAGE_API_KEY", "").strip()
    if not api_key:
        logger.warning("rag.voyage_rerank.no_key (falling back to input slate)")
        return chunks[:top_n]

    # Cap the input passed to Voyage. The plan pins K=20.
    candidates = chunks[:RERANK_TOP_K_INPUT]

    # Lazy import — keeps the legacy install slim. Any ImportError here is
    # a graceful-degrade signal (extra not installed), not a crash.
    try:
        import voyageai  # type: ignore[import-not-found]
    except Exception as exc:  # pragma: no cover - import guard
        logger.warning("rag.voyage_rerank.import_failed err=%s", exc)
        return chunks[:top_n]

    documents = [c.text for c in candidates]

    try:
        client = voyageai.AsyncClient(api_key=api_key)
        # NB: `top_k` here is Voyage's "return at most N results" — we ask
        # for the full slate back so we can decide our own truncation,
        # which keeps the trim policy in one place.
        result = await asyncio.wait_for(
            client.rerank(
                query=query,
                documents=documents,
                model=RERANK_MODEL,
                top_k=len(documents),
            ),
            timeout=RERANK_TIMEOUT_S,
        )
    except TimeoutError:
        logger.warning("rag.voyage_rerank.fallback err=timeout timeout_s=%.2f", RERANK_TIMEOUT_S)
        return chunks[:top_n]
    except Exception as exc:
        logger.warning("rag.voyage_rerank.fallback err=%s", exc)
        return chunks[:top_n]

    # Voyage shape: result.results: list[ {index, relevance_score, document?} ]
    voyage_results = getattr(result, "results", None)
    if not voyage_results:
        logger.warning("rag.voyage_rerank.fallback err=empty_results")
        return chunks[:top_n]

    reordered: list[RagChunk] = []
    for r in voyage_results:
        idx = getattr(r, "index", None)
        score = getattr(r, "relevance_score", None)
        if idx is None or not isinstance(idx, int) or idx < 0 or idx >= len(candidates):
            continue
        chunk = candidates[idx]
        # Preserve `similarity`; surface Voyage score as a side-channel.
        reordered.append(
            chunk.model_copy(update={"rerank_score": float(score) if score is not None else None})
        )
        if len(reordered) >= top_n:
            break

    if not reordered:
        # Voyage returned results but none were usable — fall back rather
        # than ship an empty slate.
        logger.warning("rag.voyage_rerank.fallback err=no_usable_results")
        return chunks[:top_n]

    return reordered


async def voyage_rerank_batch(
    items: list[tuple[str, list[RagChunk]]],
    *,
    top_n: int = RERANK_TOP_N_OUTPUT,
) -> list[list[RagChunk]]:
    """Parallel-rerank N (query, chunks) pairs in one fan-out (S20-RERANKER-BATCH-01).

    Each tuple's chunks slate is reranked independently against its own
    query; results are returned in input order. Per-item failures degrade
    locally to ``chunks[:top_n]`` so one bad call never poisons the rest.

    Why this exists: pro-tier multi-question synthesis can fan out N
    `voyage_rerank` calls per session. The Voyage SDK has no
    multi-query-single-call endpoint, so "batching" here means dispatching
    all N calls concurrently via ``asyncio.gather`` rather than awaiting
    them sequentially. On a 5-question fan-out at 300-800ms/call this
    reclaims ~2-3s of latency budget.

    Contract:

    * Empty ``items`` -> returns ``[]`` without invoking Voyage.
    * Flag off (``GECKO_RERANKER`` != "voyage") -> per-item passthrough
      to ``chunks[:top_n]`` without invoking Voyage at all.
    * On per-item exception -> log a warning, degrade *that item only* to
      ``chunks[:top_n]`` (the other items still see their reranked slates).
    * Output length and order match input exactly: ``output[i]`` is the
      reranked slate for ``items[i]``.

    Note: this helper is additive. ``voyage_rerank`` semantics for the
    single-question path are unchanged, and the basic-tier (single-question)
    path continues to use ``voyage_rerank`` directly.
    """
    if not items:
        return []

    # Fast-path: when the reranker is off, never construct coroutines that
    # would spin up a Voyage client. Cheaper and matches single-call
    # passthrough semantics exactly.
    if not _flag_enabled():
        return [chunks[:top_n] for _, chunks in items]

    started = time.perf_counter()
    coros = [voyage_rerank(q, c, top_n=top_n) for q, c in items]
    raw = await asyncio.gather(*coros, return_exceptions=True)

    out: list[list[RagChunk]] = []
    failures = 0
    for i, result in enumerate(raw):
        if isinstance(result, BaseException):
            failures += 1
            logger.warning(
                "rag.voyage_rerank.batch.item_failed idx=%d err=%s",
                i,
                result,
            )
            out.append(items[i][1][:top_n])
        else:
            out.append(result)

    wall_ms = int((time.perf_counter() - started) * 1000)
    voyage_calls = len(items) - failures
    logger.info(
        "voyage_rerank.batch.done n_items=%d voyage_calls=%d wall_ms=%d",
        len(items),
        voyage_calls,
        wall_ms,
    )
    return out


async def voyage_rerank_dicts(
    query: str,
    chunks: list[dict[str, Any]],
    top_n: int = RERANK_TOP_N_OUTPUT,
    *,
    text_key: str = "text",
    score_key: str = "rerank_score",
) -> list[dict[str, Any]]:
    """Re-score plain-dict chunks against ``query`` using Voyage ``rerank-2``.

    Dict-native sibling of :func:`voyage_rerank` for retrieval paths that
    carry chunks as plain dicts rather than ``RagChunk`` models — notably
    ``orchestration.trade_panel.retrieve_trade_corpus_chunks`` (S33-#79).
    Converting those dicts into ``RagChunk`` just to rerank would force
    synthesising required fields (``source_id`` UUID, ``chunk_index``); this
    helper avoids that round-trip and keeps the Voyage call logic in one
    module.

    Contract mirrors :func:`voyage_rerank` exactly:

    * If ``GECKO_RERANKER`` != "voyage": no-op, returns ``chunks[:top_n]``.
    * If ``VOYAGE_API_KEY`` unset / ``voyageai`` missing / call times out or
      raises / Voyage returns empty: warn, return ``chunks[:top_n]`` (the
      input vector-order slate). Retrieval never breaks on a rerank failure.
    * On success: returns up to ``top_n`` dicts ordered by Voyage's
      ``relevance_score``; each surviving dict gets ``score_key`` populated.
      The original cosine ``score`` field is left untouched.

    Input is capped at ``RERANK_TOP_K_INPUT`` before Voyage is called.
    """
    if not chunks:
        return []
    if top_n <= 0:
        return []
    if not _flag_enabled():
        return chunks[:top_n]

    api_key = os.environ.get("VOYAGE_API_KEY", "").strip()
    if not api_key:
        logger.warning("rag.voyage_rerank_dicts.no_key (falling back to input slate)")
        return chunks[:top_n]

    candidates = chunks[:RERANK_TOP_K_INPUT]

    try:
        import voyageai
    except Exception as exc:  # pragma: no cover - import guard
        logger.warning("rag.voyage_rerank_dicts.import_failed err=%s", exc)
        return chunks[:top_n]

    documents = [str(c.get(text_key) or "") for c in candidates]

    try:
        client = voyageai.AsyncClient(api_key=api_key)  # type: ignore[attr-defined]
        result = await asyncio.wait_for(
            client.rerank(
                query=query,
                documents=documents,
                model=RERANK_MODEL,
                top_k=len(documents),
            ),
            timeout=RERANK_TIMEOUT_S,
        )
    except TimeoutError:
        logger.warning(
            "rag.voyage_rerank_dicts.fallback err=timeout timeout_s=%.2f",
            RERANK_TIMEOUT_S,
        )
        return chunks[:top_n]
    except Exception as exc:
        logger.warning("rag.voyage_rerank_dicts.fallback err=%s", exc)
        return chunks[:top_n]

    voyage_results = getattr(result, "results", None)
    if not voyage_results:
        logger.warning("rag.voyage_rerank_dicts.fallback err=empty_results")
        return chunks[:top_n]

    reordered: list[dict[str, Any]] = []
    for r in voyage_results:
        idx = getattr(r, "index", None)
        score = getattr(r, "relevance_score", None)
        if idx is None or not isinstance(idx, int) or idx < 0 or idx >= len(candidates):
            continue
        chunk = dict(candidates[idx])
        chunk[score_key] = float(score) if score is not None else None
        reordered.append(chunk)
        if len(reordered) >= top_n:
            break

    if not reordered:
        logger.warning("rag.voyage_rerank_dicts.fallback err=no_usable_results")
        return chunks[:top_n]

    return reordered


__all__ = [
    "RERANK_MODEL",
    "RERANK_TIMEOUT_S",
    "RERANK_TOP_K_INPUT",
    "RERANK_TOP_N_OUTPUT",
    "voyage_rerank",
    "voyage_rerank_batch",
    "voyage_rerank_dicts",
]
