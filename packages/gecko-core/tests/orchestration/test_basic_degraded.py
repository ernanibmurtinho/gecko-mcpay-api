"""S20-OBS-01 — basic.generate wiring of DegradedSectionTracker.

Asserts that:
  1. A clean run produces an empty ``degraded_sections`` and does NOT emit
     the ``synth.degraded_sections.summary`` INFO log.
  2. A run where a tracker is passed in (and pre-marked) propagates the
     marked sections + reasons onto the ResearchResult and emits the
     structured INFO log.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest


def _mk_chunk(idx: int, chunk_id: str, url: str = "https://example.com/a") -> Any:
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


def _llm_payload() -> str:
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
async def test_clean_run_has_empty_degraded_and_no_summary_log(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Happy path: no marks → empty fields, no INFO log fired."""
    from gecko_core.orchestration import basic as basic_mod

    chunks = [_mk_chunk(0, "chunk-AAA")]

    async def _fake_rag_query(*_a: object, **_kw: object) -> list[Any]:
        return chunks

    async def _fake_call_llm(**_kw: Any) -> tuple[str, float]:
        return _llm_payload(), 0.0001

    monkeypatch.setattr(basic_mod, "rag_query", _fake_rag_query)
    monkeypatch.setattr(basic_mod, "_call_llm", _fake_call_llm)

    store = _FakeStore(["https://example.com/a"])
    caplog.set_level(logging.INFO, logger=basic_mod.__name__)

    result = await basic_mod.generate(
        session_id=uuid4(),
        idea="test idea",
        store=store,  # type: ignore[arg-type]
        openai_client=AsyncMock(),
    )

    assert result.degraded_sections == []
    assert result.degraded_reasons == {}
    # The structured summary log MUST NOT fire on clean runs.
    summary_records = [
        r for r in caplog.records if r.getMessage() == "synth.degraded_sections.summary"
    ]
    assert summary_records == []


@pytest.mark.asyncio
async def test_marked_tracker_propagates_to_result_and_logs_summary(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Caller pre-marks a tracker → ResearchResult carries the entry + INFO log fires."""
    from gecko_core.orchestration import basic as basic_mod
    from gecko_core.orchestration.degraded import DegradedSectionTracker

    chunks = [_mk_chunk(0, "chunk-AAA")]

    async def _fake_rag_query(*_a: object, **_kw: object) -> list[Any]:
        return chunks

    async def _fake_call_llm(**_kw: Any) -> tuple[str, float]:
        return _llm_payload(), 0.0001

    monkeypatch.setattr(basic_mod, "rag_query", _fake_rag_query)
    monkeypatch.setattr(basic_mod, "_call_llm", _fake_call_llm)

    store = _FakeStore(["https://example.com/a"])
    tracker = DegradedSectionTracker()
    tracker.mark("market_landscape", "validation_dropped_all")
    caplog.set_level(logging.INFO, logger=basic_mod.__name__)

    result = await basic_mod.generate(
        session_id=uuid4(),
        idea="test idea",
        store=store,  # type: ignore[arg-type]
        openai_client=AsyncMock(),
        tracker=tracker,
    )

    assert result.degraded_sections == ["market_landscape"]
    assert result.degraded_reasons == {"market_landscape": "validation_dropped_all"}

    summary_records = [
        r for r in caplog.records if r.getMessage() == "synth.degraded_sections.summary"
    ]
    assert len(summary_records) == 1
    rec = summary_records[0]
    assert getattr(rec, "degraded_sections", None) == ["market_landscape"]
    assert getattr(rec, "degraded_reasons", None) == {"market_landscape": "validation_dropped_all"}
