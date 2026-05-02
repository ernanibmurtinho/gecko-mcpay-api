"""S16-INGEST-03 — chunk_embedding_cache PK now includes embed_model.

Two rows for the same (url_hash, chunk_index) under different models
coexist; the lookup filters on the active model so a model swap doesn't
return a stale (poisoned) vector.

Driven by the same fake supabase client as
`test_insert_chunks_transactional.py` — kept narrow so the test
remains transport-shaped without standing up a real DB.
"""

from __future__ import annotations

from typing import Any

import pytest
from gecko_core.sessions.store import SessionStore


class _Resp:
    def __init__(self, data: list[dict[str, Any]]) -> None:
        self.data = data


class _CacheTable:
    """Records writes; supports filtered selects on (url_hash, chunk_index, embed_model)."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self._op: str | None = None
        self._payload: list[dict[str, Any]] | None = None
        self._filters: dict[str, Any] = {}

    def select(self, *_: Any) -> _CacheTable:
        self._op = "select"
        self._filters = {}
        return self

    def upsert(
        self,
        payload: list[dict[str, Any]],
        *,
        on_conflict: str | None = None,
        ignore_duplicates: bool = False,
    ) -> _CacheTable:
        self._op = "upsert"
        self._payload = payload
        self._on_conflict = on_conflict
        return self

    def delete(self) -> _CacheTable:
        self._op = "delete"
        self._filters = {}
        return self

    def eq(self, col: str, val: Any) -> _CacheTable:
        self._filters[col] = ("eq", val)
        return self

    def in_(self, col: str, vals: list[Any]) -> _CacheTable:
        self._filters[col] = ("in", list(vals))
        return self

    def execute(self) -> _Resp:
        if self._op == "upsert":
            payload = self._payload or []
            for r in payload:
                # Honor ON CONFLICT (url_hash, chunk_index, embed_model)
                # DO NOTHING semantics: only insert if the triple is new.
                key = (
                    r["url_hash"],
                    r["chunk_index"],
                    r.get("embed_model", "text-embedding-3-small"),
                )
                existing = {
                    (
                        x["url_hash"],
                        x["chunk_index"],
                        x.get("embed_model", "text-embedding-3-small"),
                    )
                    for x in self.rows
                }
                if key not in existing:
                    self.rows.append(
                        {**r, "embed_model": r.get("embed_model", "text-embedding-3-small")}
                    )
            return _Resp(payload)
        if self._op == "select":
            out: list[dict[str, Any]] = []
            for r in self.rows:
                ok = True
                for col, (kind, val) in self._filters.items():
                    rv = r.get(col)
                    if kind == "eq" and rv != val:
                        ok = False
                        break
                    if kind == "in" and rv not in val:
                        ok = False
                        break
                if ok:
                    out.append(r)
            return _Resp(out)
        if self._op == "delete":
            keep: list[dict[str, Any]] = []
            for r in self.rows:
                drop = True
                for col, (kind, val) in self._filters.items():
                    rv = r.get(col)
                    if kind == "eq" and rv != val:
                        drop = False
                    if kind == "in" and rv not in val:
                        drop = False
                if not drop:
                    keep.append(r)
            self.rows = keep
            return _Resp([])
        raise AssertionError(f"unhandled op {self._op}")


class _Client:
    def __init__(self, cache: _CacheTable) -> None:
        self._cache = cache

    def table(self, name: str) -> _CacheTable:
        assert name == SessionStore.CHUNK_EMBEDDING_CACHE_TABLE
        return self._cache


def _make_store() -> tuple[SessionStore, _CacheTable]:
    cache = _CacheTable()
    s = SessionStore.__new__(SessionStore)
    s._client = _Client(cache)  # type: ignore[attr-defined]
    return s, cache


@pytest.mark.asyncio
async def test_put_and_get_under_default_model() -> None:
    s, cache = _make_store()
    rows = [(0, "hello", [0.0] * SessionStore.EMBED_DIM)]
    await s.put_chunk_cache("h1", rows, embed_model="text-embedding-3-small")
    out = await s.get_chunk_cache("h1", [0], embed_model="text-embedding-3-small")
    assert 0 in out
    assert len(out[0]) == SessionStore.EMBED_DIM
    assert len(cache.rows) == 1


@pytest.mark.asyncio
async def test_model_change_isolates_cache_lookup() -> None:
    """Cache row stored under text-embedding-3-small must NOT be returned
    when a caller asks for text-embedding-3-large. Both rows can coexist."""
    s, cache = _make_store()
    await s.put_chunk_cache(
        "h1",
        [(0, "hello", [0.0] * SessionStore.EMBED_DIM)],
        embed_model="text-embedding-3-small",
    )
    # Lookup under a different model: miss.
    out_large = await s.get_chunk_cache("h1", [0], embed_model="text-embedding-3-large")
    assert out_large == {}

    # After re-embedding under the new model and caching, both rows coexist.
    await s.put_chunk_cache(
        "h1",
        [(0, "hello", [0.0] * SessionStore.EMBED_DIM)],
        embed_model="text-embedding-3-large",
    )
    out_large2 = await s.get_chunk_cache("h1", [0], embed_model="text-embedding-3-large")
    assert 0 in out_large2
    # Still 2 distinct PK rows.
    keys = {(r["url_hash"], r["chunk_index"], r["embed_model"]) for r in cache.rows}
    assert keys == {
        ("h1", 0, "text-embedding-3-small"),
        ("h1", 0, "text-embedding-3-large"),
    }


@pytest.mark.asyncio
async def test_no_embed_model_filter_returns_legacy_rows() -> None:
    """Backwards compat: a caller that doesn't pass embed_model still
    gets cache hits. Used by tests / older callers."""
    s, cache = _make_store()
    # Seed a row at the legacy default (column DEFAULT applies).
    cache.rows.append(
        {
            "url_hash": "h1",
            "chunk_index": 0,
            "embedding": [0.0] * SessionStore.EMBED_DIM,
            "embed_model": "text-embedding-3-small",
        }
    )
    out = await s.get_chunk_cache("h1", [0])  # no model filter
    assert 0 in out
