"""S16-INGEST-02 — fault-injection tests for the transactional
`SessionStore.insert_chunks` path and the cache dim-validate eviction.

We don't stand up a real Supabase client. Instead we drive a fake
`Client` shape that mimics the chained builder API we actually call
(`.table(...).upsert(...).execute()`) and lets each test inject the
exact failure mode it wants:
  - TOAST-sized payload → 413 status → retry with halved batch
  - Mocked 5xx → retry with halved batch
  - Cache row with dim 768 → eviction + re-embed signal
  - Idempotent re-run → no duplicate rows

The FakeStore in `test_pipeline.py` deliberately can't exercise these
paths (it succeeds unconditionally — Pattern C from CLAUDE.md). This
file is the proper-faked counterpart.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

import pytest
from gecko_core.ingestion.exceptions import ChunkValidationError
from gecko_core.sessions.store import SessionStore


@pytest.fixture(autouse=True)
def _pin_supabase_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin chunk store to supabase regardless of developer .env.

    These tests mock the Supabase chained-builder seam; a developer-set
    ``GECKO_CHUNK_STORE=mongo`` would route past the mock and fail with
    a kwarg-mismatch on the (unmocked) Mongo signature.
    """
    monkeypatch.setenv("GECKO_CHUNK_STORE", "supabase")
    from gecko_core.db import chunk_store as cs_mod

    cs_mod.get_chunk_store.cache_clear()
    yield
    cs_mod.get_chunk_store.cache_clear()


# ---------------------------------------------------------------------------
# Fake supabase-py client. Just enough surface to satisfy
# `_client.table(name).upsert(rows, on_conflict=..., ignore_duplicates=True).execute()`
# and `_client.table(name).delete().eq(...).in_(...).execute()`.
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, data: list[dict[str, Any]]) -> None:
        self.data = data


class _Fake413(Exception):
    """Mimics httpx-with-413 / supabase-py TOAST surface."""

    def __init__(self, msg: str = "row is too big: TOAST limit") -> None:
        super().__init__(msg)
        self.status_code = 413


class _Fake503(Exception):
    def __init__(self) -> None:
        super().__init__("upstream gateway error")
        self.status_code = 503


class _ChunksTable:
    """Per-table builder. Stores actual upsert payloads + supports
    eviction (delete) for cache tests."""

    def __init__(
        self,
        rows: list[dict[str, Any]],
        *,
        fault_per_call: list[Exception | None] | None = None,
        max_payload_rows: int | None = None,
    ) -> None:
        self.rows = rows
        self._fault_per_call = list(fault_per_call) if fault_per_call else []
        self._max_payload_rows = max_payload_rows
        self._pending_op: str | None = None
        self._pending_payload: list[dict[str, Any]] | None = None
        self._delete_filter: dict[str, Any] = {}

    # ---- upsert/insert path -----------------------------------------
    def upsert(
        self,
        payload: list[dict[str, Any]],
        *,
        on_conflict: str | None = None,
        ignore_duplicates: bool = False,
    ) -> _ChunksTable:
        self._pending_op = "upsert"
        self._pending_payload = payload
        self._on_conflict = on_conflict
        self._ignore_duplicates = ignore_duplicates
        return self

    def insert(self, payload: list[dict[str, Any]]) -> _ChunksTable:
        self._pending_op = "insert"
        self._pending_payload = payload
        return self

    # ---- delete path ------------------------------------------------
    def delete(self) -> _ChunksTable:
        self._pending_op = "delete"
        return self

    def eq(self, col: str, val: Any) -> _ChunksTable:
        self._delete_filter[col] = ("eq", val)
        return self

    def in_(self, col: str, vals: list[Any]) -> _ChunksTable:
        self._delete_filter[col] = ("in", list(vals))
        return self

    def select(self, *_: Any) -> _ChunksTable:
        self._pending_op = "select"
        return self

    # ---- terminal --------------------------------------------------
    def execute(self) -> _FakeResp:
        op = self._pending_op
        if op in {"upsert", "insert"}:
            payload = self._pending_payload or []
            # Pop a pre-loaded fault for this call, if any.
            fault: Exception | None = None
            if self._fault_per_call:
                fault = self._fault_per_call.pop(0)
            # Size-driven fault: simulate "this batch is too big" until
            # the caller halves it below the threshold.
            if (
                fault is None
                and self._max_payload_rows is not None
                and len(payload) > self._max_payload_rows
            ):
                fault = _Fake413(f"row is too big (rows={len(payload)})")
            if fault is not None:
                raise fault
            # Idempotency: ON CONFLICT DO NOTHING — only return rows
            # whose (source_id, chunk_index) is new.
            inserted: list[dict[str, Any]] = []
            seen = {(r["source_id"], r["chunk_index"]) for r in self.rows}
            for r in payload:
                key = (r["source_id"], r["chunk_index"])
                if key in seen:
                    continue
                self.rows.append(r)
                seen.add(key)
                inserted.append(r)
            return _FakeResp(inserted)
        if op == "delete":
            url_filter = self._delete_filter.get("url_hash")
            idx_filter = self._delete_filter.get("chunk_index")
            assert url_filter is not None and idx_filter is not None
            uh = url_filter[1]
            idxs = set(idx_filter[1])
            self.rows[:] = [
                r
                for r in self.rows
                if not (r.get("url_hash") == uh and r.get("chunk_index") in idxs)
            ]
            return _FakeResp([])
        if op == "select":
            return _FakeResp(list(self.rows))
        raise AssertionError(f"unhandled op {op}")


