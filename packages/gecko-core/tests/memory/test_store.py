"""Tests for the memory layer store + public API (S5-MEM-02)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from gecko_core import memory as memory_pkg
from gecko_core.memory import (
    MemoryEntry,
    MemoryEntryType,
    MemoryScope,
    recall,
    save,
    search,
)
from gecko_core.memory.store import MemoryStore


def _row(
    *,
    scope: MemoryScope,
    entry_type: MemoryEntryType,
    value: dict[str, Any],
    created_at: datetime | None = None,
    ttl_at: datetime | None = None,
    key: str | None = None,
    similarity: float | None = None,
) -> dict[str, Any]:
    return {
        "id": str(uuid4()),
        "scope_type": scope.type,
        "scope_id": scope.id,
        "entry_type": entry_type.value,
        "key": key,
        "value": value,
        "tx_signature": None,
        "created_at": (created_at or datetime.now(UTC)).isoformat(),
        "ttl_at": ttl_at.isoformat() if ttl_at else None,
        "similarity": similarity,
    }


class _FakeStore(MemoryStore):
    """In-memory MemoryStore — bypasses the real Supabase client."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.last_search_scope: MemoryScope | None = None
        self.last_inserted_embedding: list[float] | None = None

    async def insert(  # type: ignore[override]
        self,
        *,
        scope: MemoryScope,
        entry_type: MemoryEntryType,
        value: dict[str, Any],
        embedding: list[float] | None,
        key: str | None = None,
        tx_signature: str | None = None,
        ttl_at: datetime | None = None,
    ) -> UUID:
        new_id = uuid4()
        self.last_inserted_embedding = embedding
        self.rows.append(
            _row(
                scope=scope,
                entry_type=entry_type,
                value=value,
                ttl_at=ttl_at,
                key=key,
            )
            | {"id": str(new_id), "_embedding": embedding}
        )
        return new_id

    async def list_by_scope(  # type: ignore[override]
        self,
        *,
        scope: MemoryScope,
        entry_type: MemoryEntryType | None = None,
        key: str | None = None,
        limit: int = 20,
        since: datetime | None = None,
    ) -> list[MemoryEntry]:
        now = datetime.now(UTC)
        out: list[MemoryEntry] = []
        for r in self.rows:
            if r["scope_type"] != scope.type or r["scope_id"] != scope.id:
                continue
            if entry_type and r["entry_type"] != entry_type.value:
                continue
            if key is not None and r.get("key") != key:
                continue
            ttl = r.get("ttl_at")
            if ttl:
                ttl_at = datetime.fromisoformat(str(ttl).replace("Z", "+00:00"))
                if ttl_at <= now:
                    continue
            out.append(_to_entry(r))
        out.sort(key=lambda e: e.created_at, reverse=True)
        return out[:limit]

    async def search(  # type: ignore[override]
        self,
        *,
        scope: MemoryScope,
        query_embedding: list[float],
        top_k: int = 5,
        similarity_threshold: float = 0.6,
    ) -> list[tuple[MemoryEntry, float]]:
        self.last_search_scope = scope
        out: list[tuple[MemoryEntry, float]] = []
        for r in self.rows:
            if r["scope_type"] != scope.type or r["scope_id"] != scope.id:
                continue
            sim = float(r.get("similarity") or 0.9)
            if sim < similarity_threshold:
                continue
            out.append((_to_entry(r), sim))
        return out[:top_k]


def _to_entry(r: dict[str, Any]) -> MemoryEntry:
    created = r["created_at"]
    if isinstance(created, str):
        created_at = datetime.fromisoformat(created.replace("Z", "+00:00"))
    else:
        created_at = created
    return MemoryEntry(
        id=UUID(str(r["id"])),
        scope=MemoryScope(type=r["scope_type"], id=r["scope_id"]),
        entry_type=MemoryEntryType(r["entry_type"]),
        key=r.get("key"),
        value=r["value"],
        tx_signature=r.get("tx_signature"),
        created_at=created_at,
    )


@pytest.fixture
def fake_store() -> _FakeStore:
    return _FakeStore()


@pytest.fixture
def fake_embed(monkeypatch: pytest.MonkeyPatch) -> list[list[float]]:
    """Patch the embedder so save/search produce a deterministic 1536-vec."""
    captured: list[list[float]] = []

    async def _fake_embed_text(text: str) -> list[float]:
        vec = [0.001] * 1536
        captured.append(vec)
        return vec

    monkeypatch.setattr(memory_pkg, "embed_text", _fake_embed_text)
    # contradictions module also imports embed_text from memory.embedder
    from gecko_core.memory import embedder as memory_embedder

    monkeypatch.setattr(memory_embedder, "embed_text", _fake_embed_text)
    return captured


async def test_save_recall_search_round_trip(
    fake_store: _FakeStore, fake_embed: list[list[float]]
) -> None:
    scope = MemoryScope(type="project", id="proj-A")
    new_id = await save(
        scope,
        MemoryEntryType.user_note,
        {"text": "remember the LOI"},
        store=fake_store,
    )
    assert isinstance(new_id, UUID)
    assert fake_store.last_inserted_embedding is not None
    assert len(fake_store.last_inserted_embedding) == 1536

    entries = await recall(scope, store=fake_store)
    assert len(entries) == 1
    assert entries[0].value == {"text": "remember the LOI"}

    matches = await search(scope, "loi", store=fake_store)
    assert matches
    entry, sim = matches[0]
    assert entry.value == {"text": "remember the LOI"}
    assert sim >= 0.0


async def test_ttl_expiration_filters_recall(
    fake_store: _FakeStore, fake_embed: list[list[float]]
) -> None:
    scope = MemoryScope(type="project", id="proj-A")
    past = datetime.now(UTC) - timedelta(hours=1)
    await save(
        scope,
        MemoryEntryType.user_note,
        {"x": 1},
        store=fake_store,
        ttl_at=past,
    )
    # Live entry (no TTL).
    await save(scope, MemoryEntryType.user_note, {"x": 2}, store=fake_store)

    rows = await recall(scope, store=fake_store)
    assert len(rows) == 1
    assert rows[0].value == {"x": 2}


async def test_scope_isolation(fake_store: _FakeStore, fake_embed: list[list[float]]) -> None:
    scope_a = MemoryScope(type="project", id="proj-A")
    scope_b = MemoryScope(type="project", id="proj-B")
    await save(scope_a, MemoryEntryType.user_note, {"a": True}, store=fake_store)
    await save(scope_b, MemoryEntryType.user_note, {"b": True}, store=fake_store)

    only_a = await recall(scope_a, store=fake_store)
    assert len(only_a) == 1
    assert only_a[0].value == {"a": True}

    matches_a = await search(scope_a, "anything", store=fake_store)
    assert all(e.scope.id == "proj-A" for e, _ in matches_a)


async def test_embedding_shape_is_1536(
    fake_store: _FakeStore, fake_embed: list[list[float]]
) -> None:
    scope = MemoryScope(type="session", id="sess-1")
    await save(scope, MemoryEntryType.verdict_received, {"idea": "x"}, store=fake_store)
    assert fake_store.last_inserted_embedding is not None
    assert len(fake_store.last_inserted_embedding) == 1536
    assert all(isinstance(v, float) for v in fake_store.last_inserted_embedding)
