"""OpenAI embeddings, batched.

`text-embedding-3-small` → 1536-dim vectors. Batches at 100 inputs per call,
which is well under the OpenAI 2048-input limit and keeps individual call
latency bounded.

Concurrency + retry (V11-01): a *module-level* semaphore caps in-flight
embedding requests at 8 across the whole process — protects us from
`text-embedding-3-small` TPM (1M tokens/min) when an idea fans out into
many sources. Per-call retries on `openai.RateLimitError` use exponential
backoff (1s → 2s → 4s → 8s, max 4 attempts). All other openai errors
propagate immediately so genuine bugs aren't masked.
"""

from __future__ import annotations

import asyncio
import logging
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

_RETRY_BACKOFFS_S: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0)
_MAX_ATTEMPTS = 4

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


async def _create_with_retry(
    api: AsyncOpenAI, model: str, batch: list[str]
) -> tuple[list[list[float]], int]:
    """Single embeddings.create with concurrency cap + RateLimitError retry."""
    last_exc: openai.RateLimitError | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        async with _embed_semaphore:
            try:
                resp = await api.embeddings.create(model=model, input=batch)
            except openai.RateLimitError as exc:
                last_exc = exc
                if attempt >= _MAX_ATTEMPTS:
                    break
                backoff = _RETRY_BACKOFFS_S[attempt - 1]
                logger.info(
                    "embed retry %d/%d after RateLimitError %s (sleep %.1fs)",
                    attempt,
                    _MAX_ATTEMPTS,
                    _rate_limit_code(exc),
                    backoff,
                )
            else:
                tokens = resp.usage.total_tokens if resp.usage is not None else 0
                return [d.embedding for d in resp.data], tokens
        # Sleep outside the semaphore so we don't hold a slot while backing off.
        await asyncio.sleep(_RETRY_BACKOFFS_S[attempt - 1])
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
    results: list[list[float]] = []
    total_tokens = 0
    for batch in batches:
        vectors, tokens = await _create_with_retry(api, model_name, batch)
        results.extend(vectors)
        total_tokens += tokens
    await asyncio.sleep(0)
    return results, total_tokens


__all__ = ["EMBED_BATCH_SIZE", "embed", "estimate_embed_cost_usd"]
