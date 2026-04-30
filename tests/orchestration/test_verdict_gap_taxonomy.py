"""S9-VERDICT-01 — structured gap_classification on ValidationReport.

Asserts:
  - Pydantic accepts every canonical taxonomy value and rejects off-taxonomy.
  - The basic synthesizer retries once when the LLM omits / mis-types
    `gap_classification`, then falls back to GAP_CLASSIFICATION_FALLBACK with
    a logged warning if the retry still fails.
  - The CLI render surfaces the bold gap line under both the validation
    panel AND the PRD panel (one line each, taxonomy + summary).
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from rich.console import Console

# --- Pydantic model ---------------------------------------------------------


def test_validation_report_accepts_full_taxonomy() -> None:
    from gecko_core.models import ValidationReport

    for label in (
        "Full",
        "Partial:segment",
        "Partial:UX",
        "Partial:geo",
        "Partial:pricing",
        "Partial:integration",
        "False",
    ):
        v = ValidationReport(
            market_size_signal="m",
            competitor_analysis="c",
            demand_evidence="d",
            risk_flags=[],
            citations=[],
            gap_classification=label,  # type: ignore[arg-type]
            gap_summary="",
        )
        assert v.gap_classification == label


def test_validation_report_rejects_off_taxonomy() -> None:
    from gecko_core.models import ValidationReport
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ValidationReport(
            market_size_signal="m",
            competitor_analysis="c",
            demand_evidence="d",
            risk_flags=[],
            citations=[],
            gap_classification="kinda partial",  # type: ignore[arg-type]
        )


def test_validation_report_default_gap_is_fallback() -> None:
    from gecko_core.models import GAP_CLASSIFICATION_FALLBACK, ValidationReport

    v = ValidationReport(
        market_size_signal="m",
        competitor_analysis="c",
        demand_evidence="d",
        risk_flags=[],
        citations=[],
    )
    assert v.gap_classification == GAP_CLASSIFICATION_FALLBACK


# --- Synthesizer enforcement ------------------------------------------------


def _good_payload(*, gap: str = "Partial:segment", summary: str = "x") -> dict[str, Any]:
    return {
        "business_plan": {
            "problem": "p",
            "icp": "i",
            "solution": "s",
            "market": "m",
            "business_model": "bm",
            "channels": "c",
            "risks": ["r"],
            "citations": [],
        },
        "validation_report": {
            "market_size_signal": "x",
            "competitor_analysis": "y",
            "demand_evidence": "z",
            "risk_flags": [],
            "citations": [],
            "gap_classification": gap,
            "gap_summary": summary,
        },
        "prd": {
            "v1_scope": ["a"],
            "v2_scope": [],
            "v3_scope": [],
            "acceptance_criteria": ["b"],
            "non_functional": [],
            "success_metrics": [],
            "citations": [],
        },
    }


def _payload_without_gap() -> dict[str, Any]:
    p = _good_payload()
    del p["validation_report"]["gap_classification"]
    del p["validation_report"]["gap_summary"]
    return p


def _make_store_stub() -> Any:
    """Async store that records nothing but accepts the calls basic.generate
    makes (rag_query path, sources, add_cost)."""
    from gecko_core.models import SourceInfo

    store = AsyncMock()

    sources = [
        SourceInfo(
            url="https://example.com/a",  # type: ignore[arg-type]
            type="web",
            chunk_count=1,
            indexed_at=datetime.now(UTC),
        )
    ]

    async def _list_sources(_sid: object) -> list[SourceInfo]:
        return sources

    async def _add_cost(*_a: object, **_kw: object) -> None:
        return None

    store.list_sources = AsyncMock(side_effect=_list_sources)
    store.add_cost = AsyncMock(side_effect=_add_cost)
    return store


def _patch_rag(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _empty_rag(*_a: object, **_kw: object) -> list[Any]:
        return []

    monkeypatch.setattr("gecko_core.orchestration.basic.rag_query", _empty_rag, raising=True)


class _FakeOpenAI:
    """Minimal async client that returns canned raw JSON strings in order."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.chat = self  # tunnel: client.chat.completions.with_raw_response
        self.completions = self
        self.with_raw_response = self
        self.calls = 0

    async def create(self, **_kw: Any) -> Any:
        if not self._responses:
            raise AssertionError("FakeOpenAI ran out of canned responses")
        body = self._responses.pop(0)
        self.calls += 1

        class _Raw:
            def __init__(self, b: str) -> None:
                self._b = b
                self.headers: dict[str, str] = {}

            def parse(self) -> Any:
                class _Choice:
                    def __init__(self, content: str) -> None:
                        self.message = type("M", (), {"content": content})()

                class _Resp:
                    def __init__(self, content: str) -> None:
                        self.choices = [_Choice(content)]
                        self.usage = type("U", (), {"prompt_tokens": 10, "completion_tokens": 10})()
                        self.model = "gpt-4o-mini"

                return _Resp(self._b)

        return _Raw(body)


