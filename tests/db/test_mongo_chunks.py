"""Tests for the Mongo chunk write paths (S18-MONGO-WRITE-01)."""

from __future__ import annotations

from typing import Any, ClassVar
from uuid import uuid4

import pytest
from gecko_core.db import mongo as mongo_mod
from gecko_core.db import mongo_chunks

# ---------------------------------------------------------------------------
# Fakes — async collection stubs that record calls
# ---------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self._docs = list(docs)

    def __aiter__(self) -> _FakeCursor:
        return self

    async def __anext__(self) -> dict[str, Any]:
        if not self._docs:
            raise StopAsyncIteration
        return self._docs.pop(0)


class _InsertManyResult:
    def __init__(self, ids: list[Any]) -> None:
        self.inserted_ids = ids


class _FakeChunksCollection:
    def __init__(self) -> None:
        self.docs: list[dict[str, Any]] = []
        self.dup_keys_to_raise: int = 0
        self.fatal_to_raise: bool = False

    async def insert_many(
        self, docs: list[dict[str, Any]], ordered: bool = True
    ) -> _InsertManyResult:
        if self.fatal_to_raise:

            class _BoomError(Exception):
                details: ClassVar[dict[str, Any]] = {
                    "nInserted": 0,
                    "writeErrors": [{"code": 9999, "errmsg": "fatal"}],
                }

            raise _BoomError()
        if self.dup_keys_to_raise > 0:

            class _DupBulkWriteError(Exception):
                details: ClassVar[dict[str, Any]] = {
                    "nInserted": len(docs) - self.dup_keys_to_raise,
                    "writeErrors": [{"code": 11000} for _ in range(self.dup_keys_to_raise)],
                }

            self.docs.extend(docs[: len(docs) - self.dup_keys_to_raise])
            raise _DupBulkWriteError()
        self.docs.extend(docs)
        return _InsertManyResult([f"id-{i}" for i in range(len(docs))])

    async def insert_one(self, doc: dict[str, Any]) -> None:
        self.docs.append(doc)

    def find(self, query: dict[str, Any], projection: dict[str, Any] | None = None) -> _FakeCursor:
        matches = [
            d
            for d in self.docs
            if all(
                (
                    d.get(k) in v.get("$in", [])
                    if isinstance(v, dict) and "$in" in v
                    else d.get(k) == v
                )
                for k, v in query.items()
            )
        ]
        return _FakeCursor(matches)

    async def delete_many(self, query: dict[str, Any]) -> Any:
        before = len(self.docs)
        self.docs = [
            d
            for d in self.docs
            if not all(
                (
                    d.get(k) in v.get("$in", [])
                    if isinstance(v, dict) and "$in" in v
                    else d.get(k) == v
                )
                for k, v in query.items()
            )
        ]
        self.deleted = before - len(self.docs)

        class _DeleteResult:
            deleted_count = self.deleted

        return _DeleteResult()

    async def count_documents(self, query: dict[str, Any]) -> int:
        def _matches(d: dict[str, Any]) -> bool:
            for k, v in query.items():
                # naive dotted-path support for "metadata.deprecated"
                if "." in k:
                    parts = k.split(".")
                    cur: Any = d
                    for p in parts:
                        if isinstance(cur, dict):
                            cur = cur.get(p)
                        else:
                            cur = None
                            break
                    actual = cur
                else:
                    actual = d.get(k)
                if isinstance(v, dict) and "$ne" in v:
                    if actual == v["$ne"]:
                        return False
                elif isinstance(v, dict) and "$in" in v:
                    if actual not in v["$in"]:
                        return False
                else:
                    if actual != v:
                        return False
            return True

        return sum(1 for d in self.docs if _matches(d))

    async def bulk_write(self, ops: list[Any], ordered: bool = True) -> None:
        for op in ops:
            doc = op._doc.get("$setOnInsert", {})
            key = (
                doc.get("url_hash"),
                doc.get("chunk_index"),
                doc.get("embed_model"),
            )
            if not any(
                (d.get("url_hash"), d.get("chunk_index"), d.get("embed_model")) == key
                for d in self.docs
            ):
                self.docs.append(doc)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_chunks_coll(monkeypatch: pytest.MonkeyPatch) -> _FakeChunksCollection:
    coll = _FakeChunksCollection()
    monkeypatch.setattr(mongo_chunks, "chunks_collection", lambda: coll)
    return coll


