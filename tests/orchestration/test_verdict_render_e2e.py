"""S11-VERDICT-01 — E2E smoke that ``bb research`` surfaces the verdict.

Stubs ``gecko_core.research`` with a hand-built ResearchResult so we don't
depend on Tavily / OpenAI / Supabase at unit-test time, then runs the
Click CLI and asserts the verdict headline + typed gap sub-line both
appear in the rendered output (above the validation panel and at the top
of the PRD panel).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from click.testing import CliRunner


def _result_with_verdict(*, gap: str, verdict_token: str) -> Any:
    """Build a ResearchResult with an explicit verdict + gap label."""
    from gecko_core.models import (
        PRD,
        BusinessPlan,
        ResearchResult,
        SourceInfo,
        ValidationReport,
        Verdict,
    )

    return ResearchResult(
        session_id="22222222-2222-2222-2222-222222222222",
        tier="basic",
        business_plan=BusinessPlan(
            problem="p",
            icp="i",
            solution="s",
            market="m",
            business_model="bm",
            channels="c",
            risks=["r1"],
            citations=[],
        ),
        validation_report=ValidationReport(
            market_size_signal="m",
            competitor_analysis="c",
            demand_evidence="d",
            risk_flags=[],
            citations=[],
            gap_classification=gap,  # type: ignore[arg-type]
            gap_summary="competitor X covers most segments but not THIS one.",
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
        verdict=Verdict(verdict_token),
    )


@pytest.fixture
def _patch_research_with(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Factory: returns a setup() that patches `research` to return a given
    ResearchResult, mirroring the pattern in test_research_yes_flag.py."""

    def _setup(result_obj: Any) -> None:
        async def _fake_research(**_kwargs: Any) -> Any:
            return result_obj

        import gecko_core
        from gecko_cli.commands import research as research_module

        monkeypatch.setattr(gecko_core, "research", _fake_research)
        monkeypatch.setattr(research_module.gecko_core, "research", _fake_research)
        monkeypatch.setattr(research_module, "resolve_project_id", lambda *_a, **_k: None)

    return _setup


def _run(idea: str = "smoke test") -> Any:
    from gecko_cli.main import cli

    runner = CliRunner()
    return runner.invoke(
        cli,
        ["research", "--idea", idea, "--yes"],
        input="",
        catch_exceptions=False,
    )


def test_bb_research_renders_pivot_verdict_line(_patch_research_with: Any) -> None:
    # S17-TONE-01: KILL → PIVOT (same semantics, softer founder-facing copy).
    _patch_research_with(_result_with_verdict(gap="Full", verdict_token="PIVOT"))

    result = _run()
    assert result.exit_code == 0, result.output
    # Headline token surfaces — at least once in validation panel, once in PRD.
    assert result.output.count("PIVOT") >= 2
    # Typed gap stays as evidence under the verdict.
    assert "Gap: Full" in result.output
    # Old vocabulary must be gone from the surface.
    assert "KILL" not in result.output


def test_bb_research_renders_go_verdict_line(_patch_research_with: Any) -> None:
    # S17-TONE-01: BUILD → GO.
    _patch_research_with(_result_with_verdict(gap="Partial:pricing", verdict_token="GO"))

    result = _run()
    assert result.exit_code == 0, result.output
    assert result.output.count("GO") >= 2
    assert "Gap: Partial:pricing" in result.output
    assert "BUILD" not in result.output


def test_bb_research_renders_refine_verdict_line(_patch_research_with: Any) -> None:
    _patch_research_with(_result_with_verdict(gap="Partial:segment", verdict_token="REFINE"))

    result = _run()
    assert result.exit_code == 0, result.output
    assert result.output.count("REFINE") >= 2
    assert "Gap: Partial:segment" in result.output
