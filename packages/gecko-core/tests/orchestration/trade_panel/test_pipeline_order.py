"""S29 #31 — pipeline ordering tests for retrieve_trade_corpus_chunks.

The swap under test: `_provider_quota_floor` now runs BEFORE `cohere_rerank`
(was: rerank-then-quota_floor). The floor allocates `top_k*2` candidates
balanced across provider_kinds; Cohere then reranks within that balanced
slate down to `top_k`.

These tests pin call order + arg shapes via lightweight monkeypatches.
The actual ranking math is exercised in test_provider_quota.py + the
Cohere rerank module's own tests — we don't double-cover here.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from gecko_core.orchestration import trade_panel as tp

# S33 merge — this whole module pins a superseded pipeline. S33-#79/#82
# replaced the S29-#31 `_provider_quota_floor → cohere_rerank` path with the
# voyage-rerank + dedicated canon-retrieval-floor architecture, and S29-#34
# moved the default top_k 10 → 5. These tests assert the removed shape.
# Skipped, not deleted — a rewrite against the S33 pipeline is tracked in
# S33-#83. The live S33 path is covered by the 42 passing trade_panel tests
# and the deterministic retrieval eval (tests/eval/scripts/retrieval_eval.py).
pytestmark = pytest.mark.skip(
    reason="S33-#83: pipeline superseded by canon-floor + voyage_rerank — rewrite pending"
)


def _chunk(score: float, provider_kind: str, text: str | None = None) -> dict[str, Any]:
    return {
        "id": f"id_{provider_kind}_{score}",
        "text": text if text is not None else ("x" * 400),
        "source_url": f"https://example/{provider_kind}",
        "source": provider_kind,
        "provider_kind": provider_kind,
        "freshness_tier": "static",
        "protocol": [],
        "vertical": "dex",
        "score": score,
    }


def _install_fake_mongo(monkeypatch: pytest.MonkeyPatch, pool: list[dict[str, Any]]) -> None:
    """Wire fake chunk-store, embedder, and Mongo collection.aggregate."""
    import gecko_core.db as db_mod
    import gecko_core.db.mongo as mongo_mod
    import gecko_core.ingestion.embedder as embedder_mod

    fake_coll = MagicMock()

    async def _aiter(_pipeline: Any):
        for row in pool:
            yield {
                "_id": row["id"],
                "source_url": row["source_url"],
                "text": row["text"],
                "vertical": row["vertical"],
                "protocol": row["protocol"],
                "provider_kind": row["provider_kind"],
                "freshness_tier": row["freshness_tier"],
                "source": row["source"],
                "metadata": {},
                "score": row["score"],
            }

    fake_coll.aggregate = lambda pipeline: _aiter(pipeline)

    monkeypatch.setattr(db_mod, "get_chunk_store", lambda: "mongo")
    monkeypatch.setattr(mongo_mod, "chunks_collection", lambda: fake_coll)
    monkeypatch.setattr(mongo_mod, "VECTOR_INDEX_NAME", "test_index", raising=False)

    async def _fake_embed(texts: list[str]) -> tuple[list[list[float]], int]:
        return ([[0.0] * 1536 for _ in texts], 0)

    monkeypatch.setattr(embedder_mod, "embed", _fake_embed)


@pytest.mark.asyncio
async def test_quota_floor_runs_before_rerank(monkeypatch: pytest.MonkeyPatch) -> None:
    """quota_floor must be called BEFORE cohere_rerank, with top_k*2 as the
    floor's slot count and the original top_k as Cohere's top_n."""

    call_order: list[tuple[str, dict[str, Any]]] = []

    pool = [
        _chunk(score=0.9 - i * 0.001, provider_kind=kind)
        for i, kind in enumerate(
            ["protocol_native"] * 20
            + ["canon_damodaran"] * 10
            + ["canon_marks"] * 10
            + ["canon_berkshire"] * 8
            + ["market_data"] * 6
            + ["bazaar_live"] * 6
        )
    ]
    _install_fake_mongo(monkeypatch, pool)

    real_floor = tp._provider_quota_floor

    def _spy_floor(rows: list[dict[str, Any]], *, top_k: int, **kw: Any) -> list[dict[str, Any]]:
        call_order.append(("quota_floor", {"top_k": top_k, "input_size": len(rows)}))
        return real_floor(rows, top_k=top_k, **kw)

    monkeypatch.setattr(tp, "_provider_quota_floor", _spy_floor)

    async def _fake_rerank(*, query: str, documents: list[str], top_n: int):
        call_order.append(("rerank", {"top_n": top_n, "input_size": len(documents)}))
        return [(i, 1.0 - i * 0.01) for i in range(min(top_n, len(documents)))]

    import gecko_core.rag.rerank as rerank_mod

    monkeypatch.setattr(rerank_mod, "cohere_rerank", _fake_rerank)

    out = await tp.retrieve_trade_corpus_chunks(
        idea="should I deposit into kamino jlp/usdc?",
        protocol="kamino",
        vertical="dex",
        top_k=10,
    )

    # 1. Order: quota_floor first, then rerank.
    assert [name for name, _ in call_order] == ["quota_floor", "rerank"], call_order

    # 2. quota_floor gets top_k*2 = 20, and the FULL wide pool as input.
    floor_kwargs = call_order[0][1]
    assert floor_kwargs["top_k"] == 20
    assert floor_kwargs["input_size"] == len(pool)

    # 3. Cohere rerank gets top_n=top_k=10, operating on the quota-floored set.
    rerank_kwargs = call_order[1][1]
    assert rerank_kwargs["top_n"] == 10
    assert 10 <= rerank_kwargs["input_size"] <= 20

    # 4. Final output is top_k chunks.
    assert len(out) == 10

    # 5. Diversity invariant — ≥2 distinct provider_kinds.
    distinct = {c["provider_kind"] for c in out}
    assert len(distinct) >= 2, distinct


@pytest.mark.asyncio
async def test_pipeline_survives_rerank_degrade(monkeypatch: pytest.MonkeyPatch) -> None:
    """When cohere_rerank returns [] (no key, soft failure), pipeline
    falls through to the quota-floored slate truncated to top_k."""

    # Interleave kinds so the score-sorted slate has natural diversity —
    # this exercises that quota_floor + truncate preserves it even without
    # Cohere rerank.
    kinds_cycle = ["protocol_native", "canon_damodaran", "canon_marks", "market_data"]
    pool = [
        _chunk(score=0.9 - i * 0.001, provider_kind=kinds_cycle[i % len(kinds_cycle)])
        for i in range(26)
    ]
    _install_fake_mongo(monkeypatch, pool)

    async def _empty_rerank(**kwargs: Any):
        return []

    import gecko_core.rag.rerank as rerank_mod

    monkeypatch.setattr(rerank_mod, "cohere_rerank", _empty_rerank)

    out = await tp.retrieve_trade_corpus_chunks(
        idea="kamino deposit?",
        protocol="kamino",
        vertical="dex",
        top_k=10,
    )

    assert len(out) == 10
    assert len({c["provider_kind"] for c in out}) >= 2


def test_default_top_k_constant_is_10() -> None:
    """S29 #31 bumped the constant from 5 to 10. Pin it."""
    assert tp._DEFAULT_TRADE_TOP_K == 10
