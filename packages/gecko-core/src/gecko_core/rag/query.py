"""RAG query layer — pgvector cosine similarity over a session's chunks.

Calls the `match_chunks` SQL function via Supabase RPC. Embeds the question
with the same model used at ingest time so the vectors live in the same
space. Returns chunks ordered by similarity desc.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, cast
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from gecko_core.ingestion.embedder import embed
from gecko_core.models import _validate_citation_uri
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


def _rerank_by_provider(chunks: list[RagChunk], top_k: int) -> list[RagChunk]:
    """Apply per-provider boosts to similarity, re-sort, return top_k.

    Pure function — leaves the input list alone. Mutates each chunk's
    `similarity` to the boosted value so downstream consumers (citation
    grounding floor, low-grounding gate) see the post-boost score. Per the
    design memo §3.2 we keep the existing `[N] <uri>` citation shape; the
    URI scheme already encodes provider, so no new field is needed for
    the renderer to surface provenance.
    """
    if not chunks:
        return chunks
    weights = _resolve_provider_weights()
    boosted: list[RagChunk] = []
    for c in chunks:
        w = weights.get(c.provider_kind, 1.0)
        if w == 1.0:
            boosted.append(c)
            continue
        new_sim = max(0.0, min(1.0, c.similarity * w))
        boosted.append(c.model_copy(update={"similarity": new_sim}))
    boosted.sort(key=lambda c: c.similarity, reverse=True)
    return boosted[:top_k]


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

    vectors, tokens = await embed([question])
    if not vectors:
        return []
    query_embedding = vectors[0]
    # Account for the question-embedding cost too — small but real, and avoids
    # apparent margin drift between research and follow-up queries.
    if tokens > 0:
        from gecko_core.ingestion.embedder import estimate_embed_cost_usd
        from gecko_core.ingestion.settings import get_ingestion_settings

        cost = estimate_embed_cost_usd(get_ingestion_settings().embed_model, tokens)
        await store.add_cost(session_id, "embed", cost)

    # S17-WEDGE-CITE-03 — over-fetch by 2x so the per-provider boost can pull
    # structured-provider chunks (Bazaar/Arxiv/twit.sh) into the final top_k
    # even when their raw cosine sim is below the Tavily cutoff. Without
    # over-fetch the boost is purely cosmetic — re-ordering within a set
    # that's already biased toward long-form web prose. Bounded at 2x so the
    # extra wire cost is small (12→24 rows max for the pro tier default).
    fetch_k = top_k * 2

    def _rpc() -> list[dict[str, Any]]:
        # Underscore name to reach the inner Client; we keep the seam thin to
        # avoid leaking supabase types into gecko-core's public surface.
        client = store._client
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
    # Apply per-provider boost + re-sort + truncate to caller's top_k.
    return _rerank_by_provider(chunks, top_k)


__all__ = ["RagChunk", "_rerank_by_provider", "rag_query"]
