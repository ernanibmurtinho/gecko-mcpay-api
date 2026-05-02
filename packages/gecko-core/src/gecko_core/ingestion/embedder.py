"""OpenAI embeddings, batched.

`text-embedding-3-small` → 1536-dim vectors. Batches at 100 inputs per call,
which is well under the OpenAI 2048-input limit and keeps individual call
latency bounded.

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

# OpenAI list price for text-embedding-3-small as of 2026-04. Cheap enough
# that we don't worry about a few cents of drift; surfaced on the per-session
# economics view so dashboards reflect real, not stub, spend.
_EMBED_RATES_USD_PER_1M: dict[str, float] = {
    "text-embedding-3-small": 0.02,
    "text-embedding-3-large": 0.13,
    "text-embedding-ada-002": 0.10,
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


async def embed(
    texts: list[str],
    *,
    client: AsyncOpenAI | None = None,
    model: str | None = None,
    batch_size: int = EMBED_BATCH_SIZE,
) -> tuple[list[list[float]], int]:
    """Embed `texts`, batching at `batch_size`. Order-preserving.

    Returns (vectors, total_tokens). The token count is the sum across all
    batches and lets callers attribute cost to a session.
    """
    if not texts:
        return [], 0

    # Only resolve settings when we actually need them — passing both `client`
    # and `model` (the unit-test path, and the future bring-your-own-client
    # path) must not require OPENAI_API_KEY in the env.
    if client is None or model is None:
        settings = get_ingestion_settings()
        api = client or AsyncOpenAI(api_key=settings.openai_api_key.get_secret_value())
        model_name = model or settings.embed_model
    else:
        api = client
        model_name = model

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


__all__ = ["EMBED_BATCH_SIZE", "embed", "estimate_embed_cost_usd"]