@pytest.fixture
def fake_audit_coll(monkeypatch: pytest.MonkeyPatch) -> _FakeChunksCollection:
    coll = _FakeChunksCollection()
    monkeypatch.setattr(mongo_chunks, "audit_collection", lambda: coll)
    return coll


@pytest.fixture
def fake_cache_coll(monkeypatch: pytest.MonkeyPatch) -> _FakeChunksCollection:
    coll = _FakeChunksCollection()
    monkeypatch.setattr(mongo_chunks, "cache_collection", lambda: coll)
    return coll


def _embed(value: float = 0.1) -> list[float]:
    return [value] * mongo_chunks.EMBED_DIM


# ---------------------------------------------------------------------------
# insert_chunks_mongo
# ---------------------------------------------------------------------------


class TestInsertChunksMongo:
    @pytest.mark.asyncio
    async def test_empty_short_circuits(self, fake_chunks_coll: _FakeChunksCollection) -> None:
        n = await mongo_chunks.insert_chunks_mongo(uuid4(), uuid4(), [])
        assert n == 0
        assert fake_chunks_coll.docs == []

    @pytest.mark.asyncio
    async def test_writes_doc_shape(self, fake_chunks_coll: _FakeChunksCollection) -> None:
        sid = uuid4()
        srcid = uuid4()
        n = await mongo_chunks.insert_chunks_mongo(
            sid,
            srcid,
            [(0, "first chunk", _embed(0.1)), (1, "second chunk", _embed(0.2))],
            provider_kind="bazaar",
        )
        assert n == 2
        assert len(fake_chunks_coll.docs) == 2
        d0 = fake_chunks_coll.docs[0]
        assert d0["session_id"] == str(sid)
        assert d0["source_id"] == str(srcid)
        assert d0["chunk_index"] == 0
        assert d0["text"] == "first chunk"
        assert d0["provider_kind"] == "bazaar"
        assert d0["project_id"] is None
        assert len(d0["embedding"]) == mongo_chunks.EMBED_DIM
        assert "captured_at" in d0

    @pytest.mark.asyncio
    async def test_rejects_empty_text(self, fake_chunks_coll: _FakeChunksCollection) -> None:
        from gecko_core.ingestion.exceptions import ChunkValidationError

        with pytest.raises(ChunkValidationError):
            await mongo_chunks.insert_chunks_mongo(uuid4(), uuid4(), [(0, "  ", _embed())])
        assert fake_chunks_coll.docs == []

    @pytest.mark.asyncio
    async def test_rejects_dim_mismatch(self, fake_chunks_coll: _FakeChunksCollection) -> None:
        from gecko_core.ingestion.exceptions import ChunkValidationError

        with pytest.raises(ChunkValidationError):
            await mongo_chunks.insert_chunks_mongo(uuid4(), uuid4(), [(0, "ok", [0.1])])

    @pytest.mark.asyncio
    async def test_unavailable_when_coll_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(mongo_chunks, "chunks_collection", lambda: None)
        with pytest.raises(mongo_chunks._MongoUnavailable):
            await mongo_chunks.insert_chunks_mongo(uuid4(), uuid4(), [(0, "ok", _embed())])

    @pytest.mark.asyncio
    async def test_dup_keys_treated_as_durable(
        self, fake_chunks_coll: _FakeChunksCollection
    ) -> None:
        fake_chunks_coll.dup_keys_to_raise = 1
        n = await mongo_chunks.insert_chunks_mongo(
            uuid4(), uuid4(), [(0, "a", _embed()), (1, "b", _embed())]
        )
        assert n == 2  # 1 inserted + 1 dup-skipped both counted as durable

    @pytest.mark.asyncio
    async def test_real_error_reraises(self, fake_chunks_coll: _FakeChunksCollection) -> None:
        fake_chunks_coll.fatal_to_raise = True
        with pytest.raises(Exception):
            await mongo_chunks.insert_chunks_mongo(uuid4(), uuid4(), [(0, "a", _embed())])