class _FakeClient:
    """Routes `.table(name)` to a per-table fake."""

    def __init__(self, tables: dict[str, _ChunksTable]) -> None:
        self._tables = tables

    def table(self, name: str) -> _ChunksTable:
        return self._tables[name]


def _make_store(
    *,
    chunks_rows: list[dict[str, Any]] | None = None,
    cache_rows: list[dict[str, Any]] | None = None,
    fault_per_call: list[Exception | None] | None = None,
    max_payload_rows: int | None = None,
) -> tuple[SessionStore, _ChunksTable, _ChunksTable]:
    chunks_tbl = _ChunksTable(
        chunks_rows if chunks_rows is not None else [],
        fault_per_call=fault_per_call,
        max_payload_rows=max_payload_rows,
    )
    cache_tbl = _ChunksTable(cache_rows if cache_rows is not None else [])
    client = _FakeClient(
        {
            SessionStore.CHUNKS_TABLE: chunks_tbl,
            SessionStore.CHUNK_EMBEDDING_CACHE_TABLE: cache_tbl,
        }
    )
    store = SessionStore.__new__(SessionStore)  # bypass real client init
    store._client = client  # type: ignore[attr-defined]
    return store, chunks_tbl, cache_tbl


def _good_chunk(idx: int) -> tuple[int, str, list[float]]:
    return (idx, f"chunk text {idx}", [0.0] * SessionStore.EMBED_DIM)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_insert_chunks_idempotent_on_re_run() -> None:
    """Re-running the same source twice → exactly one row per
    (session_id, source_id, chunk_idx) triplet. ON CONFLICT DO NOTHING
    is the contract."""
    sid = uuid4()
    src = uuid4()
    rows = [_good_chunk(i) for i in range(5)]
    store, chunks_tbl, _ = _make_store()

    n1 = await store.insert_chunks(sid, src, rows)
    n2 = await store.insert_chunks(sid, src, rows)

    assert n1 == 5
    assert n2 == 5  # second call reports "all rows durable" (ON CONFLICT skipped, but counted)
    # The actual physical row count is the unique-key count.
    keys = {(r["source_id"], r["chunk_index"]) for r in chunks_tbl.rows}
    assert len(keys) == 5
    assert len(chunks_tbl.rows) == 5


