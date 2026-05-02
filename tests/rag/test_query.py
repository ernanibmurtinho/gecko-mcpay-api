"""Unit tests for `gecko_core.rag.query.rag_query`.

Mocks the supabase RPC seam and the embedder. No network.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest


class _FakeRpcResp:
    def __init__(self, data: list[dict[str, Any]]) -> None:
        self.data = data


class _FakeRpc:
    def __init__(self, resp: _FakeRpcResp) -> None:
        self._resp = resp
        self.last_args: tuple[str, dict[str, Any]] | None = None

    def __call__(self, name: str, params: dict[str, Any]) -> _FakeRpc:
        self.last_args = (name, params)
        return self

    def execute(self) -> _FakeRpcResp:
        return self._resp


class _FakeClient:
    def __init__(self, resp: _FakeRpcResp) -> None:
        self.rpc = _FakeRpc(resp)


class _FakeRpcRouter:
    """Two-RPC fake — returns different payloads for hybrid vs legacy.

    Lets a single test exercise the hybrid path AND the fallback path
    by swapping which name maps to which response.
    """

    def __init__(self, responses: dict[str, _FakeRpcResp | Exception]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._pending: _FakeRpcResp | Exception | None = None

    def __call__(self, name: str, params: dict[str, Any]) -> _FakeRpcRouter:
        self.calls.append((name, params))
        self._pending = self._responses.get(name)
        return self

    def execute(self) -> _FakeRpcResp:
        pending = self._pending
        if isinstance(pending, Exception):
            raise pending
        if pending is None:
            return _FakeRpcResp([])
        return pending


class _FakeClientRouted:
    def __init__(self, router: _FakeRpcRouter) -> None:
        self.rpc = router


@pytest.mark.asyncio
async def test_rag_query_returns_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    from gecko_core.rag import query as q

    # Force the legacy (non-hybrid) path so this test stays focused on the
    # match_chunks contract; the hybrid path has dedicated tests below.
    monkeypatch.setenv("GECKO_RETRIEVAL_HYBRID", "false")

    sid = uuid4()
    chunk_source_id = uuid4()
    fake_resp = _FakeRpcResp(
        [
            {
                "id": str(uuid4()),
                "source_id": str(chunk_source_id),
                "source_url": "https://example.com/a",
                "chunk_index": 0,
                "text": "hello",
                "similarity": 0.91,
            },
            {
                "id": str(uuid4()),
                "source_id": str(chunk_source_id),
                "source_url": "https://example.com/b",
                "chunk_index": 3,
                "text": "world",
                "similarity": 0.72,
            },
        ]
    )

    fake_store = MagicMock()
    fake_store._client = _FakeClient(fake_resp)

    async def _fake_embed(texts: list[str]) -> tuple[list[list[float]], int]:
        return [[0.1] * 1536], 0

    monkeypatch.setattr(q, "embed", _fake_embed)

    chunks = await q.rag_query(sid, "what is x?", top_k=5, store=fake_store)

    assert len(chunks) == 2
    assert chunks[0].similarity == pytest.approx(0.91)
    assert str(chunks[0].source_url) == "https://example.com/a"
    assert chunks[1].chunk_index == 3
    name, params = fake_store._client.rpc.last_args
    assert name == "match_chunks"
    assert params["p_session_id"] == str(sid)
    # S17-WEDGE-CITE-03 — rag_query over-fetches by 2x so the per-provider
    # boost can pull structured-provider chunks into the final top_k from
    # below the raw cosine cutoff. Caller's top_k still bounds the returned
    # list (asserted above: len(chunks) == 2 here, since the fake RPC
    # returned only 2 rows).
    assert params["match_count"] == 10


@pytest.mark.asyncio
async def test_rag_query_empty_question() -> None:
    from gecko_core.rag import query as q

    fake_store = MagicMock()
    fake_store._client = _FakeClient(_FakeRpcResp([]))
    chunks = await q.rag_query(uuid4(), "   ", top_k=5, store=fake_store)
    assert chunks == []


@pytest.mark.asyncio
async def test_rag_query_zero_top_k() -> None:
    from gecko_core.rag import query as q

    fake_store = MagicMock()
    fake_store._client = _FakeClient(_FakeRpcResp([]))
    chunks = await q.rag_query(uuid4(), "x", top_k=0, store=fake_store)
    assert chunks == []


# ---------------------------------------------------------------------------
# S17-WEDGE-CITE-03 — per-provider boost reranker.
# ---------------------------------------------------------------------------


def _mk_chunk(sim: float, kind: str, src: str = "https://example.com/x", idx: int = 0):
    from uuid import uuid4

    from gecko_core.rag.query import RagChunk

    return RagChunk(
        source_id=uuid4(),
        source_url=src,
        chunk_index=idx,
        text="t",
        similarity=sim,
        provider_kind=kind,  # type: ignore[arg-type]
    )


def test_rerank_promotes_bazaar_above_web_at_close_sim() -> None:
    from gecko_core.rag.query import _rerank_by_provider

    web = _mk_chunk(0.80, "web", "https://example.com/web")
    bazaar = _mk_chunk(0.72, "bazaar", "bazaar://svc/r1")
    arxiv = _mk_chunk(0.70, "arxiv", "https://arxiv.org/abs/0001.0001")
    twitsh = _mk_chunk(0.85, "twitsh", "twitsh://t/1")
    out = _rerank_by_provider([web, bazaar, arxiv, twitsh], top_k=4)
    # twitsh 0.85 * 0.95 = 0.8075; web 0.80; bazaar 0.72 * 1.15 = 0.828; arxiv 0.70 * 1.10 = 0.770
    kinds_in_order = [c.provider_kind for c in out]
    assert kinds_in_order == ["bazaar", "twitsh", "web", "arxiv"]


def test_rerank_truncates_to_top_k() -> None:
    from gecko_core.rag.query import _rerank_by_provider

    chunks = [_mk_chunk(0.5 + i * 0.01, "web") for i in range(10)]
    out = _rerank_by_provider(chunks, top_k=3)
    assert len(out) == 3


def test_rerank_env_override(monkeypatch) -> None:
    from gecko_core.rag.query import _rerank_by_provider

    monkeypatch.setenv("GECKO_RETRIEVAL_WEIGHT_BAZAAR", "2.0")
    web = _mk_chunk(0.90, "web")
    bazaar = _mk_chunk(0.50, "bazaar", "bazaar://svc/r")
    out = _rerank_by_provider([web, bazaar], top_k=2)
    # bazaar 0.50 * 2.0 = 1.0 (clamped); should win
    assert out[0].provider_kind == "bazaar"


def test_citation_uri_validator_accepts_bazaar_and_twitsh() -> None:
    from gecko_core.models import Citation

    c1 = Citation(source_url="bazaar://crypto-onramp/coinbase", chunk_index=0, similarity=0.5)
    c2 = Citation(source_url="twitsh://session/abc-123", chunk_index=0, similarity=0.5)
    c3 = Citation(source_url="https://arxiv.org/abs/2401.12345", chunk_index=0, similarity=0.5)
    assert c1.source_url.startswith("bazaar://")
    assert c2.source_url.startswith("twitsh://")
    assert c3.source_url.startswith("https://")


def test_citation_uri_validator_rejects_garbage() -> None:
    import pytest
    from gecko_core.models import Citation

    with pytest.raises(Exception):
        Citation(source_url="ftp://nope/", chunk_index=0, similarity=0.5)
    with pytest.raises(Exception):
        Citation(source_url="not-a-url", chunk_index=0, similarity=0.5)


# ---------------------------------------------------------------------------
# S17-WEDGE-CITE-04 — hybrid retrieval reachability tests.
#
# These are reachability checks per feedback_wedge_reachability_check memory:
# every "we shipped X" claim demands an end-to-end check that the structured
# provider chunks actually reach the LLM, not just live in the DB.
# ---------------------------------------------------------------------------


def _row(source_url: str, sim: float, kind: str, idx: int = 0) -> dict[str, Any]:
    return {
        "id": str(uuid4()),
        "source_id": str(uuid4()),
        "source_url": source_url,
        "chunk_index": idx,
        "text": f"text-{kind}-{idx}",
        "similarity": sim,
        "provider_kind": kind,
    }


@pytest.mark.asyncio
async def test_rag_query_uses_hybrid_rpc_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default (no env set) must call match_chunks_hybrid, not match_chunks."""
    from gecko_core.rag import query as q

    monkeypatch.delenv("GECKO_RETRIEVAL_HYBRID", raising=False)

    router = _FakeRpcRouter(
        {"match_chunks_hybrid": _FakeRpcResp([_row("https://example.com/a", 0.9, "web")])}
    )
    fake_store = MagicMock()
    fake_store._client = _FakeClientRouted(router)

    async def _fake_embed(texts: list[str]) -> tuple[list[list[float]], int]:
        return [[0.1] * 1536], 0

    monkeypatch.setattr(q, "embed", _fake_embed)

    chunks = await q.rag_query(uuid4(), "x", top_k=5, store=fake_store)
    assert len(chunks) == 1
    assert router.calls[0][0] == "match_chunks_hybrid"
    params = router.calls[0][1]
    assert params["match_count"] == 10  # over-fetch 2x
    assert params["per_kind_quota"] == 2  # default


