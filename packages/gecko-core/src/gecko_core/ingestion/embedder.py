"""Embeddings dispatch: OpenAI and Voyage AI paths.

Default provider is Voyage AI `voyage-context-3` (1024-dim vectors) — the
RAG-optimal, context-aware embedding model that conditions each chunk vector
on its full source document, controlled by `EMBED_PROVIDER=voyage` /
`EMBED_MODEL=voyage-context-3` in settings.  Legacy `voyage-3` remains a
valid override (same 1024 dim, no Atlas index changes needed).  The
OpenAI `text-embedding-3-small` path (1536-dim) is selected when
`EMBED_PROVIDER=openai` or when an explicit `client=` is passed.

Supabase pgvector columns (`gecko_precedent`, `memory`, chunk RPCs) are
migrated as ``vector(1536)`` for ``text-embedding-3-small``. Call
:func:`embed_for_postgres_vector` from those code paths so they stay
1536-dimensional even when the default ingest embedder is Voyage (1024).

--- OpenAI path ---
Concurrency + retry (V11-01): a *module-level* semaphore caps in-flight
embedding requests at 8 across the whole process — protects us from
`text-embedding-3-small` TPM (1M tokens/min) when an idea fans out into
many sources.

S16-INGEST-03 (FM-3 fix from docs/diagnostics/2026-05-01-chunk-write-failures.md):

  - Retry on `openai.RateLimitError` uses **full-jitter exponential
    backoff** instead of fixed (1, 2, 4, 8) seconds. Synchronized
    backoffs across `MAX_CONCURRENT_SOURCES` sources caused a thundering
    herd — every retry hit OpenAI at the same wall-clock instants.
    Jitter spreads them out.
  - Cap retries at 5 (was 4).
  - **Batch shedding**: when a single `embed()` call sees ≥ 2 rate-limit
    responses, the per-call concurrency it can hold is halved (8 → 4)
    for the remainder of the call. The semaphore itself is not resized
    (that would affect other in-flight callers); we approximate by
    locally tracking how many slots we hold concurrently. Reset to full
    on the next `embed()` invocation — that's "next source" granularity,
    which matches the ticket's intent.

All other openai errors propagate immediately so genuine bugs aren't masked.

--- Voyage AI path ---
Simple manual retry loop (3 attempts, full-jitter exponential backoff) on
any exception with status_code/status 429 or on unknown errors. No semaphore
— Voyage AI's published rate limits are generous for our batch sizes.
`_ShedState` is OpenAI-only.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Iterable

import openai
from openai import AsyncOpenAI

from .settings import get_ingestion_settings

logger = logging.getLogger(__name__)

EMBED_BATCH_SIZE = 100

# Module-level singleton — sharing across the process is the whole point.
# A per-request semaphore would defeat the rate-limit protection because
# every request would get its own pool of 8.
_EMBED_CONCURRENCY = 8
_embed_semaphore = asyncio.Semaphore(_EMBED_CONCURRENCY)

# S16-INGEST-03 — retry policy. Full-jitter exponential:
#   sleep_n = uniform(0, min(_RETRY_CAP_S, _RETRY_BASE_S * 2^n))
# Capped at 5 attempts. The cap matches AWS's published "full jitter"
# recipe; spread is wide enough to break the herd, narrow enough that
# the wall-clock budget stays bounded (worst case ~30s).
_RETRY_BASE_S = 1.0
_RETRY_CAP_S = 16.0
_MAX_ATTEMPTS = 5

# Batch-shedding threshold: after this many rate-limit responses inside
# a single `embed()` call, halve the local concurrency cap. 2 is the
# ticket spec.
_RATE_LIMIT_SHED_THRESHOLD = 2

# Published list prices per 1M tokens (refreshed 2026-05-15 against
# https://docs.voyageai.com/docs/embeddings.md). Surfaced on the per-session
# economics view so dashboards reflect real spend.
#
# Voyage model catalog (ctx / default dim):
#   voyage-4-large    32K / 1024  — best general + multilingual retrieval
#   voyage-4          32K / 1024  — general + multilingual retrieval
#   voyage-4-lite     32K / 1024  — latency/cost-optimized
#   voyage-code-3     32K / 1024  — code retrieval
#   voyage-finance-2  32K / 1024  — finance retrieval + RAG (relevant to trade-panel)
#   voyage-law-2      16K / 1024  — legal retrieval + RAG
#   voyage-code-2     16K / 1536  — legacy code embeddings
#   voyage-3-large    32K / 1024  — previous-gen RAG default (S20-RAG-04)
#   voyage-3          32K / 1024  — previous-gen general
#   voyage-context-3  32K / 1024  — MongoDB-hosted only (ai.mongodb.com/v1/embeddings)
#
# Series-4 embeddings are cross-compatible (1024/256/512/2048 truncatable).
# Prices below are best-effort; verify against
# https://docs.voyageai.com/docs/pricing before billing changes.
_EMBED_RATES_USD_PER_1M: dict[str, float] = {
    "text-embedding-3-small": 0.02,
    "text-embedding-3-large": 0.13,
    "text-embedding-ada-002": 0.10,
    # Voyage v4 series (current)
    "voyage-4-large": 0.18,
    "voyage-4": 0.06,
    "voyage-4-lite": 0.02,
    # Domain-tuned
    "voyage-code-3": 0.18,
    "voyage-finance-2": 0.12,
    "voyage-law-2": 0.12,
    "voyage-code-2": 0.12,
    # Voyage v3 series (previous-gen, kept for backfill compatibility)
    "voyage-3": 0.06,
    "voyage-3-large": 0.18,
    "voyage-context-3": 0.18,  # MongoDB-hosted only
}


def _chunked(seq: list[str], size: int) -> Iterable[list[str]]:
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def estimate_embed_cost_usd(model: str, total_tokens: int) -> float:
    rate = _EMBED_RATES_USD_PER_1M.get(model, 0.0)
    return total_tokens * rate / 1_000_000


def _rate_limit_code(exc: openai.RateLimitError) -> str:
    """Best-effort extraction of the OpenAI error code (e.g. 'tokens')
    so CloudWatch shows whether we tripped TPM vs RPM."""
    code = getattr(exc, "code", None)
    if code:
        return str(code)
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        err = body.get("error") or {}
        if isinstance(err, dict) and err.get("code"):
            return str(err["code"])
    return "unknown"


def _full_jitter_backoff(attempt: int) -> float:
    """AWS-style full-jitter exponential.

    `attempt` is 1-indexed (1st retry = attempt 1). Returns a sleep
    in seconds drawn uniformly from [0, min(_RETRY_CAP_S, base * 2^(n-1))].

    Module-private + deterministic-shape so the unit test can assert
    the distribution. We use the global `random` module so tests can
    seed it for reproducibility.
    """
    ceiling = min(_RETRY_CAP_S, _RETRY_BASE_S * (2 ** (attempt - 1)))
    return random.uniform(0.0, ceiling)


class _ShedState:
    """Per-`embed()`-call rate-limit accounting. Reset on each call.

    We don't resize the module-level semaphore because that would
    starve other in-flight `embed()` callers. Instead we track the
    *effective* concurrency we'll allow ourselves to hold — when shed
    is active, we acquire-then-immediately-release one slot before
    each create call so other callers can interleave. The net effect:
    half the throughput, no thundering herd.
    """

    def __init__(self) -> None:
        self.rate_limit_hits = 0
        self.shed_active = False

    def record_rate_limit(self) -> None:
        self.rate_limit_hits += 1
        if self.rate_limit_hits >= _RATE_LIMIT_SHED_THRESHOLD and not self.shed_active:
            self.shed_active = True
            logger.warning(
                "embed.batch_shedding_activated",
                extra={
                    "rate_limit_hits": self.rate_limit_hits,
                    "effective_concurrency": _EMBED_CONCURRENCY // 2,
                    "module_concurrency": _EMBED_CONCURRENCY,
                },
            )


async def _create_with_retry(
    api: AsyncOpenAI,
    model: str,
    batch: list[str],
    shed: _ShedState,
) -> tuple[list[list[float]], int]:
    """Single embeddings.create with concurrency cap + RateLimitError retry."""
    last_exc: openai.RateLimitError | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        async with _embed_semaphore:
            # S16-INGEST-03 — local batch shedding. When >=2 RateLimit
            # responses have been seen this call, hold a *second* slot
            # concurrently so we effectively halve our own throughput
            # without resizing the global semaphore (which would affect
            # unrelated callers). The shed slot is acquired with a tiny
            # timeout and released immediately if not available — best-
            # effort, never blocks indefinitely.
            shed_slot_held = False
            if shed.shed_active:
                try:
                    # `acquire()` is unbounded; use wait_for to keep this
                    # bounded so we don't deadlock if every other slot
                    # is also shedding.
                    await asyncio.wait_for(_embed_semaphore.acquire(), timeout=0.1)
                    shed_slot_held = True
                except TimeoutError:
                    shed_slot_held = False
            try:
                try:
                    resp = await api.embeddings.create(model=model, input=batch)
                except openai.RateLimitError as exc:
                    last_exc = exc
                    shed.record_rate_limit()
                    if attempt >= _MAX_ATTEMPTS:
                        break
                    backoff = _full_jitter_backoff(attempt)
                    # S16-INGEST-01 — structured kwargs so log scrapers can
                    # bucket the retry path by `error_kind`. The pipeline's
                    # audit row will record the *terminal* outcome; this log
                    # gives per-attempt visibility into the retry cliff (FM-3).
                    logger.info(
                        "embed retry %d/%d after RateLimitError %s (sleep %.2fs jittered)",
                        attempt,
                        _MAX_ATTEMPTS,
                        _rate_limit_code(exc),
                        backoff,
                        extra={
                            "batch_size": len(batch),
                            "error_kind": "embedding_null",
                            "attempt": attempt,
                            "max_attempts": _MAX_ATTEMPTS,
                            "rate_limit_code": _rate_limit_code(exc),
                            "embed_model": model,
                            "shed_active": shed.shed_active,
                            "backoff_seconds": backoff,
                        },
                    )
                else:
                    tokens = resp.usage.total_tokens if resp.usage is not None else 0
                    return [d.embedding for d in resp.data], tokens
            finally:
                if shed_slot_held:
                    _embed_semaphore.release()
        # Sleep outside the semaphore so we don't hold a slot while backing off.
        await asyncio.sleep(_full_jitter_backoff(attempt))
    assert last_exc is not None  # invariant: only path here is exhausted retries
    raise last_exc


async def _embed_voyage(
    texts: list[str],
    *,
    api_key: str,
    model: str = "voyage-3-large",
    input_type: str | None = None,
    batch_size: int = EMBED_BATCH_SIZE,
) -> tuple[list[list[float]], int]:
    """Voyage AI embed path. Batches at batch_size, manual retry on 429.

    Uses a lazy import so tests can inject a fake module via sys.modules
    without importing the real voyageai package.
    """
    import voyageai  # lazy — allows sys.modules injection in tests

    client = voyageai.AsyncClient(api_key=api_key)  # type: ignore[attr-defined]
    results: list[list[float]] = []
    total_tokens = 0

    for batch in _chunked(texts, batch_size):
        for attempt in range(1, 4):
            try:
                resp = await client.embed(texts=batch, model=model, input_type=input_type)
                results.extend(list(resp.embeddings))  # type: ignore[arg-type]
                total_tokens += resp.total_tokens
                break
            except Exception as exc:
                status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
                if attempt < 3 and status in (429, None):
                    backoff = _full_jitter_backoff(attempt)
                    logger.info(
                        "voyage embed retry %d/3 (sleep %.2fs)",
                        attempt,
                        backoff,
                        extra={"attempt": attempt, "error": str(exc)},
                    )
                    await asyncio.sleep(backoff)
                else:
                    raise

    await asyncio.sleep(0)  # yield to event loop, mirrors OpenAI path
    return results, total_tokens


# Dimension assumed by ``infra/supabase/migrations`` for precedent / memory /
# ``match_chunks*`` RPC parameters (text-embedding-3-small).
POSTGRES_VECTOR_DIM: int = 1536
POSTGRES_EMBED_MODEL: str = "text-embedding-3-small"


async def embed_for_postgres_vector(
    texts: list[str],
    *,
    batch_size: int = EMBED_BATCH_SIZE,
) -> tuple[list[list[float]], int]:
    """Embed for Supabase ``vector(1536)`` ANN (precedent, memory, chunk RPCs).

    Always uses OpenAI ``text-embedding-3-small`` and ``OPENAI_API_KEY``,
    regardless of ``EMBED_PROVIDER``, so Postgres rows and query vectors stay
    in the same space while ingestion / Mongo paths may use Voyage 1024 via
    :func:`embed`.
    """
    if not texts:
        return [], 0
    settings = get_ingestion_settings()
    if settings.openai_api_key is None:
        raise ValueError(
            "OPENAI_API_KEY must be set for embed_for_postgres_vector "
            f"(Supabase ANN expects {POSTGRES_VECTOR_DIM} dims / {POSTGRES_EMBED_MODEL})"
        )
    api = AsyncOpenAI(api_key=settings.openai_api_key.get_secret_value())
    batches = list(_chunked(texts, batch_size))
    shed = _ShedState()
    results: list[list[float]] = []
    total_tokens = 0
    for batch in batches:
        vectors, tokens = await _create_with_retry(api, POSTGRES_EMBED_MODEL, batch, shed)
        results.extend(vectors)
        total_tokens += tokens
    await asyncio.sleep(0)
    return results, total_tokens


async def embed(
    texts: list[str],
    *,
    client: AsyncOpenAI | None = None,
    model: str | None = None,
    batch_size: int = EMBED_BATCH_SIZE,
    input_type: str | None = None,
) -> tuple[list[list[float]], int]:
    """Embed `texts`, batching at `batch_size`. Order-preserving.

    Returns (vectors, total_tokens). The token count is the sum across all
    batches and lets callers attribute cost to a session.

    Provider dispatch:
    - When an explicit `client` is supplied, the OpenAI path is used
      unconditionally (backwards-compat for tests and bring-your-own-client
      callers).
    - Otherwise, `EMBED_PROVIDER` from settings controls dispatch.
      `voyage` → `_embed_voyage()`; `openai` → OpenAI embeddings.create path.

    `input_type` (S33-#79): Voyage asymmetric retrieval hint. Pass
    `"query"` for retrieval-side query embeds and `"document"` for
    ingest-side chunk embeds — `voyage-3-large` prepends instruction
    prefixes tuned per side, a documented relevance gain over symmetric
    (`None`/`None`) embedding. OpenAI ignores it (no asymmetric API).
    CRITICAL: query/document embeds must be *consistent with the stored
    corpus*. Corpus chunks pre-S33 were embedded `input_type=None`; a
    `"query"` query embed against an `input_type=None` corpus is mixed
    until the corpus is re-embedded as `"document"`.
    """
    if not texts:
        return [], 0

    settings = None
    provider = "openai"  # default — overridden below if settings resolved

    if client is None or model is None:
        settings = get_ingestion_settings()
        provider = settings.embed_provider.lower()
        model_name = model or settings.embed_model
    else:
        # Explicit client always means OpenAI path (backwards compat).
        model_name = model

    if provider == "voyage":
        assert settings is not None  # only set when client/model are None
        key = settings.voyage_api_key
        if key is None:
            raise ValueError("VOYAGE_API_KEY must be set when EMBED_PROVIDER=voyage")
        return await _embed_voyage(
            texts,
            api_key=key.get_secret_value(),
            model=model_name,
            input_type=input_type,
            batch_size=batch_size,
        )

    # OpenAI path — requires openai_api_key when settings are resolved
    if settings is not None:
        if settings.openai_api_key is None:
            raise ValueError("OPENAI_API_KEY must be set when EMBED_PROVIDER=openai")
        api = client or AsyncOpenAI(api_key=settings.openai_api_key.get_secret_value())
    else:
        # Explicit-client path: api_key already baked into the provided client.
        assert client is not None  # invariant: client is None only when settings path taken
        api = client

    batches = list(_chunked(texts, batch_size))
    # Sequential at the call-site — protects rate limits and keeps cost
    # predictable. Concurrency control still applies via the module
    # semaphore for callers that fan out across multiple `embed()` calls.
    # S16-INGEST-03 — shed state is per-`embed()` invocation. The ticket
    # says "reset to full concurrency on next source" — at the pipeline
    # layer one source corresponds to one (or a small number of)
    # `embed()` calls, so per-call reset matches.
    shed = _ShedState()
    results: list[list[float]] = []
    total_tokens = 0
    for batch in batches:
        vectors, tokens = await _create_with_retry(api, model_name, batch, shed)
        results.extend(vectors)
        total_tokens += tokens
    await asyncio.sleep(0)
    return results, total_tokens


__all__ = [
    "EMBED_BATCH_SIZE",
    "POSTGRES_EMBED_MODEL",
    "POSTGRES_VECTOR_DIM",
    "embed",
    "embed_for_postgres_vector",
    "estimate_embed_cost_usd",
]