# ---------------------------------------------------------------------------
# S20-A2 — categorized-knowledge fields
# ---------------------------------------------------------------------------


class TestInsertChunksCategorizedFields:
    @pytest.mark.asyncio
    async def test_writes_categorized_fields_and_metadata(
        self, fake_chunks_coll: _FakeChunksCollection
    ) -> None:
        n = await mongo_chunks.insert_chunks_mongo(
            uuid4(),
            uuid4(),
            [(0, "a chunk", _embed(0.1))],
            category="business_financial",
            subcategory="regulatory",
            vertical="neobank",
            source="tavily",
            confidence=0.87,
        )
        assert n == 1
        d = fake_chunks_coll.docs[0]
        assert d["category"] == "business_financial"
        assert d["subcategory"] == "regulatory"
        assert d["vertical"] == "neobank"
        assert d["source"] == "tavily"
        meta = d["metadata"]
        assert meta["confidence"] == pytest.approx(0.87)
        assert meta["usage_count"] == 0
        # S20-A7 — empty cell at insert time → batch is pioneer.
        # The pioneer-false default seeded at metadata-build is flipped
        # by mark_pioneer_chunks before bulk insert.
        assert meta["pioneer"] is True
        assert "timestamp" in meta

    @pytest.mark.asyncio
    async def test_missing_category_raises(self, fake_chunks_coll: _FakeChunksCollection) -> None:
        from gecko_core.ingestion.exceptions import ChunkValidationError

        with pytest.raises(ChunkValidationError) as exc_info:
            await mongo_chunks.insert_chunks_mongo(
                uuid4(),
                uuid4(),
                [(0, "ok", _embed())],
                vertical="neobank",
                source="web",
            )
        assert "category" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_missing_vertical_raises(self, fake_chunks_coll: _FakeChunksCollection) -> None:
        from gecko_core.ingestion.exceptions import ChunkValidationError

        with pytest.raises(ChunkValidationError) as exc_info:
            await mongo_chunks.insert_chunks_mongo(
                uuid4(),
                uuid4(),
                [(0, "ok", _embed())],
                category="ai_ml",
                source="web",
            )
        assert "vertical" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_provider_kind_alias_to_source_emits_deprecation(
        self, fake_chunks_coll: _FakeChunksCollection
    ) -> None:
        """Legacy callers that pass only provider_kind still write a 'source' field."""
        import warnings

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            n = await mongo_chunks.insert_chunks_mongo(
                uuid4(),
                uuid4(),
                [(0, "ok", _embed())],
                provider_kind="bazaar",
            )
        assert n == 1
        d = fake_chunks_coll.docs[0]
        # source got the provider_kind value (bazaar is in KNOWLEDGE_SOURCES).
        assert d["source"] == "bazaar"
        assert d["provider_kind"] == "bazaar"
        # category / vertical defaulted with a deprecation warning.
        assert d["category"] == "market_intelligence"
        assert d["vertical"] == "unknown"
        assert any(issubclass(w.category, DeprecationWarning) for w in caught)

    @pytest.mark.asyncio
    async def test_invalid_category_value_raises(
        self, fake_chunks_coll: _FakeChunksCollection
    ) -> None:
        from gecko_core.ingestion.exceptions import ChunkValidationError

        with pytest.raises(ChunkValidationError) as exc_info:
            await mongo_chunks.insert_chunks_mongo(
                uuid4(),
                uuid4(),
                [(0, "ok", _embed())],
                category="not_a_real_category",  # type: ignore[arg-type]
                vertical="neobank",
                source="web",
            )
        assert "invalid category" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_legacy_uncategorized_rejected_without_deprecated(
        self, fake_chunks_coll: _FakeChunksCollection
    ) -> None:
        """S20-A3 — new ingestions cannot land in the legacy bucket."""
        from gecko_core.ingestion.exceptions import ChunkValidationError

        with pytest.raises(ChunkValidationError) as exc_info:
            await mongo_chunks.insert_chunks_mongo(
                uuid4(),
                uuid4(),
                [(0, "ok", _embed())],
                category="legacy_uncategorized",
                vertical="unknown",
                source="web",
                deprecated=False,
            )
        assert exc_info.value.kind == "invalid_category"
        assert "reserved for the A3 revoke script" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_legacy_uncategorized_allowed_with_deprecated(
        self, fake_chunks_coll: _FakeChunksCollection
    ) -> None:
        """S20-A3 — the revoke script's own write path succeeds."""
        n = await mongo_chunks.insert_chunks_mongo(
            uuid4(),
            uuid4(),
            [(0, "old chunk", _embed(0.1))],
            category="legacy_uncategorized",
            vertical="unknown",
            source="web",
            deprecated=True,
        )
        assert n == 1
        d = fake_chunks_coll.docs[0]
        assert d["category"] == "legacy_uncategorized"
        assert d["metadata"]["deprecated"] is True


