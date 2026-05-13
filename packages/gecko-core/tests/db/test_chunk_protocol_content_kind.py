"""Trading-oracle Task 3 — verify protocol / content_kind / is_stale axes
land on inserted chunk docs.

Per Pattern A: defaults preserve existing behavior (protocol=[],
content_kind='unknown', is_stale=False); explicit values land verbatim.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from gecko_core.db import mongo_chunks


def _make_coll() -> MagicMock:
    coll = MagicMock()
    result = MagicMock()
    result.inserted_ids = ["x"]
    coll.insert_many = AsyncMock(return_value=result)
    return coll


@pytest.fixture
def patched(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"coll": _make_coll()}
    monkeypatch.setattr(mongo_chunks, "chunks_collection", lambda: state["coll"])

    async def _identity_pioneer(
        docs: list[dict[str, Any]], *_a: Any, **_k: Any
    ) -> list[dict[str, Any]]:
        return docs

    monkeypatch.setattr(
        "gecko_core.knowledge.pioneer.mark_pioneer_chunks",
        _identity_pioneer,
    )
    return state


async def test_protocol_defaults_to_empty(patched: dict[str, Any]) -> None:
    chunks = [
        (
            0,
            ("hello world placeholder text long enough to pass the S29 chunk-quality gate: " * 5),
            [0.1] * mongo_chunks.EMBED_DIM,
        )
    ]
    await mongo_chunks.insert_chunks_mongo(
        session_id=uuid4(),
        source_id=uuid4(),
        chunks=chunks,
        category="market_intelligence",
        vertical="dex",
        source="web",
    )
    docs = patched["coll"].insert_many.await_args.args[0]
    assert docs[0]["protocol"] == []
    assert docs[0]["content_kind"] == "unknown"
    assert docs[0]["is_stale"] is False


async def test_protocol_explicit_lands(patched: dict[str, Any]) -> None:
    chunks = [
        (
            0,
            ("jup tvl row placeholder text long enough to pass the S29 chunk-quality gate: " * 5),
            [0.1] * mongo_chunks.EMBED_DIM,
        )
    ]
    await mongo_chunks.insert_chunks_mongo(
        session_id=uuid4(),
        source_id=uuid4(),
        chunks=chunks,
        category="market_intelligence",
        vertical="dex",
        source="web",
        protocol=["jupiter", "kamino"],
    )
    docs = patched["coll"].insert_many.await_args.args[0]
    assert docs[0]["protocol"] == ["jupiter", "kamino"]


async def test_content_kind_explicit_lands_with_is_stale(patched: dict[str, Any]) -> None:
    chunks = [
        (
            0,
            ("snapshot row placeholder text long enough to pass the S29 chunk-quality gate: " * 5),
            [0.1] * mongo_chunks.EMBED_DIM,
        )
    ]
    await mongo_chunks.insert_chunks_mongo(
        session_id=uuid4(),
        source_id=uuid4(),
        chunks=chunks,
        category="market_intelligence",
        vertical="dex",
        source="paysh_live",
        content_kind="quote",
        is_stale=True,
    )
    docs = patched["coll"].insert_many.await_args.args[0]
    assert docs[0]["content_kind"] == "quote"
    assert docs[0]["is_stale"] is True
