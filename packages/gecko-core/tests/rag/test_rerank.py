"""Unit tests for `gecko_core.rag.rerank` (S28 #25).

Light-fakes per `feedback_lighter_tests` — we monkeypatch the
``cohere`` SDK at the module surface (a single async ``client.rerank``
call) rather than spin up an httpx transport against Cohere's HTTP shape.
The contract we own is the function-level: cohere-key gate, retry behavior,
timeout behavior, return shape. The SDK's HTTP marshalling is not our test.
"""

from __future__ import annotations

import asyncio
import sys
import types
from typing import Any

import pytest

from gecko_core.rag import rerank as rerank_mod


# ---------------------------------------------------------------------------
# Fake Cohere SDK — installed once per test via monkeypatch.setitem on
# sys.modules so the lazy `import cohere` inside cohere_rerank picks it up.
# ---------------------------------------------------------------------------


class _FakeRerankResultRow:
    def __init__(self, index: int, score: float) -> None:
        self.index = index
        self.relevance_score = score


class _FakeRerankResponse:
    def __init__(self, pairs: list[tuple[int, float]]) -> None:
        self.results = [_FakeRerankResultRow(i, s) for i, s in pairs]


class _FakeAsyncClient:
    """Records call args and returns a programmable response."""

    def __init__(
        self,
        *,
        api_key: str,
        response: _FakeRerankResponse | None = None,
        raise_first_429: bool = False,
        raise_timeout: bool = False,
    ) -> None:
        self.api_key = api_key
        self._response = response or _FakeRerankResponse([(0, 0.9), (1, 0.5)])
        self._raise_first_429 = raise_first_429
        self._raise_timeout = raise_timeout
        self.call_count = 0
        self.last_call: dict[str, Any] = {}

    async def rerank(self, **kwargs: Any) -> _FakeRerankResponse:
        self.call_count += 1
        self.last_call = kwargs
        if self._raise_timeout:
            # Simulate a hang that the asyncio.wait_for boundary will catch.
            await asyncio.sleep(60)
        if self._raise_first_429 and self.call_count == 1:
            raise RuntimeError("429 rate_limit_exceeded: retry later")
        return self._response


def _install_fake_cohere(monkeypatch: pytest.MonkeyPatch, client: _FakeAsyncClient) -> None:
    """Inject a fake ``cohere`` module exposing ``AsyncClient`` as a factory."""
    fake = types.ModuleType("cohere")

    def _factory(api_key: str) -> _FakeAsyncClient:
        # Re-bind the api_key the test cares about while keeping the
        # programmable response state.
        client.api_key = api_key
        return client

    fake.AsyncClient = _factory  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "cohere", fake)


@pytest.fixture(autouse=True)
def _reset_no_key_warn() -> None:
    """Re-arm the once-per-process 'no_key' warning latch between tests."""
    rerank_mod._NO_KEY_WARNED = False


# ---------------------------------------------------------------------------
# Fixture 1 — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cohere_rerank_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COHERE_API_KEY", "test-key-abc")
    # Disable Mongo cache by ensuring `_db()` returns None (it does in CI
    # when MONGODB_URI is unset; belt-and-suspenders explicit override).
    monkeypatch.setattr(rerank_mod, "_rerank_cache_coll", lambda: None)

    fake_client = _FakeAsyncClient(
        api_key="ignored",
        response=_FakeRerankResponse([(2, 0.95), (0, 0.81), (1, 0.42)]),
    )
    _install_fake_cohere(monkeypatch, fake_client)

    docs = ["alpha chunk", "beta chunk", "gamma chunk"]
    pairs = await rerank_mod.cohere_rerank(
        query="what is the protocol risk",
        documents=docs,
        top_n=2,
    )

    assert pairs == [(2, 0.95), (0, 0.81)]
    assert fake_client.call_count == 1
    assert fake_client.last_call["query"] == "what is the protocol risk"
    assert fake_client.last_call["model"] == "rerank-v3.5"
    assert fake_client.last_call["top_n"] == 2
    assert fake_client.last_call["documents"] == docs


# ---------------------------------------------------------------------------
# Fixture 2 — 429 retry succeeds on second attempt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cohere_rerank_retries_once_on_429(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COHERE_API_KEY", "test-key-abc")
    monkeypatch.setattr(rerank_mod, "_rerank_cache_coll", lambda: None)
    # Skip the backoff sleep so the test is fast.
    monkeypatch.setattr(rerank_mod, "RERANK_RETRY_429_BACKOFF_S", 0.0)

    fake_client = _FakeAsyncClient(
        api_key="ignored",
        response=_FakeRerankResponse([(0, 0.7)]),
        raise_first_429=True,
    )
    _install_fake_cohere(monkeypatch, fake_client)

    pairs = await rerank_mod.cohere_rerank(
        query="q",
        documents=["d1", "d2"],
        top_n=1,
    )

    assert pairs == [(0, 0.7)]
    assert fake_client.call_count == 2  # one 429 + one success


# ---------------------------------------------------------------------------
# Fixture 3 — timeout degrades to empty
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cohere_rerank_timeout_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COHERE_API_KEY", "test-key-abc")
    monkeypatch.setattr(rerank_mod, "_rerank_cache_coll", lambda: None)
    monkeypatch.setattr(rerank_mod, "RERANK_TIMEOUT_S", 0.05)

    fake_client = _FakeAsyncClient(api_key="ignored", raise_timeout=True)
    _install_fake_cohere(monkeypatch, fake_client)

    pairs = await rerank_mod.cohere_rerank(
        query="q",
        documents=["d1"],
        top_n=1,
    )

    assert pairs == []  # graceful degrade


# ---------------------------------------------------------------------------
# Bonus reachability — no key path skips SDK entirely (Pattern E).
# Counts as a 4th fixture; well under the 5-fixture ceiling.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cohere_rerank_no_key_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COHERE_API_KEY", raising=False)
    # Plant a fake cohere SDK that would explode if invoked.
    fake_client = _FakeAsyncClient(api_key="ignored", raise_timeout=True)
    _install_fake_cohere(monkeypatch, fake_client)

    pairs = await rerank_mod.cohere_rerank(query="q", documents=["d"], top_n=1)

    assert pairs == []
    assert fake_client.call_count == 0  # SDK was never reached
