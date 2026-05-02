"""S16-INGEST-03 — embedder retry + jitter + batch-shedding tests.

Drives `gecko_core.ingestion.embedder._create_with_retry` against a
fake AsyncOpenAI client whose `embeddings.create` raises a configurable
sequence of `RateLimitError`s before succeeding. Asserts:

  - Up to 5 retry attempts (was 4 pre-S16-INGEST-03).
  - Sleep intervals are NOT the fixed (1, 2, 4, 8) sequence — full
    jitter spreads them across [0, ceiling]. We verify the *ceiling*
    is exponential and the actual draws differ from the fixed schedule.
  - On >=2 RateLimit responses inside one `embed()` call, the
    `_ShedState` flips `shed_active=True` and the warning log fires.
  - Exhausted retries propagate the last `RateLimitError` (no silent
    success / no swallowed exception).

We intentionally do NOT real-sleep — the test patches
`asyncio.sleep` to record durations and return immediately. Otherwise
this file would dominate the test suite wall-clock budget.
"""

from __future__ import annotations

import asyncio
import random
from typing import Any
from unittest.mock import patch

import openai
import pytest
from gecko_core.ingestion import embedder

# ---------------------------------------------------------------------------
# Fake OpenAI client
# ---------------------------------------------------------------------------


class _FakeUsage:
    def __init__(self, total_tokens: int) -> None:
        self.total_tokens = total_tokens


class _FakeResp:
    def __init__(self, n: int, dim: int = 1536) -> None:
        self.data = [type("E", (), {"embedding": [0.0] * dim})() for _ in range(n)]
        self.usage = _FakeUsage(n * 7)


class _FakeRateLimit(openai.RateLimitError):
    """Bypass openai.RateLimitError's strict ctor (which requires a
    real httpx.Response). The embedder's classifier only inspects
    `code` / `body` / `__class__.__name__`, all of which we satisfy."""

    def __init__(self) -> None:
        # Skip super().__init__ — that path requires response.request.
        Exception.__init__(self, "rate limit exceeded for tokens")
        self.code = "rate_limit_exceeded"
        self.body = {"error": {"code": "rate_limit_exceeded"}}


class _FakeEmbeddings:
    def __init__(self, fail_first_n: int) -> None:
        self._fail_first_n = fail_first_n
        self.calls = 0

    async def create(self, *, model: str, input: list[str]) -> _FakeResp:
        self.calls += 1
        if self.calls <= self._fail_first_n:
            raise _FakeRateLimit()
        return _FakeResp(len(input))


class _FakeApi:
    def __init__(self, fail_first_n: int) -> None:
        self.embeddings = _FakeEmbeddings(fail_first_n)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_max_attempts_is_5() -> None:
    """5 attempts total: 1 initial + 4 retries. The 6th-attempt-fail
    case must NOT happen — exhausted retries surface the last exc."""
    assert embedder._MAX_ATTEMPTS == 5
    api = _FakeApi(fail_first_n=4)  # 4 fails + 5th succeeds
    sleeps: list[float] = []

    async def _record_sleep(s: float) -> None:
        sleeps.append(s)

    with patch.object(embedder.asyncio, "sleep", new=_record_sleep):
        out, tokens = await embedder.embed(
            ["hello", "world"], client=api, model="text-embedding-3-small"
        )
    assert len(out) == 2
    assert tokens == 2 * 7
    # 4 backoffs slept between the 4 failed attempts (+ 1 trailing
    # asyncio.sleep(0) at the end of embed() for cooperative yield).
    backoffs = [s for s in sleeps if s > 0]
    assert len(backoffs) == 4


@pytest.mark.asyncio
async def test_jitter_breaks_thundering_herd() -> None:
    """Two parallel embed() calls draw their backoffs INDEPENDENTLY
    (fresh random samples each retry), so they don't synchronize on
    the 1, 2, 4, 8s fixed schedule. We verify by collecting two runs'
    sleep traces and asserting they differ.

    We seed `random` deterministically inside each run so the test is
    reproducible: with two different seeds, the trace shapes differ.
    """
    sleeps_a: list[float] = []
    sleeps_b: list[float] = []

    async def _record_sleep(target: list[float], s: float) -> None:
        target.append(s)

    api_a = _FakeApi(fail_first_n=3)
    api_b = _FakeApi(fail_first_n=3)
    random.seed(11)
    with patch.object(embedder.asyncio, "sleep", new=lambda s: _record_sleep(sleeps_a, s)):
        await embedder.embed(["x"], client=api_a, model="text-embedding-3-small")

    random.seed(42)
    with patch.object(embedder.asyncio, "sleep", new=lambda s: _record_sleep(sleeps_b, s)):
        await embedder.embed(["x"], client=api_b, model="text-embedding-3-small")

    # Two different seeds → different traces.
    assert sleeps_a != sleeps_b
    # And neither matches the legacy fixed schedule.
    legacy_fixed = [1.0, 2.0, 4.0]
    assert sleeps_a != legacy_fixed
    assert sleeps_b != legacy_fixed


@pytest.mark.asyncio
async def test_jitter_ceiling_is_exponential() -> None:
    """Each draw is in [0, min(cap, base * 2^(n-1))]. With base=1, cap=16:
    attempts 1..5 ceilings are 1, 2, 4, 8, 16. We sample many times and
    assert no draw exceeds the ceiling for that attempt."""
    random.seed(0)
    for attempt in range(1, _ATTEMPT_CEILING_SAMPLE + 1):
        ceiling = min(embedder._RETRY_CAP_S, embedder._RETRY_BASE_S * (2 ** (attempt - 1)))
        for _ in range(200):
            s = embedder._full_jitter_backoff(attempt)
            assert 0.0 <= s <= ceiling


_ATTEMPT_CEILING_SAMPLE = 5


@pytest.mark.asyncio
async def test_batch_shedding_activates_after_two_rate_limits() -> None:
    """Two consecutive 429s in a single embed() call flips the local
    shed flag. The warning log carries the new effective concurrency."""
    api = _FakeApi(fail_first_n=2)  # 2 fails, then success

    async def _no_sleep(_: float) -> None:
        return None

    with patch.object(embedder.asyncio, "sleep", new=_no_sleep):
        # Drive `embed()` directly to capture shed state via log inspection.
        captured: list[dict[str, Any]] = []

        class _Cap(asyncio.Queue):  # type: ignore[type-arg]
            pass

        # Easier: monkeypatch the warning logger to capture extras.
        original_warning = embedder.logger.warning

        def _capture(msg: str, *args: Any, **kwargs: Any) -> None:
            extra = kwargs.get("extra") or {}
            captured.append({"msg": msg, **extra})
            return original_warning(msg, *args, **kwargs)

        with patch.object(embedder.logger, "warning", side_effect=_capture):
            await embedder.embed(["a"], client=api, model="text-embedding-3-small")

        sheds = [c for c in captured if c.get("msg") == "embed.batch_shedding_activated"]
        assert len(sheds) == 1
        assert sheds[0]["effective_concurrency"] == embedder._EMBED_CONCURRENCY // 2


@pytest.mark.asyncio
async def test_exhausted_retries_raises_last_rate_limit() -> None:
    """After 5 attempts of consistent 429, the RateLimitError surfaces.
    No swallow, no silent success."""
    api = _FakeApi(fail_first_n=10)  # always fail

    async def _no_sleep(_: float) -> None:
        return None

    with (
        patch.object(embedder.asyncio, "sleep", new=_no_sleep),
        pytest.raises(openai.RateLimitError),
    ):
        await embedder.embed(["a"], client=api, model="text-embedding-3-small")
