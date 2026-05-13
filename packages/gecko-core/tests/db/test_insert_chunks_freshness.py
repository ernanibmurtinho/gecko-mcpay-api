"""Trading-oracle Task 2 — verify ``freshness_tier`` lands on inserted docs.

Per Pattern A: the kwarg defaults to ``"static"`` (preserves existing
behavior); explicit values land verbatim on the chunk document.
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

    # mark_pioneer_chunks is awaited; return docs unchanged so we can inspect them.
    async def _identity_pioneer(
        docs: list[dict[str, Any]], *_a: Any, **_k: Any
    ) -> list[dict[str, Any]]:
        return docs

    monkeypatch.setattr(
        "gecko_core.knowledge.pioneer.mark_pioneer_chunks",
        _identity_pioneer,
    )
    return state


async def test_freshness_tier_defaults_to_static(patched: dict[str, Any]) -> None:
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
    assert docs[0]["freshness_tier"] == "static"


async def test_freshness_tier_explicit_value_lands(patched: dict[str, Any]) -> None:
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
        freshness_tier="daily",
    )
    docs = patched["coll"].insert_many.await_args.args[0]
    assert docs[0]["freshness_tier"] == "daily"