@pytest.mark.asyncio
async def test_insert_chunks_retries_on_toast_limit_with_halved_batches() -> None:
    """A TOAST-sized payload (mocked via max_payload_rows) is recovered
    by halving twice. Final outcome: all rows inserted; no exception
    surfaces. The shedding loop is the FM-1 successor."""
    sid = uuid4()
    src = uuid4()
    rows = [_good_chunk(i) for i in range(8)]
    # Simulate "batches > 4 rows are too big" — initial 8-row batch
    # halves to 4+4 (each fits), so two retries succeed.
    store, chunks_tbl, _ = _make_store(max_payload_rows=4)

    n = await store.insert_chunks(sid, src, rows)
    assert n == 8
    assert len(chunks_tbl.rows) == 8


@pytest.mark.asyncio
async def test_insert_chunks_retries_on_supabase_5xx() -> None:
    """One transient 503 → retry with halved batches → success."""
    sid = uuid4()
    src = uuid4()
    rows = [_good_chunk(i) for i in range(4)]
    # First call raises 503; halved sub-batches succeed.
    store, chunks_tbl, _ = _make_store(fault_per_call=[_Fake503(), None, None])

    n = await store.insert_chunks(sid, src, rows)
    assert n == 4
    assert len(chunks_tbl.rows) == 4


@pytest.mark.asyncio
async def test_insert_chunks_surfaces_after_max_halvings() -> None:
    """If shedding doesn't help (every retry still 503), the exception
    surfaces so the audit row records the real error_kind. We don't
    swallow it into a partial-success."""
    sid = uuid4()
    src = uuid4()
    rows = [_good_chunk(i) for i in range(4)]
    # Initial + 2 halvings, 2 sub-batches each = up to 7 calls; force-fault all.
    store, _, _ = _make_store(fault_per_call=[_Fake503()] * 8)

    with pytest.raises(_Fake503):
        await store.insert_chunks(sid, src, rows)


@pytest.mark.asyncio
async def test_insert_chunks_rejects_empty_text_preflight() -> None:
    """Pre-flight ChunkValidationError fires before any DB round-trip."""
    sid = uuid4()
    src = uuid4()
    bad = [(0, "", [0.0] * SessionStore.EMBED_DIM)]
    store, chunks_tbl, _ = _make_store()

    with pytest.raises(ChunkValidationError) as exc:
        await store.insert_chunks(sid, src, bad)
    assert exc.value.kind == "empty_text"
    # No write attempted.
    assert chunks_tbl.rows == []


@pytest.mark.asyncio
async def test_insert_chunks_rejects_dim_mismatch_preflight() -> None:
    sid = uuid4()
    src = uuid4()
    bad = [(0, "text", [0.0] * 768)]  # wrong dim
    store, chunks_tbl, _ = _make_store()

    with pytest.raises(ChunkValidationError) as exc:
        await store.insert_chunks(sid, src, bad)
    assert exc.value.kind == "dim_mismatch"
    assert chunks_tbl.rows == []


@pytest.mark.asyncio
async def test_get_chunk_cache_evicts_wrong_dim_rows() -> None:
    """FM-2 — a poisoned cache row (dim 768 instead of 1536) is dropped
    from the lookup result AND deleted from the cache table so the
    pipeline re-embeds. Idempotency promise: next call returns nothing
    for that index until a re-embed re-populates."""
    cache_rows = [
        {
            "url_hash": "abc",
            "chunk_index": 0,
            "embedding": [0.0] * 768,  # poisoned
        },
        {
            "url_hash": "abc",
            "chunk_index": 1,
            "embedding": [0.0] * SessionStore.EMBED_DIM,
        },
    ]
    store, _, cache_tbl = _make_store(cache_rows=cache_rows)

    out = await store.get_chunk_cache("abc", [0, 1])
    # Index 0 was poisoned → not returned.
    assert 0 not in out
    assert 1 in out
    assert len(out[1]) == SessionStore.EMBED_DIM
    # And it's been evicted (next call would miss too).
    remaining_indices = {r["chunk_index"] for r in cache_tbl.rows if r["url_hash"] == "abc"}
    assert 0 not in remaining_indices
    assert 1 in remaining_indices


def test_event_loop_isolation() -> None:
    """Sanity — pytest_asyncio fixture isn't doing anything weird."""
    assert asyncio.iscoroutinefunction(SessionStore.insert_chunks)