@pytest.mark.asyncio
async def test_rag_query_hybrid_falls_back_when_rpc_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Migration not yet applied -> hybrid RPC errors -> fall back gracefully."""
    from gecko_core.rag import query as q

    monkeypatch.delenv("GECKO_RETRIEVAL_HYBRID", raising=False)
    router = _FakeRpcRouter(
        {
            "match_chunks_hybrid": RuntimeError("function does not exist"),
            "match_chunks": _FakeRpcResp([_row("https://example.com/a", 0.8, "web")]),
        }
    )
    fake_store = MagicMock()
    fake_store._client = _FakeClientRouted(router)

    async def _fake_embed(texts: list[str]) -> tuple[list[list[float]], int]:
        return [[0.1] * 1536], 0

    monkeypatch.setattr(q, "embed", _fake_embed)

    chunks = await q.rag_query(uuid4(), "x", top_k=5, store=fake_store)
    assert len(chunks) == 1
    # Both RPCs were attempted in order: hybrid first, then legacy fallback.
    assert [c[0] for c in router.calls] == ["match_chunks_hybrid", "match_chunks"]


@pytest.mark.asyncio
async def test_rag_query_preserves_provider_chunks_in_topk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reachability: when the hybrid RPC returns a slate that includes
    bazaar/twitsh chunks, retrieve() must return at least one non-web
    chunk in the final top_k. Demo claim 'Bazaar shapes the verdict'
    requires this — without it the wedge is invisible to the LLM.
    """
    from gecko_core.rag import query as q

    monkeypatch.delenv("GECKO_RETRIEVAL_HYBRID", raising=False)
    # Mix a dominant pile of web chunks with one bazaar + one twitsh.
    rows = [_row(f"https://example.com/{i}", 0.85 - i * 0.01, "web", idx=i) for i in range(8)]
    rows.append(_row("bazaar://svc/onramp", 0.55, "bazaar"))
    rows.append(_row("twitsh://session/abc", 0.50, "twitsh"))

    router = _FakeRpcRouter({"match_chunks_hybrid": _FakeRpcResp(rows)})
    fake_store = MagicMock()
    fake_store._client = _FakeClientRouted(router)

    async def _fake_embed(texts: list[str]) -> tuple[list[list[float]], int]:
        return [[0.1] * 1536], 0

    monkeypatch.setattr(q, "embed", _fake_embed)

    chunks = await q.rag_query(uuid4(), "x402 services", top_k=5, store=fake_store)
    kinds = {c.provider_kind for c in chunks}
    # At least one non-web kind must survive the boost+truncate stage.
    assert kinds & {"bazaar", "twitsh", "arxiv"}, (
        f"reachability regression — top_k contains only {kinds}"
    )


@pytest.mark.asyncio
async def test_rag_query_per_kind_quota_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gecko_core.rag import query as q

    monkeypatch.delenv("GECKO_RETRIEVAL_HYBRID", raising=False)
    monkeypatch.setenv("GECKO_RETRIEVAL_PER_KIND_QUOTA", "5")

    router = _FakeRpcRouter({"match_chunks_hybrid": _FakeRpcResp([])})
    fake_store = MagicMock()
    fake_store._client = _FakeClientRouted(router)

    async def _fake_embed(texts: list[str]) -> tuple[list[list[float]], int]:
        return [[0.1] * 1536], 0

    monkeypatch.setattr(q, "embed", _fake_embed)

    await q.rag_query(uuid4(), "x", top_k=5, store=fake_store)
    assert router.calls[0][1]["per_kind_quota"] == 5