# ---------------------------------------------------------------------------
# S20-A7 — pioneer-cell signal wired through insert_chunks_mongo
# ---------------------------------------------------------------------------


class TestInsertChunksPioneerSignal:
    @pytest.mark.asyncio
    async def test_sparse_cell_marks_chunks_pioneer_true(
        self, fake_chunks_coll: _FakeChunksCollection
    ) -> None:
        # 0 prior chunks in the cell → batch is pioneer.
        n = await mongo_chunks.insert_chunks_mongo(
            uuid4(),
            uuid4(),
            [(0, "a", _embed(0.1)), (1, "b", _embed(0.2))],
            category="business_financial",
            vertical="neobank",
            source="tavily",
        )
        assert n == 2
        for d in fake_chunks_coll.docs:
            assert d["metadata"]["pioneer"] is True

    @pytest.mark.asyncio
    async def test_dense_cell_leaves_chunks_pioneer_false(
        self, fake_chunks_coll: _FakeChunksCollection
    ) -> None:
        # Pre-populate 10 prior chunks in the (neobank, business_financial)
        # cell so the next insert sees a dense cell.
        for i in range(10):
            fake_chunks_coll.docs.append(
                {
                    "chunk_index": 1000 + i,
                    "vertical": "neobank",
                    "category": "business_financial",
                    "metadata": {"pioneer": False},
                }
            )
        n = await mongo_chunks.insert_chunks_mongo(
            uuid4(),
            uuid4(),
            [(0, "a", _embed(0.1))],
            category="business_financial",
            vertical="neobank",
            source="tavily",
        )
        assert n == 1
        # The newly inserted doc is the LAST one in fake_chunks_coll.docs.
        new_doc = fake_chunks_coll.docs[-1]
        assert new_doc["metadata"]["pioneer"] is False


# ---------------------------------------------------------------------------
# S33-#80 — delete_chunks_for_source_mongo (replace-before-insert)
# ---------------------------------------------------------------------------