async def test_basic_generate_passes_through_valid_gap(monkeypatch: pytest.MonkeyPatch) -> None:
    from gecko_core.orchestration.basic import generate

    _patch_rag(monkeypatch)
    fake = _FakeOpenAI([json.dumps(_good_payload(gap="Partial:UX", summary="competitor X"))])
    store = _make_store_stub()
    sid = uuid4()
    res = await generate(sid, "an idea", store, openai_client=fake)  # type: ignore[arg-type]
    assert res.validation_report.gap_classification == "Partial:UX"
    assert res.validation_report.gap_summary == "competitor X"
    assert fake.calls == 1  # no retry needed


async def test_basic_generate_retries_when_gap_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gecko_core.orchestration.basic import generate

    _patch_rag(monkeypatch)
    # First call: missing gap_classification. Retry: valid.
    fake = _FakeOpenAI(
        [
            json.dumps(_payload_without_gap()),
            json.dumps(_good_payload(gap="Full", summary="Stripe Radar already covers this.")),
        ]
    )
    store = _make_store_stub()
    sid = uuid4()
    res = await generate(sid, "an idea", store, openai_client=fake)  # type: ignore[arg-type]
    assert res.validation_report.gap_classification == "Full"
    assert "Stripe Radar" in res.validation_report.gap_summary
    assert fake.calls == 2


async def test_basic_generate_falls_back_when_retry_also_invalid(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from gecko_core.models import GAP_CLASSIFICATION_FALLBACK
    from gecko_core.orchestration.basic import generate

    _patch_rag(monkeypatch)
    # Both calls omit / mis-type gap_classification.
    bad1 = _payload_without_gap()
    bad2 = _good_payload(gap="kinda partial")  # off-taxonomy string
    fake = _FakeOpenAI([json.dumps(bad1), json.dumps(bad2)])
    store = _make_store_stub()
    sid = uuid4()

    caplog.set_level(logging.WARNING)
    res = await generate(sid, "an idea", store, openai_client=fake)  # type: ignore[arg-type]

    # Fell back AND logged a warning.
    assert res.validation_report.gap_classification == GAP_CLASSIFICATION_FALLBACK
    messages = [rec.getMessage() for rec in caplog.records]
    assert any(
        "gap_classification" in m and ("fallback" in m.lower() or "falling back" in m.lower())
        for m in messages
    ), f"expected warning about gap_classification fallback; got {messages!r}"


# --- CLI render -------------------------------------------------------------


def _result_with_gap(gap: str, summary: str) -> Any:
    from gecko_core.models import (
        PRD,
        BusinessPlan,
        ResearchResult,
        SourceInfo,
        ValidationReport,
    )

    return ResearchResult(
        session_id=str(uuid4()),
        tier="basic",
        business_plan=BusinessPlan(
            problem="p",
            icp="i",
            solution="s",
            market="m",
            business_model="bm",
            channels="c",
            risks=["r"],
            citations=[],
        ),
        validation_report=ValidationReport(
            market_size_signal="x",
            competitor_analysis="y",
            demand_evidence="z",
            risk_flags=[],
            citations=[],
            gap_classification=gap,  # type: ignore[arg-type]
            gap_summary=summary,
        ),
        prd=PRD(
            v1_scope=["v1 thing"],
            v2_scope=[],
            v3_scope=[],
            acceptance_criteria=["acc"],
            non_functional=[],
            success_metrics=[],
            citations=[],
        ),
        sources=[
            SourceInfo(
                url="https://example.com/a",  # type: ignore[arg-type]
                type="web",
                chunk_count=1,
                indexed_at=datetime.now(UTC),
            )
        ],
    )


def test_render_surfaces_gap_in_validation_and_prd_panels() -> None:
    from gecko_cli.render import render_research_result

    result = _result_with_gap(
        "Partial:pricing",
        "Stripe Radar covers fraud detection but not webhook replay debugging.",
    )
    console = Console(record=True, width=120, color_system=None)
    render_research_result(result, console=console)
    out = console.export_text()
    # Validation panel — taxonomy label + summary both visible.
    assert "Gap: Partial:pricing" in out
    assert "Stripe Radar" in out
    # PRD panel — taxonomy label echoed under V1 scope.
    # We assert the label appears at least twice (once per panel).
    assert out.count("Partial:pricing") >= 2


def test_render_gap_renders_label_only_when_summary_empty() -> None:
    from gecko_cli.render import render_research_result

    result = _result_with_gap("False", "")
    console = Console(record=True, width=120, color_system=None)
    render_research_result(result, console=console)
    out = console.export_text()
    # Label still surfaces, no trailing em-dash separator.
    assert "Gap: False" in out
    assert "Gap: False —" not in out
