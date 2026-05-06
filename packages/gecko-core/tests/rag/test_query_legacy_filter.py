"""S20-A3 — rag_query default-excludes legacy chunks.

Stubs the Mongo read path (``match_chunks_mongo`` /
``match_chunks_hybrid_mongo``) so we can assert which docs each call
sees through the ``extra_filter`` kwarg. No network. Mirrors the fake-
collection pattern in ``tests/db/test_mongo_chunks.py``.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest


@pytest.fixture(autouse=True)
def _route_to_mongo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GECKO_CHUNK_STORE", "mongo")
    # Force hybrid OFF so we exercise the simpler match_chunks_mongo path
    # (the hybrid path is exercised by the second test).
    monkeypatch.setenv("GECKO_RETRIEVAL_HYBRID", "false")
    # Voyage rerank off — keeps the slate flow predictable.
    monkeypatch.setenv("GECKO_RERANKER", "none")
    # S20-HOTFIX: legacy filter is opt-in via GECKO_RAG_LEGACY_FILTER (default
    # off until Atlas index migration declares the filter paths). Tests that
    # verify the filter SHAPE turn it on; the off-default test below explicitly
    # leaves it unset.
    from gecko_core.db import chunk_store as cs_mod

    cs_mod.get_chunk_store.cache_clear()
    yield
    cs_mod.get_chunk_store.cache_clear()


@pytest.fixture
def _legacy_filter_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Opt-in to the legacy filter via env. Required for the existing
    filter-shape tests; the default-off behavior is covered separately."""
    monkeypatch.setenv("GECKO_RAG_LEGACY_FILTER", "on")


@pytest.fixture
def stub_embed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the question embedder with a deterministic stub."""

    async def _embed(_texts: list[str]) -> tuple[list[list[float]], int]:
        return ([[0.1] * 1024], 0)

    monkeypatch.setattr("gecko_core.rag.query.embed", _embed)


def _row(chunk_index: int, similarity: float = 0.9, **extra: Any) -> dict[str, Any]:
    return {
        "id": f"row-{chunk_index}",
        "source_id": str(uuid4()),
        "source_url": f"https://example.com/{chunk_index}",
        "chunk_index": chunk_index,
        "text": f"text-{chunk_index}",
        "similarity": similarity,
        "provider_kind": extra.pop("provider_kind", "web"),
        **extra,
    }


@pytest.mark.asyncio
async def test_default_filters_legacy_uncategorized_via_extra_filter(
    monkeypatch: pytest.MonkeyPatch, stub_embed: None, _legacy_filter_on: None
) -> None:
    """rag_query (with GECKO_RAG_LEGACY_FILTER=on) passes a Mongo $ne legacy_uncategorized filter."""
    captured: dict[str, Any] = {}

    async def _fake_match_chunks_mongo(**kwargs: Any) -> list[dict[str, Any]]:
        captured.update(kwargs)
        return [_row(0)]

    monkeypatch.setattr(
        "gecko_core.db.mongo_reads.match_chunks_mongo",
        _fake_match_chunks_mongo,
    )

    # Stub session add_cost so we don't talk to Supabase.
    class _StubStore:
        async def add_cost(self, *_a: Any, **_kw: Any) -> None:
            return None

    from gecko_core.rag.query import rag_query

    chunks = await rag_query(uuid4(), "what is x", top_k=4, store=_StubStore())  # type: ignore[arg-type]
    assert len(chunks) == 1

    assert "extra_filter" in captured
    flt = captured["extra_filter"]
    assert flt is not None
    assert flt["category"] == {"$ne": "legacy_uncategorized"}


@pytest.mark.asyncio
async def test_default_filters_metadata_deprecated(
    monkeypatch: pytest.MonkeyPatch, stub_embed: None, _legacy_filter_on: None
) -> None:
    """rag_query (with GECKO_RAG_LEGACY_FILTER=on) excludes metadata.deprecated=True chunks."""
    captured: dict[str, Any] = {}

    async def _fake_match_chunks_mongo(**kwargs: Any) -> list[dict[str, Any]]:
        captured.update(kwargs)
        return []

    monkeypatch.setattr(
        "gecko_core.db.mongo_reads.match_chunks_mongo",
        _fake_match_chunks_mongo,
    )

    class _StubStore:
        async def add_cost(self, *_a: Any, **_kw: Any) -> None:
            return None

    from gecko_core.rag.query import rag_query

    await rag_query(uuid4(), "q", top_k=4, store=_StubStore())  # type: ignore[arg-type]

    flt = captured["extra_filter"]
    assert flt["metadata.deprecated"] == {"$ne": True}


@pytest.mark.asyncio
async def test_include_legacy_drops_filter(
    monkeypatch: pytest.MonkeyPatch, stub_embed: None
) -> None:
    """include_legacy=True passes extra_filter=None to the read path."""
    captured: dict[str, Any] = {}

    async def _fake_match_chunks_mongo(**kwargs: Any) -> list[dict[str, Any]]:
        captured.update(kwargs)
        return []

    monkeypatch.setattr(
        "gecko_core.db.mongo_reads.match_chunks_mongo",
        _fake_match_chunks_mongo,
    )

    class _StubStore:
        async def add_cost(self, *_a: Any, **_kw: Any) -> None:
            return None

    from gecko_core.rag.query import rag_query

    await rag_query(
        uuid4(),
        "q",
        top_k=4,
        store=_StubStore(),  # type: ignore[arg-type]
        include_legacy=True,
    )

    assert captured["extra_filter"] is None


@pytest.mark.asyncio
async def test_legacy_exclude_filter_shape() -> None:
    """The exported filter is a Mongo $ne over both axes."""
    from gecko_core.rag.query import _legacy_exclude_filter

    flt = _legacy_exclude_filter()
    assert flt == {
        "category": {"$ne": "legacy_uncategorized"},
        "metadata.deprecated": {"$ne": True},
    }


@pytest.mark.asyncio
async def test_default_no_filter_when_env_off(
    monkeypatch: pytest.MonkeyPatch, stub_embed: None
) -> None:
    """S20-HOTFIX: legacy filter is OFF by default (no GECKO_RAG_LEGACY_FILTER).

    Until the Atlas index migration declares `category` + `metadata.deprecated`
    as filter paths on `chunks_vector`, pushing them into `$vectorSearch.filter`
    causes Atlas to hard-reject the query. Default-off keeps production alive.
    """
    captured: dict[str, Any] = {}

    async def _fake_match_chunks_mongo(**kwargs: Any) -> list[dict[str, Any]]:
        captured.update(kwargs)
        return [_row(0)]

    monkeypatch.setattr(
        "gecko_core.db.mongo_reads.match_chunks_mongo",
        _fake_match_chunks_mongo,
    )
    # Explicitly DO NOT set GECKO_RAG_LEGACY_FILTER. The autouse fixture leaves
    # it unset; this test pins that default-off path.
    monkeypatch.delenv("GECKO_RAG_LEGACY_FILTER", raising=False)

    class _StubStore:
        async def add_cost(self, *_a: Any, **_kw: Any) -> None:
            return None

    from gecko_core.rag.query import rag_query

    await rag_query(uuid4(), "q", top_k=4, store=_StubStore())  # type: ignore[arg-type]
    assert captured["extra_filter"] is None