class TestDeleteChunksForSource:
    @pytest.mark.asyncio
    async def test_deletes_only_matching_provider_url_protocol(
        self, fake_chunks_coll: _FakeChunksCollection
    ) -> None:
        # Two protocol_native chunks for the kamino-vaults endpoint, plus
        # one unrelated chunk that must survive.
        fake_chunks_coll.docs.extend(
            [
                {
                    "provider_kind": "protocol_native",
                    "source_url": "https://api.kamino.finance/kvaults/vaults",
                    "protocol": ["kamino"],
                    "text": "stale day 1",
                },
                {
                    "provider_kind": "protocol_native",
                    "source_url": "https://api.kamino.finance/kvaults/vaults",
                    "protocol": ["kamino"],
                    "text": "stale day 2",
                },
                {
                    "provider_kind": "protocol_native",
                    "source_url": "https://api.kamino.finance/strategies",
                    "protocol": ["kamino"],
                    "text": "different endpoint — keep",
                },
            ]
        )
        deleted = await mongo_chunks.delete_chunks_for_source_mongo(
            provider_kind="protocol_native",
            source_url="https://api.kamino.finance/kvaults/vaults",
            protocol="kamino",
        )
        assert deleted == 2
        assert len(fake_chunks_coll.docs) == 1
        assert fake_chunks_coll.docs[0]["text"] == "different endpoint — keep"

    @pytest.mark.asyncio
    async def test_no_match_deletes_nothing(self, fake_chunks_coll: _FakeChunksCollection) -> None:
        fake_chunks_coll.docs.append(
            {
                "provider_kind": "protocol_native",
                "source_url": "https://api.kamino.finance/strategies",
                "protocol": ["kamino"],
                "text": "x",
            }
        )
        deleted = await mongo_chunks.delete_chunks_for_source_mongo(
            provider_kind="protocol_native",
            source_url="https://example.com/nope",
            protocol="kamino",
        )
        assert deleted == 0
        assert len(fake_chunks_coll.docs) == 1

    @pytest.mark.asyncio
    async def test_unconfigured_returns_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(mongo_chunks, "chunks_collection", lambda: None)
        deleted = await mongo_chunks.delete_chunks_for_source_mongo(
            provider_kind="protocol_native",
            source_url="https://api.kamino.finance/kvaults/vaults",
        )
        assert deleted == 0


def test_compound_index_constant_exported() -> None:
    """The compound-index name is exported for tests / doctor probes."""
    assert mongo_chunks.CHUNKS_VERTICAL_CATEGORY_INDEX == "vertical_category_compound"


# ---------------------------------------------------------------------------
# insert_chunks_write_audit_mongo
# ---------------------------------------------------------------------------


class TestAuditMongo:
    @pytest.mark.asyncio
    async def test_writes_audit_doc(self, fake_audit_coll: _FakeChunksCollection) -> None:
        sid = uuid4()
        await mongo_chunks.insert_chunks_write_audit_mongo(
            session_id=sid,
            source_id=None,
            batch_size=10,
            succeeded=10,
            failed=0,
            error_kind="none",
            embed_model="text-embedding-3-small",
        )
        assert len(fake_audit_coll.docs) == 1
        d = fake_audit_coll.docs[0]
        assert d["session_id"] == str(sid)
        assert d["source_id"] is None
        assert d["error_kind"] == "none"
        assert d["embed_model"] == "text-embedding-3-small"
        assert "captured_at" in d

    @pytest.mark.asyncio
    async def test_audit_swallows_errors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _BoomColl:
            async def insert_one(self, doc: dict[str, Any]) -> None:
                raise RuntimeError("boom")

        monkeypatch.setattr(mongo_chunks, "audit_collection", lambda: _BoomColl())
        # Must not raise
        await mongo_chunks.insert_chunks_write_audit_mongo(
            session_id=uuid4(),
            source_id=None,
            batch_size=1,
            succeeded=1,
            failed=0,
            error_kind="none",
            embed_model=None,
        )

    @pytest.mark.asyncio
    async def test_audit_no_op_when_unconfigured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(mongo_chunks, "audit_collection", lambda: None)
        await mongo_chunks.insert_chunks_write_audit_mongo(
            session_id=uuid4(),
            source_id=None,
            batch_size=0,
            succeeded=0,
            failed=0,
            error_kind="none",
            embed_model=None,
        )


# ---------------------------------------------------------------------------
# chunk_embedding_cache
# ---------------------------------------------------------------------------


