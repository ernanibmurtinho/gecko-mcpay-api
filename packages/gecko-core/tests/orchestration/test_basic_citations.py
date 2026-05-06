"""S20-C-CITATION-CONTRACT-01 — basic-tier end-to-end citation propagation.

Mocks the LLM seam (``_call_llm``) plus ``rag_query`` and the session
store so we can assert that:

  (a) a model emitting a top-level ``citations`` list lands as
      ``ResearchResult.cited_doc_ids`` + ``ResearchResult.citation_markers``,
      and
  (b) the rendered context block (``_format_context``) carries the
      chunk_id so the model has something to copy into ``doc_id``.

No real network, no real Supabase. The fake store implements only the
methods generate() actually calls.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest


def _mk_chunk(idx: int, chunk_id: str, url: str = "https://example.com/a") -> Any:
    """Build a RagChunk-validated object with chunk_id populated."""
    from gecko_core.rag.query import RagChunk

    return RagChunk(
        source_id=uuid4(),
        source_url=url,
        chunk_index=idx,
        text=f"context blob {idx}",
        similarity=0.85,
        provider_kind="web",
        chunk_id=chunk_id,
    )


class _FakeStore:
    """Minimal SessionStore stand-in — only the methods generate() calls."""

    def __init__(self, urls: list[str]) -> None:
        from gecko_core.models import SourceInfo

        self._sources = [
            SourceInfo(
                url=u,
                type="web",
                chunk_count=1,
                indexed_at=datetime.now(UTC),
            )
            for u in urls
        ]

    async def list_sources(self, _session_id: Any) -> list[Any]:
        return self._sources

    async def add_cost(self, *_a: Any, **_k: Any) -> None:
        return None


def _llm_payload_with_citations() -> str:
    """A minimally-valid _LLMOutput JSON with a top-level citations list."""
    return json.dumps(
        {
            "business_plan": {
                "problem": "p",
                "icp": "i",
                "solution": "s",
                "market": "m",
                "business_model": "bm",
                "channels": "c",
                "risks": [],
                "citations": [
                    {
                        "source_url": "https://example.com/a",
                        "chunk_index": 0,
                        "similarity": 0.85,
                    }
                ],
            },
            "validation_report": {
                "market_size_signal": "x",
                "competitor_analysis": "x",
                "demand_evidence": "x",
                "risk_flags": [],
                "citations": [
                    {
                        "source_url": "https://example.com/a",
                        "chunk_index": 0,
                        "similarity": 0.85,
                    }
                ],
                "gap_classification": "Partial:UX",
                "gap_summary": "competitor X covers fraud but not replay debugging",
                "gap_explanation": (
                    "The UX is the gap — chunk one [1] confirms the wedge. "
                    "Ship the replay-from-error path before competing on dashboards."
                ),
            },
            "prd": {
                "v1_scope": ["x"],
                "v2_scope": ["x"],
                "v3_scope": ["x"],
                "acceptance_criteria": ["x"],
                "non_functional": ["x"],
                "success_metrics": ["x"],
                "citations": [
                    {
                        "source_url": "https://example.com/a",
                        "chunk_index": 0,
                        "similarity": 0.85,
                    }
                ],
            },
            "citations": [
                {
                    "idx": 1,
                    "doc_id": "chunk-AAA",
                    "url": "https://example.com/a",
                    "span": "the cited passage",
                }
            ],
        }
    )


@pytest.mark.asyncio
async def test_basic_generate_propagates_cited_doc_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: mocked LLM emits citations → ResearchResult carries them."""
    from gecko_core.orchestration import basic as basic_mod

    chunks = [_mk_chunk(0, "chunk-AAA")]

    async def _fake_rag_query(*_a: object, **_kw: object) -> list[Any]:
        return chunks

    async def _fake_call_llm(**_kw: Any) -> tuple[str, float]:
        return _llm_payload_with_citations(), 0.0001

    monkeypatch.setattr(basic_mod, "rag_query", _fake_rag_query)
    monkeypatch.setattr(basic_mod, "_call_llm", _fake_call_llm)

    store = _FakeStore(["https://example.com/a"])
    session_id = uuid4()

    result = await basic_mod.generate(
        session_id=session_id,
        idea="test idea",
        store=store,  # type: ignore[arg-type]
        openai_client=AsyncMock(),
    )

    assert result.cited_doc_ids == ["chunk-AAA"]
    assert len(result.citation_markers) == 1
    m = result.citation_markers[0]
    assert m.idx == 1
    assert m.doc_id == "chunk-AAA"
    assert m.url == "https://example.com/a"
    # Existing citation surface still works — validation_report.citations
    # carries the legacy footnote shape and is non-empty.
    assert len(result.validation_report.citations) >= 1


def test_format_context_includes_chunk_id() -> None:
    """The rendered context block must surface chunk_id so the model can copy it."""
    from gecko_core.orchestration.basic import _format_context

    chunks = [_mk_chunk(0, "chunk-AAA"), _mk_chunk(1, "chunk-BBB")]
    rendered = _format_context(chunks)

    # The legacy `[1]` marker + URL is preserved (renderer-compat).
    assert "[1]" in rendered
    assert "[2]" in rendered
    assert "https://example.com/a" in rendered
    # And the new chunk_id is exposed.
    assert "chunk-AAA" in rendered
    assert "chunk-BBB" in rendered
