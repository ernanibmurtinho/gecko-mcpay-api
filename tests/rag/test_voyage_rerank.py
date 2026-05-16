"""Unit tests for `gecko_core.rag.voyage_rerank`.

Mocks `voyageai.AsyncClient` via sys.modules injection. No network.
The legacy install does not have `voyageai` on the path; tests inject a
fake module so the lazy `import voyageai` inside the rerank function
resolves to our fake.
"""

from __future__ import annotations

import logging
import sys
import types
from typing import Any
from uuid import uuid4

import pytest
from gecko_core.rag.query import RagChunk
from gecko_core.rag.voyage_rerank import (
    RERANK_TOP_K_INPUT,
    voyage_rerank,
    voyage_rerank_dicts,
)

# ---------------------------------------------------------------------------
# Fixtures and fakes
# ---------------------------------------------------------------------------


def _make_chunk(idx: int, sim: float = 0.8, kind: str = "web") -> RagChunk:
    return RagChunk(
        source_id=uuid4(),
        source_url=f"https://example.com/doc/{idx}",
        chunk_index=idx,
        text=f"chunk {idx} content",
        similarity=sim,
        provider_kind=kind,  # type: ignore[arg-type]
    )


class _FakeRerankResult:
    def __init__(self, index: int, score: float) -> None:
        self.index = index
        self.relevance_score = score


class _FakeRerankResponse:
    def __init__(self, results: list[_FakeRerankResult]) -> None:
        self.results = results


class _FakeAsyncClient:
    """Records the kwargs of each .rerank call. Returns scripted responses."""

    last_call_kwargs: dict[str, Any] | None = None
    next_response: _FakeRerankResponse | Exception | None = None
    call_count: int = 0

    def __init__(self, api_key: str = "") -> None:
        self.api_key = api_key

    async def rerank(self, **kwargs: Any) -> _FakeRerankResponse:
        type(self).last_call_kwargs = kwargs
        type(self).call_count += 1
        nxt = type(self).next_response
        if isinstance(nxt, Exception):
            raise nxt
        if nxt is None:
            # Default: return identity-order with descending scores.
            docs = kwargs.get("documents", [])
            results = [_FakeRerankResult(i, score=1.0 - 0.01 * i) for i in range(len(docs))]
            return _FakeRerankResponse(results)
        return nxt