class TestCacheMongo:
    @pytest.mark.asyncio
    async def test_get_empty_indices(self, fake_cache_coll: _FakeChunksCollection) -> None:
        out = await mongo_chunks.get_chunk_cache_mongo("hash", [])
        assert out == {}

    @pytest.mark.asyncio
    async def test_get_unconfigured_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(mongo_chunks, "cache_collection", lambda: None)
        out = await mongo_chunks.get_chunk_cache_mongo("h", [0, 1])
        assert out == {}

    @pytest.mark.asyncio
    async def test_put_and_get_roundtrip(self, fake_cache_coll: _FakeChunksCollection) -> None:
        await mongo_chunks.put_chunk_cache_mongo(
            "hash-a",
            [(0, "t0", _embed(0.1)), (1, "t1", _embed(0.2))],
            embed_model="text-embedding-3-small",
        )
        out = await mongo_chunks.get_chunk_cache_mongo(
            "hash-a", [0, 1], embed_model="text-embedding-3-small"
        )
        assert set(out.keys()) == {0, 1}
        assert len(out[0]) == mongo_chunks.EMBED_DIM

    @pytest.mark.asyncio
    async def test_put_idempotent(self, fake_cache_coll: _FakeChunksCollection) -> None:
        rows = [(0, "t0", _embed(0.1))]
        await mongo_chunks.put_chunk_cache_mongo("h", rows, embed_model="m")
        await mongo_chunks.put_chunk_cache_mongo("h", rows, embed_model="m")
        assert len(fake_cache_coll.docs) == 1

    @pytest.mark.asyncio
    async def test_get_evicts_dim_mismatch(self, fake_cache_coll: _FakeChunksCollection) -> None:
        # Inject a poisoned row directly
        fake_cache_coll.docs.append(
            {"url_hash": "h", "chunk_index": 0, "embedding": [0.1, 0.2], "embed_model": "m"}
        )
        out = await mongo_chunks.get_chunk_cache_mongo("h", [0], embed_model="m")
        assert out == {}
        # Eviction removed the poisoned row
        assert fake_cache_coll.docs == []


# ---------------------------------------------------------------------------
# SessionStore dispatch — flag flip routes to Mongo
# ---------------------------------------------------------------------------


class TestSessionStoreDispatch:
    @pytest.mark.asyncio
    async def test_insert_chunks_routes_to_mongo(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake_chunks_coll: _FakeChunksCollection,
    ) -> None:
        from gecko_core.db import chunk_store as cs_mod

        monkeypatch.setenv("GECKO_CHUNK_STORE", "mongo")
        cs_mod.get_chunk_store.cache_clear()

        from gecko_core.sessions.store import SessionStore

        store = SessionStore.__new__(SessionStore)
        store._client = None  # type: ignore[attr-defined]
        n = await store.insert_chunks(uuid4(), uuid4(), [(0, "t", _embed())], provider_kind="web")
        assert n == 1
        assert len(fake_chunks_coll.docs) == 1
        cs_mod.get_chunk_store.cache_clear()

    @pytest.mark.asyncio
    async def test_audit_routes_to_mongo(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake_audit_coll: _FakeChunksCollection,
    ) -> None:
        from gecko_core.db import chunk_store as cs_mod

        monkeypatch.setenv("GECKO_CHUNK_STORE", "mongo")
        cs_mod.get_chunk_store.cache_clear()

        from gecko_core.sessions.store import SessionStore

        store = SessionStore.__new__(SessionStore)
        store._client = None  # type: ignore[attr-defined]
        await store.insert_chunks_write_audit(
            session_id=uuid4(),
            source_id=None,
            batch_size=1,
            succeeded=1,
            failed=0,
            error_kind="none",
            embed_model=None,
        )
        assert len(fake_audit_coll.docs) == 1
        cs_mod.get_chunk_store.cache_clear()


@pytest.fixture(autouse=True)
def _reset_chunk_store_cache() -> None:
    from gecko_core.db import chunk_store as cs_mod

    cs_mod.get_chunk_store.cache_clear()
    mongo_mod._client.cache_clear()
    yield
    cs_mod.get_chunk_store.cache_clear()
    mongo_mod._client.cache_clear()
