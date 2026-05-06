"""RAG query — session-scoped chunk similarity (Supabase or Mongo).

Embeds the user question in the **same** space as stored chunk vectors:
``embed_for_postgres_vector`` (OpenAI 1536) when ``GECKO_CHUNK_STORE`` is
``supabase``; :func:`gecko_core.ingestion.embedder.embed` (Voyage 1024 by
default) when the chunk store is ``mongo`` so $vectorSearch matches ingest.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, cast
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from gecko_core.db import get_chunk_store
from gecko_core.ingestion.embedder import (
    POSTGRES_EMBED_MODEL,
    embed,
    embed_for_postgres_vector,
    estimate_embed_cost_usd,
)
from gecko_core.ingestion.settings import get_ingestion_settings
from gecko_core.models import _validate_citation_uri
from gecko_core.rag.self_citation import (
    SELF_CITATION_DOWNWEIGHT,
    is_self_citation,
    is_self_referential_idea,
)
from gecko_core.sessions.store import SessionStore
from gecko_core.sources.types import PROVIDER_KINDS, ProviderKind

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Per-provider retrieval boost (S17-WEDGE-CITE-03 Part B)
#
# Why a Python-side reranker instead of a SQL-side weight:
#   match_chunks runs raw cosine similarity. Structured providers (Bazaar,
#   Arxiv abstracts, twit.sh tweets) are short, dense, and shape-mismatched
#   against the long-form web prose Tavily ships — they systematically
#   under-score in raw cosine even when topically on-target. Boosting in
#   Python keeps the SQL hot path cheap and the weights tunable without a DB
#   redeploy. Defaults represent a paid-context premium signal: Bazaar is
#   the strongest (paid + curated), Arxiv is moderate (free but
#   peer-reviewed), twit.sh is slightly down-weighted (high signal but
#   noisy across a session).
#
# Env overrides exist so dogfood operators can A/B without a code change.
# ---------------------------------------------------------------------------
_DEFAULT_PROVIDER_WEIGHTS: dict[str, float] = {
    "web": 1.0,
    "youtube": 1.0,
    "bazaar": 1.15,
    "arxiv": 1.10,
    "twitsh": 0.95,
    "hn": 1.0,
    "reddit": 1.0,
    "gecko_precedent": 1.0,
}


def _resolve_provider_weights() -> dict[str, float]:
    """Build the active weight table, applying GECKO_RETRIEVAL_WEIGHT_* env overrides.

    Env var name = ``GECKO_RETRIEVAL_WEIGHT_<UPPERCASE_PROVIDER_KIND>``.
    Invalid floats are logged and ignored — the default wins. Missing
    entries fall back to 1.0 so a new provider added to ProviderKind
    before this dict is updated still scores at baseline.
    """
    weights = dict(_DEFAULT_PROVIDER_WEIGHTS)
    for kind in PROVIDER_KINDS:
        env_name = f"GECKO_RETRIEVAL_WEIGHT_{kind.upper()}"
        raw = os.environ.get(env_name)
        if raw is None:
            continue
        try:
            weights[kind] = float(raw)
        except ValueError:
            logger.warning(
                "rag.weights.invalid_env env=%s value=%r (using default %.2f)",
                env_name,
                raw,
                weights.get(kind, 1.0),
            )
    return weights


def _resolve_per_kind_quota() -> int:
    """Resolve the per-provider-kind quota for the hybrid retrieval RPC.

    The SQL function takes a ``per_kind_quota`` argument naming the minimum
    number of best-cosine rows to surface for *each* provider_kind present
    in the session. Defaults to 2 — enough for the LLM to see structured
    provenance without crowding out the global top-N. Operators can tune
    via ``GECKO_RETRIEVAL_PER_KIND_QUOTA`` for dogfood experiments.
    """
    raw = os.environ.get("GECKO_RETRIEVAL_PER_KIND_QUOTA")
    if raw is None:
        return 2
    try:
        v = int(raw)
        return max(1, v)
    except ValueError:
        logger.warning(
            "rag.per_kind_quota.invalid_env value=%r (using default 2)",
            raw,
        )
        return 2


def _rerank_by_provider(
    chunks: list[RagChunk],
    top_k: int,
    *,
    reserve_quota: int | None = None,
    self_citation_active: bool = False,  # S20-SELF-CITATION-GUARD-01
) -> list[RagChunk]:
    """Apply per-provider boosts to similarity, re-sort, return top_k.

    Pure function — leaves the input list alone. Mutates each chunk's
    `similarity` to the boosted value so downstream consumers (citation
    grounding floor, low-grounding gate) see the post-boost score. Per the
    design memo §3.2 we keep the existing `[N] <uri>` citation shape; the
    URI scheme already encodes provider, so no new field is needed for
    the renderer to surface provenance.

    ``reserve_quota`` (S17-WEDGE-CITE-04): when set, guarantees that up to
    ``reserve_quota`` chunks of each *non-web* provider_kind present in the
    input survive into the returned top_k, even if their boosted similarity
    would otherwise lose to a flood of web chunks. The boost alone isn't
    enough — bazaar 1.15 * 0.55 < web 1.0 * 0.85 in realistic data, so a
    reordering-only rerank silently drops the wedge providers. The quota
    is the structural fix the demo claim 'Bazaar shapes the verdict' needs.
    """
    if not chunks:
        return chunks
    weights = _resolve_provider_weights()
    boosted: list[RagChunk] = []
    for c in chunks:
        w = weights.get(c.provider_kind, 1.0)
        # S20-SELF-CITATION-GUARD-01 — when the idea is itself about Gecko's
        # product space, multiplicatively down-weight chunks resolving to
        # Gecko-owned domains. Soft guard, not a filter: the chunk stays
        # eligible but loses the structural advantage of self-citation
        # circularity. Stacks with the per-provider boost.
        if self_citation_active and is_self_citation(c.source_url):
            w *= SELF_CITATION_DOWNWEIGHT
        if w == 1.0:
            boosted.append(c)
            continue
        new_sim = max(0.0, min(1.0, c.similarity * w))
        boosted.append(c.model_copy(update={"similarity": new_sim}))
    boosted.sort(key=lambda c: c.similarity, reverse=True)

    if reserve_quota is None or reserve_quota <= 0:
        return boosted[:top_k]

    # Quota pass: pick the best ``reserve_quota`` non-web chunks per kind
    # first, then fill remaining slots with the global top of the boosted
    # list. Dedup by RagChunk identity (object id) — two RagChunks for
    # the same DB row are not produced upstream so id() suffices and
    # avoids leaning on chunk.source_id+chunk_index hashing semantics.
    selected: list[RagChunk] = []
    seen: set[int] = set()
    per_kind: dict[str, int] = {}
    for c in boosted:
        if c.provider_kind == "web":
            continue
        n = per_kind.get(c.provider_kind, 0)
        if n >= reserve_quota:
            continue
        per_kind[c.provider_kind] = n + 1
        selected.append(c)
        seen.add(id(c))
        if len(selected) >= top_k:
            break
    # Fill the rest from global boosted ordering.
    for c in boosted:
        if len(selected) >= top_k:
            break
        if id(c) in seen:
            continue
        selected.append(c)
        seen.add(id(c))
    selected.sort(key=lambda c: c.similarity, reverse=True)
    return selected[:top_k]


class RagChunk(BaseModel):
    """A chunk surfaced by similarity search.

    `similarity` is in [0, 1]; 1.0 is identical. The `source_url` round-trips
    through to citations so the orchestration layer can validate every claim.
    S17-WEDGE-CITE-03: relaxed from HttpUrl to scheme-validated str so
    bazaar:// / twitsh:// URIs from synthetic provider sources retrieve cleanly.
    """

    source_id: UUID
    source_url: str
    chunk_index: int
    text: str
    similarity: float = Field(ge=0.0, le=1.0)

    @field_validator("source_url")
    @classmethod
    def _check_source_url(cls, v: str) -> str:
        return _validate_citation_uri(v)

    # S17-WEDGE-DATA-01 — provider attribution surfaced through match_chunks.
    # Defaults to 'web' so rows from databases that haven't yet run the
    # provider_kind migration still validate (matches the SQL DEFAULT).
    provider_kind: ProviderKind = "web"

    # S19-VOYAGE-RERANK-01 — populated only when the Voyage reranker is on
    # (`GECKO_RERANKER=voyage`) AND the call succeeded. Stays None on the
    # legacy path so eval consumers can detect "was this slate reranked?"
    # by `chunk.rerank_score is not None`. NB: Voyage's relevance score is
    # on a different scale than cosine `similarity`; downstream citation
    # grounding still reads `similarity`, this is a side-channel for eval.
    rerank_score: float | None = None

    # S26-CITE-03 — provider-level per-chunk metadata (e.g. tweet_url for
    # twitsh). Defaults to {} so Supabase/Mongo rows that predate the field
    # still validate. Consumers should prefer named keys over raw metadata
    # access, but this field keeps the plumbing generic.
    metadata: dict[str, Any] = Field(default_factory=dict)


async def rag_query(
    session_id: UUID,
    question: str,
    top_k: int = 8,
    store: SessionStore | None = None,
) -> list[RagChunk]:
    """Embed `question` and return the top-k most similar chunks for the session."""
    if not question.strip():
        return []
    if top_k <= 0:
        return []

    store = store or SessionStore.from_env()

    if get_chunk_store() == "mongo":
        vectors, tokens = await embed([question])
        cost_model = get_ingestion_settings().embed_model
    else:
        vectors, tokens = await embed_for_postgres_vector([question])
        cost_model = POSTGRES_EMBED_MODEL
    if not vectors:
        return []
    query_embedding = vectors[0]
    # Account for the question-embedding cost too — small but real, and avoids
    # apparent margin drift between research and follow-up queries.
    if tokens > 0:
        cost = estimate_embed_cost_usd(cost_model, tokens)
        await store.add_cost(session_id, "embed", cost)

    # S17-WEDGE-CITE-03 — over-fetch by 2x so the per-provider boost can pull
    # structured-provider chunks (Bazaar/Arxiv/twit.sh) into the final top_k
    # even when their raw cosine sim is below the Tavily cutoff. Without
    # over-fetch the boost is purely cosmetic — re-ordering within a set
    # that's already biased toward long-form web prose. Bounded at 2x so the
    # extra wire cost is small (12→24 rows max for the pro tier default).
    fetch_k = top_k * 2

    # S17-WEDGE-CITE-04 — hybrid retrieval. Per-provider quota UNION global
    # top-N at the SQL layer guarantees Bazaar/twit.sh/Arxiv chunks reach the
    # LLM even when 99 web chunks vs 1-3 provider chunks would otherwise
    # starve the structured providers out of the top-k. The boost rerank
    # below still applies — it now operates on a slate that *contains* the
    # provider chunks, instead of re-ordering an already-biased web-only set.
    # Toggle via GECKO_RETRIEVAL_HYBRID (default true). Falls back to the
    # legacy match_chunks RPC when disabled or when the hybrid RPC errors
    # (e.g. migration not yet applied on a remote DB).
    use_hybrid = os.environ.get("GECKO_RETRIEVAL_HYBRID", "true").lower() not in (
        "0",
        "false",
        "no",
    )
    per_kind_quota = _resolve_per_kind_quota()

    # S18-MONGO-READ-01 — dispatch on chunk store. Mongo path uses
    # $vectorSearch + app-side per-kind quota (mirrors the Postgres
    # match_chunks_hybrid SQL function 1:1). Supabase path stays on the
    # RPC seam.
    if get_chunk_store() == "mongo":
        from gecko_core.db.mongo_reads import (
            match_chunks_hybrid_mongo,
            match_chunks_mongo,
        )

        if use_hybrid:
            try:
                rows = await match_chunks_hybrid_mongo(
                    session_id=session_id,
                    query_embedding=query_embedding,
                    match_count=fetch_k,
                    per_kind_quota=per_kind_quota,
                )
            except Exception as exc:
                logger.warning(
                    "rag.mongo.hybrid.fallback err=%s",
                    exc,
                )
                rows = await match_chunks_mongo(
                    session_id=session_id,
                    query_embedding=query_embedding,
                    match_count=fetch_k,
                )
        else:
            rows = await match_chunks_mongo(
                session_id=session_id,
                query_embedding=query_embedding,
                match_count=fetch_k,
            )
    else:

        def _rpc() -> list[dict[str, Any]]:
            # Underscore name to reach the inner Client; we keep the seam thin to
            # avoid leaking supabase types into gecko-core's public surface.
            client = store._client
            if use_hybrid:
                try:
                    res = client.rpc(
                        "match_chunks_hybrid",
                        {
                            "p_session_id": str(session_id),
                            "query_embedding": query_embedding,
                            "match_count": fetch_k,
                            "per_kind_quota": per_kind_quota,
                        },
                    ).execute()
                    return cast(list[dict[str, Any]], res.data or [])
                except Exception as exc:
                    logger.warning(
                        "rag.hybrid.fallback err=%s (falling back to match_chunks)",
                        exc,
                    )
            res = client.rpc(
                "match_chunks",
                {
                    "p_session_id": str(session_id),
                    "query_embedding": query_embedding,
                    "match_count": fetch_k,
                },
            ).execute()
            return cast(list[dict[str, Any]], res.data or [])

        rows = await asyncio.to_thread(_rpc)
    chunks = [RagChunk.model_validate(row) for row in rows]
    # Composition order (S19-VOYAGE-RERANK-01):
    #   1. provider boost — per-kind multiplicative weights on cosine sim
    #   2. quota rescue   — guarantee non-web wedge providers survive truncate
    #   3. Voyage rerank  — semantic re-score on the shape-balanced slate
    # The order is load-bearing: structural rescue must happen before any
    # semantic re-score, otherwise wedge providers get evicted before
    # Voyage gets to evaluate them.
    #
    # Slate width: when the reranker is OFF we truncate to `top_k` here.
    # When the reranker is ON we keep the slate up to RERANK_TOP_K_INPUT
    # so Voyage gets the full candidate window the plan specifies (K=20),
    # then Voyage trims to `top_k`.
    from gecko_core.rag.voyage_rerank import (
        RERANK_TOP_K_INPUT,
        _flag_enabled,
        voyage_rerank,
    )

    reserve = per_kind_quota if use_hybrid else 0
    pre_voyage_k = RERANK_TOP_K_INPUT if _flag_enabled() else top_k
    # S20-SELF-CITATION-GUARD-01 — detect Gecko-self-referential ideas at the
    # entry point and pass the flag through so the per-provider rerank can
    # down-weight Gecko-domain chunks. See gecko_core.rag.self_citation.
    self_citation_active = is_self_referential_idea(question)
    if self_citation_active:
        logger.info(
            "rag.self_citation.active question_excerpt=%s",
            question[:60],
        )
    boosted = _rerank_by_provider(
        chunks,
        pre_voyage_k,
        reserve_quota=reserve,
        self_citation_active=self_citation_active,
    )

    return await voyage_rerank(question, boosted, top_n=top_k)


__all__ = ["RagChunk", "_rerank_by_provider", "rag_query"]