@pytest.fixture
def fake_voyage(monkeypatch: pytest.MonkeyPatch) -> type[_FakeAsyncClient]:
    """Inject a fake `voyageai` module so the lazy import inside
    voyage_rerank resolves to our fake."""
    _FakeAsyncClient.last_call_kwargs = None
    _FakeAsyncClient.next_response = None
    _FakeAsyncClient.call_count = 0

    fake_module = types.ModuleType("voyageai")
    fake_module.AsyncClient = _FakeAsyncClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "voyageai", fake_module)
    return _FakeAsyncClient


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rerank_off_returns_input_truncated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`GECKO_RERANKER` unset -> no-op, returns chunks[:top_n] unchanged."""
    monkeypatch.delenv("GECKO_RERANKER", raising=False)
    chunks = [_make_chunk(i) for i in range(12)]
    out = await voyage_rerank("query", chunks, top_n=8)
    assert len(out) == 8
    # Identity preserved — no rerank_score populated.
    for original, returned in zip(chunks[:8], out, strict=True):
        assert returned is original
        assert returned.rerank_score is None


@pytest.mark.asyncio
async def test_rerank_no_api_key_falls_through(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Flag on but key missing -> fallback + warning logged."""
    monkeypatch.setenv("GECKO_RERANKER", "voyage")
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    chunks = [_make_chunk(i) for i in range(10)]
    with caplog.at_level(logging.WARNING, logger="gecko_core.rag.voyage_rerank"):
        out = await voyage_rerank("query", chunks, top_n=5)
    assert len(out) == 5
    assert all(c.rerank_score is None for c in out)
    assert any("no_key" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_rerank_reorders_by_voyage_score(
    monkeypatch: pytest.MonkeyPatch,
    fake_voyage: type[_FakeAsyncClient],
) -> None:
    """Flag on + key set + mock returns reversed scores -> chunk order
    matches Voyage's, not input order."""
    monkeypatch.setenv("GECKO_RERANKER", "voyage")
    monkeypatch.setenv("VOYAGE_API_KEY", "pa-test-key")
    chunks = [_make_chunk(i) for i in range(5)]
    # Reverse order: best result is index 4, then 3, 2, 1, 0.
    fake_voyage.next_response = _FakeRerankResponse(
        [
            _FakeRerankResult(4, 0.99),
            _FakeRerankResult(3, 0.85),
            _FakeRerankResult(2, 0.70),
            _FakeRerankResult(1, 0.55),
            _FakeRerankResult(0, 0.40),
        ]
    )
    out = await voyage_rerank("query", chunks, top_n=5)
    assert [c.chunk_index for c in out] == [4, 3, 2, 1, 0]
    assert [round(c.rerank_score or 0.0, 2) for c in out] == [0.99, 0.85, 0.70, 0.55, 0.40]


@pytest.mark.asyncio
async def test_rerank_preserves_similarity_field(
    monkeypatch: pytest.MonkeyPatch,
    fake_voyage: type[_FakeAsyncClient],
) -> None:
    """`similarity` is unchanged after rerank — Voyage score lives in
    `rerank_score`, the citation-grounding floor still reads cosine."""
    monkeypatch.setenv("GECKO_RERANKER", "voyage")
    monkeypatch.setenv("VOYAGE_API_KEY", "pa-test-key")
    sims = [0.91, 0.82, 0.73]
    chunks = [_make_chunk(i, sim=sims[i]) for i in range(3)]
    fake_voyage.next_response = _FakeRerankResponse(
        [
            _FakeRerankResult(2, 0.99),
            _FakeRerankResult(0, 0.85),
            _FakeRerankResult(1, 0.50),
        ]
    )
    out = await voyage_rerank("query", chunks, top_n=3)
    sim_by_idx = {c.chunk_index: c.similarity for c in out}
    assert sim_by_idx == {0: 0.91, 1: 0.82, 2: 0.73}


@pytest.mark.asyncio
async def test_rerank_timeout_falls_through(
    monkeypatch: pytest.MonkeyPatch,
    fake_voyage: type[_FakeAsyncClient],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """asyncio.TimeoutError -> return chunks[:top_n] + warn."""
    monkeypatch.setenv("GECKO_RERANKER", "voyage")
    monkeypatch.setenv("VOYAGE_API_KEY", "pa-test-key")
    fake_voyage.next_response = TimeoutError()
    chunks = [_make_chunk(i) for i in range(10)]
    with caplog.at_level(logging.WARNING, logger="gecko_core.rag.voyage_rerank"):
        out = await voyage_rerank("query", chunks, top_n=4)
    assert len(out) == 4
    assert [c.chunk_index for c in out] == [0, 1, 2, 3]
    assert all(c.rerank_score is None for c in out)
    assert any("timeout" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_rerank_caps_input_at_20(
    monkeypatch: pytest.MonkeyPatch,
    fake_voyage: type[_FakeAsyncClient],
) -> None:
    """Pass 50 chunks -> Voyage was called with exactly 20 documents."""
    monkeypatch.setenv("GECKO_RERANKER", "voyage")
    monkeypatch.setenv("VOYAGE_API_KEY", "pa-test-key")
    chunks = [_make_chunk(i) for i in range(50)]
    out = await voyage_rerank("query", chunks, top_n=8)
    assert fake_voyage.last_call_kwargs is not None
    docs = fake_voyage.last_call_kwargs["documents"]
    assert len(docs) == RERANK_TOP_K_INPUT == 20
    # Returned slate is bounded by top_n.
    assert len(out) == 8


@pytest.mark.asyncio
async def test_rerank_generic_exception_falls_through(
    monkeypatch: pytest.MonkeyPatch,
    fake_voyage: type[_FakeAsyncClient],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Any non-timeout exception -> fallback to input slate."""
    monkeypatch.setenv("GECKO_RERANKER", "voyage")
    monkeypatch.setenv("VOYAGE_API_KEY", "pa-test-key")
    fake_voyage.next_response = RuntimeError("voyage 503")
    chunks = [_make_chunk(i) for i in range(6)]
    with caplog.at_level(logging.WARNING, logger="gecko_core.rag.voyage_rerank"):
        out = await voyage_rerank("query", chunks, top_n=4)
    assert len(out) == 4
    assert any("voyage 503" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# voyage_rerank_dicts — dict-native sibling (S33-#79, trade-panel path)
# ---------------------------------------------------------------------------


def _make_dict(idx: int, score: float = 0.8) -> dict[str, Any]:
    return {"id": str(idx), "text": f"chunk {idx} content", "score": score}


@pytest.mark.asyncio
async def test_rerank_dicts_off_returns_input_truncated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flag unset -> no-op, returns chunks[:top_n], no rerank_score key."""
    monkeypatch.delenv("GECKO_RERANKER", raising=False)
    chunks = [_make_dict(i) for i in range(12)]
    out = await voyage_rerank_dicts("query", chunks, top_n=8)
    assert len(out) == 8
    assert [c["id"] for c in out] == [str(i) for i in range(8)]
    assert all("rerank_score" not in c for c in out)


@pytest.mark.asyncio
async def test_rerank_dicts_reorders_and_populates_score(
    monkeypatch: pytest.MonkeyPatch,
    fake_voyage: type[_FakeAsyncClient],
) -> None:
    """Flag on -> dicts reordered by Voyage score, rerank_score populated,
    original cosine `score` field left untouched."""
    monkeypatch.setenv("GECKO_RERANKER", "voyage")
    monkeypatch.setenv("VOYAGE_API_KEY", "pa-test-key")
    chunks = [_make_dict(i, score=0.81) for i in range(5)]
    fake_voyage.next_response = _FakeRerankResponse(
        [
            _FakeRerankResult(4, 0.99),
            _FakeRerankResult(0, 0.70),
            _FakeRerankResult(2, 0.40),
        ]
    )
    out = await voyage_rerank_dicts("query", chunks, top_n=3)
    assert [c["id"] for c in out] == ["4", "0", "2"]
    assert [round(c["rerank_score"], 2) for c in out] == [0.99, 0.70, 0.40]
    # Cosine score field untouched.
    assert all(c["score"] == 0.81 for c in out)


@pytest.mark.asyncio
async def test_rerank_dicts_timeout_falls_through(
    monkeypatch: pytest.MonkeyPatch,
    fake_voyage: type[_FakeAsyncClient],
) -> None:
    """Timeout -> graceful degrade to vector-order slate[:top_n]."""
    monkeypatch.setenv("GECKO_RERANKER", "voyage")
    monkeypatch.setenv("VOYAGE_API_KEY", "pa-test-key")
    fake_voyage.next_response = TimeoutError()
    chunks = [_make_dict(i) for i in range(10)]
    out = await voyage_rerank_dicts("query", chunks, top_n=4)
    assert [c["id"] for c in out] == ["0", "1", "2", "3"]
    assert all("rerank_score" not in c for c in out)


@pytest.mark.asyncio
async def test_rerank_dicts_caps_input_at_20(
    monkeypatch: pytest.MonkeyPatch,
    fake_voyage: type[_FakeAsyncClient],
) -> None:
    """Over-fetch slate of 50 -> Voyage called with exactly 20 docs."""
    monkeypatch.setenv("GECKO_RERANKER", "voyage")
    monkeypatch.setenv("VOYAGE_API_KEY", "pa-test-key")
    chunks = [_make_dict(i) for i in range(50)]
    out = await voyage_rerank_dicts("query", chunks, top_n=8)
    assert fake_voyage.last_call_kwargs is not None
    assert len(fake_voyage.last_call_kwargs["documents"]) == RERANK_TOP_K_INPUT == 20
    assert len(out) == 8
