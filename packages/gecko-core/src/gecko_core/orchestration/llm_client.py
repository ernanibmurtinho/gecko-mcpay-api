"""Centralized OpenAI-compatible client factory + streaming helper.

Single resolution path for every LLM call in `gecko_core`. Replaces the
ad-hoc ``AsyncOpenAI(api_key=…, base_url=…)`` constructors scattered
across ``basic.py``, ``post_processors.py``, ``refine.py``,
``judges/synth.py``, ``workflows.ask``, ``advisor/agents.py``, and
``routing/_call_model``.

Why this exists
---------------
1. **Explicit timeouts.** OpenAI's SDK default httpx timeout is 600s
   (10 minutes), but Cloudflare / ALB / NLB will close idle connections
   in ~60-100s. Without an explicit ``timeout=`` the user sees a generic
   ``APITimeoutError`` and we have no per-phase visibility.

2. **Streaming with finish_reason.** OpenRouter only sends keep-alive
   comments (``: OPENROUTER PROCESSING``) on SSE streams. Non-streaming
   requests on slow models (Kimi K2.6, DeepSeek V3.2, GPT-5 Pro) get
   killed at the proxy edge before the response body lands. Streaming
   keeps the connection alive and lets us inspect ``finish_reason`` on
   the final chunk before parsing JSON — catching mid-stream truncation
   that would otherwise surface as a confusing pydantic ``ValidationError``.

3. **Stall detection.** A connection that stays open but stops emitting
   chunks is worse than a clean timeout — the SDK won't raise. We track
   ``last_chunk_at`` per stream and abort with ``LLMStalledError`` when
   the gap exceeds ``OPENROUTER_STALL_S``.

Env knobs (all optional)
------------------------
- ``OPENROUTER_TIMEOUT_S`` — total read timeout per request (default 180).
- ``OPENROUTER_CONNECT_TIMEOUT_S`` — connect timeout (default 10).
- ``OPENROUTER_MAX_RETRIES`` — SDK-level retries on transient errors
  (default 2). Streaming responses are not retried by the SDK.
- ``OPENROUTER_STALL_S`` — max gap between streamed chunks before we
  abort (default 60).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionChunk

from gecko_core.orchestration.settings import LLMClientConfig

logger = logging.getLogger(__name__)


__all__ = [
    "LLMStalledError",
    "LLMTruncationError",
    "StreamedCompletion",
    "build_async_client",
    "stream_chat_completion",
]


class LLMTruncationError(Exception):
    """Raised when the upstream finishes with ``finish_reason="length"``.

    The message includes ``model``, ``provider``, ``gen_id``,
    ``completion_tokens`` so the caller can surface a self-contained
    error without trip to log aggregation.
    """


class LLMStalledError(Exception):
    """Raised when a streaming completion stops emitting chunks for
    longer than ``OPENROUTER_STALL_S``.

    Distinct from ``APITimeoutError`` (connect/read timeout): the
    connection is still open, the upstream just went silent.
    """


@dataclass(slots=True, frozen=True)
class StreamedCompletion:
    """Aggregated result of a streamed chat completion.

    ``content`` is the concatenation of every ``delta.content`` across
    chunks. ``finish_reason``, ``usage``, ``model``, ``provider``,
    ``gen_id`` come from the final chunk (OpenRouter populates ``usage``
    when ``stream_options={"include_usage": True}`` is set, which the
    helper does by default).

    ``usage_cost_usd`` is OpenRouter's ``usage.cost`` (real billed amount,
    not an estimate) when present. ``upstream_cost_usd`` is
    ``usage.cost_details.upstream_inference_cost`` when surfaced. Both
    are ``None`` for non-OpenRouter providers — caller falls back to a
    token-based estimate.
    """

    content: str
    finish_reason: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    model: str
    provider: str
    gen_id: str
    chunks: int
    usage_cost_usd: float | None
    upstream_cost_usd: float | None


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("invalid %s=%r, using default %s", name, raw, default)
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("invalid %s=%r, using default %s", name, raw, default)
        return default


def build_async_client(cfg: LLMClientConfig) -> AsyncOpenAI:
    """Construct an ``AsyncOpenAI`` client with explicit timeouts.

    Replaces every direct ``AsyncOpenAI(api_key=…, base_url=…)`` call.
    Reads ``OPENROUTER_TIMEOUT_S`` / ``OPENROUTER_CONNECT_TIMEOUT_S`` /
    ``OPENROUTER_MAX_RETRIES`` from the env at call time so operators
    can tune without redeploying.
    """
    read_timeout = _env_float("OPENROUTER_TIMEOUT_S", 180.0)
    connect_timeout = _env_float("OPENROUTER_CONNECT_TIMEOUT_S", 10.0)
    max_retries = _env_int("OPENROUTER_MAX_RETRIES", 2)

    timeout = httpx.Timeout(
        connect=connect_timeout,
        read=read_timeout,
        write=30.0,
        pool=10.0,
    )

    client_kwargs: dict[str, Any] = {
        "api_key": cfg.api_key,
        "base_url": cfg.base_url,
        "timeout": timeout,
        "max_retries": max_retries,
    }
    if cfg.extra_headers:
        client_kwargs["default_headers"] = dict(cfg.extra_headers)
    return AsyncOpenAI(**client_kwargs)


def _provider_from_chunk(
    chunk: ChatCompletionChunk,
    fallback: str,
) -> str:
    """Best-effort extract of the upstream sub-provider from an OpenRouter chunk.

    OpenRouter exposes the resolved provider on the response object as
    ``provider`` (e.g. ``"openai"``, ``"deepinfra"``, ``"atlascloud"``).
    Some SDK versions surface it on the parent response, others on the
    raw model_extra; check both.
    """
    explicit = getattr(chunk, "provider", None)
    if isinstance(explicit, str) and explicit:
        return explicit
    extra = getattr(chunk, "model_extra", None)
    if isinstance(extra, dict):
        candidate = extra.get("provider")
        if isinstance(candidate, str) and candidate:
            return candidate
    return fallback


async def _iter_with_stall_guard(
    stream: AsyncIterator[ChatCompletionChunk],
    *,
    stall_seconds: float,
    gen_id_holder: list[str],
) -> AsyncIterator[ChatCompletionChunk]:
    """Wrap a streaming iterator with a per-chunk wall-clock guard.

    Raises ``LLMStalledError`` when the gap between consecutive chunks
    exceeds ``stall_seconds``. ``gen_id_holder`` is a 1-element list
    used to surface the partial generation id in the error (so callers
    can debug against OpenRouter's activity log).
    """
    while True:
        try:
            chunk = await asyncio.wait_for(stream.__anext__(), timeout=stall_seconds)
        except StopAsyncIteration:
            return
        except TimeoutError as exc:
            gid = gen_id_holder[0] if gen_id_holder else "unknown"
            raise LLMStalledError(
                f"LLM stream stalled (no chunks for {stall_seconds:.0f}s, gen_id={gid})"
            ) from exc
        yield chunk


async def stream_chat_completion(
    client: AsyncOpenAI,
    *,
    model: str,
    messages: list[dict[str, Any]],
    temperature: float = 0.3,
    max_tokens: int | None = None,
    response_format: dict[str, Any] | None = None,
    seed: int | None = 42,
    extra_body: dict[str, Any] | None = None,
    stall_seconds: float | None = None,
) -> StreamedCompletion:
    """Stream a chat completion, aggregate chunks, validate finish_reason.

    Streaming is the default path for every long generation: it keeps
    the connection alive (OpenRouter sends ``: OPENROUTER PROCESSING``
    keep-alives), surfaces ``finish_reason`` on the final chunk, and
    lets us watch for stalls.

    Raises ``LLMTruncationError`` when ``finish_reason="length"`` —
    callers should not attempt to parse the partial buffer in that case.

    ``response_format`` (``{"type": "json_object"}`` or strict
    ``json_schema``) is fully compatible with ``stream=True`` on
    OpenRouter — every chunk carries a fragment of the JSON document.
    """
    stall = stall_seconds if stall_seconds is not None else _env_float("OPENROUTER_STALL_S", 60.0)

    create_kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if max_tokens is not None:
        create_kwargs["max_tokens"] = max_tokens
    if response_format is not None:
        create_kwargs["response_format"] = response_format
    if seed is not None:
        create_kwargs["seed"] = seed
    if extra_body is not None:
        create_kwargs["extra_body"] = extra_body

    started_at = time.monotonic()
    buffer: list[str] = []
    finish_reason: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    usage_cost_usd: float | None = None
    upstream_cost_usd: float | None = None
    resolved_model = model
    provider = "unknown"
    gen_id = ""
    chunk_count = 0

    stream = await client.chat.completions.create(**create_kwargs)
    gen_id_holder: list[str] = [""]
    try:
        async for chunk in _iter_with_stall_guard(
            stream, stall_seconds=stall, gen_id_holder=gen_id_holder
        ):
            chunk_count += 1
            if chunk.id and not gen_id:
                gen_id = chunk.id
                gen_id_holder[0] = gen_id
            if chunk.model:
                resolved_model = chunk.model
            provider = _provider_from_chunk(chunk, provider)

            if chunk.usage is not None:
                prompt_tokens = chunk.usage.prompt_tokens
                completion_tokens = chunk.usage.completion_tokens
                # OpenRouter populates `usage.cost` (real billed amount)
                # and optionally `usage.cost_details.upstream_inference_cost`
                # when stream_options.include_usage=True. These fields
                # aren't on the typed Pydantic CompletionUsage, so reach
                # in via getattr / model_extra to stay forward-compatible.
                cost_val = getattr(chunk.usage, "cost", None)
                if cost_val is None:
                    extras = getattr(chunk.usage, "model_extra", None)
                    if isinstance(extras, dict):
                        cost_val = extras.get("cost")
                if isinstance(cost_val, int | float):
                    usage_cost_usd = float(cost_val)
                cost_details = getattr(chunk.usage, "cost_details", None)
                if cost_details is None:
                    extras = getattr(chunk.usage, "model_extra", None)
                    if isinstance(extras, dict):
                        cost_details = extras.get("cost_details")
                if isinstance(cost_details, dict):
                    upstream = cost_details.get("upstream_inference_cost")
                    if isinstance(upstream, int | float):
                        upstream_cost_usd = float(upstream)

            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            if choice.delta and choice.delta.content:
                buffer.append(choice.delta.content)
            if choice.finish_reason:
                finish_reason = choice.finish_reason
    finally:
        close = getattr(stream, "close", None)
        if callable(close):
            with contextlib.suppress(Exception):
                await close()

    content = "".join(buffer)
    elapsed_ms = int((time.monotonic() - started_at) * 1000)
    logger.info(
        "llm.stream model=%s provider=%s finish=%s chunks=%d "
        "prompt_tokens=%s completion_tokens=%s gen_id=%s content_len=%d "
        "elapsed_ms=%d",
        resolved_model,
        provider,
        finish_reason,
        chunk_count,
        prompt_tokens,
        completion_tokens,
        gen_id,
        len(content),
        elapsed_ms,
    )

    if finish_reason == "length":
        raise LLMTruncationError(
            f"LLM output truncated [finish_reason=length, "
            f"completion_tokens={completion_tokens}, model={resolved_model}, "
            f"provider={provider}, gen_id={gen_id}]"
        )

    return StreamedCompletion(
        content=content,
        finish_reason=finish_reason,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        model=resolved_model,
        provider=provider,
        gen_id=gen_id,
        chunks=chunk_count,
        usage_cost_usd=usage_cost_usd,
        upstream_cost_usd=upstream_cost_usd,
    )
