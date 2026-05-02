"""S12-HARDEN-03 — production judge transcript capture.

Asserts the capture writes a record with all the audit-trail fields the
runbook needs, and that the toggle + path-resolution behave correctly.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest


def _result(
    *,
    tier: str = "pro",
    gap: str = "Partial:segment",
    verdict_token: str = "REFINE",
    judge_prose: str = "Final verdict: REFINE\nThe wedge is real but the ICP is too broad.",
) -> Any:
    """Build a ResearchResult that exercises both basic and pro paths."""
    from gecko_core.models import (
        PRD,
        BusinessPlan,
        ResearchResult,
        SourceInfo,
        ValidationReport,
        Verdict,
    )

    transcript: dict[str, Any] | None
    if tier == "pro":
        transcript = {
            "turns": [
                {"agent": "analyst", "content": "ICP analysis", "tokens_in": 10, "tokens_out": 20},
                {"agent": "judge", "content": judge_prose, "tokens_in": 30, "tokens_out": 40},
            ]
        }
        summary = judge_prose
    else:
        transcript = None
        summary = None

    return ResearchResult(
        session_id="22222222-2222-2222-2222-222222222222",
        tier=tier,  # type: ignore[arg-type]
        business_plan=BusinessPlan(
            problem="p",
            icp="i",
            solution="s",
            market="m",
            business_model="bm",
            channels="c",
            risks=[],
            citations=[],
        ),
        validation_report=ValidationReport(
            market_size_signal="m",
            competitor_analysis="c",
            demand_evidence="d",
            risk_flags=[],
            citations=[],
            gap_classification=gap,  # type: ignore[arg-type]
            gap_summary="Stripe Radar covers fraud but not webhook replay.",
        ),
        prd=PRD(
            v1_scope=["v1"],
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
        transcript=transcript,
        pro_session_summary=summary,
        verdict=Verdict(verdict_token),
    )


def test_capture_disabled_by_default_in_tests(tmp_path: Path) -> None:
    """conftest sets GECKO_TRANSCRIPT_CAPTURE=false; capture() returns None."""
    from gecko_core.orchestration.transcripts import capture, is_capture_enabled

    assert is_capture_enabled() is False
    out = capture(session_id=uuid4(), idea="smoke", result=_result())
    assert out is None


def test_capture_writes_full_record_pro_tier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gecko_core.orchestration.transcripts import capture

    monkeypatch.setenv("GECKO_TRANSCRIPT_CAPTURE", "true")
    monkeypatch.setenv("GECKO_TRANSCRIPT_DIR", str(tmp_path))

    sid = uuid4()
    out = capture(session_id=sid, idea="a hotel guide for Brazil", result=_result(tier="pro"))

    assert out is not None
    assert out == tmp_path / f"{sid}.json"
    payload = json.loads(out.read_text(encoding="utf-8"))

    # Audit-trail contract — every key the runbook's jq queries depend on.
    assert payload["session_id"] == str(sid)
    assert payload["idea_text"] == "a hotel guide for Brazil"
    assert payload["tier"] == "pro"
    assert payload["actual_verdict"] == "pivot"  # REFINE → legacy pivot
    assert payload["actual_verdict_v2"] == "REFINE"
    assert payload["parsed_verdict"] == "REFINE"
    assert payload["gap_classification"] == "Partial:segment"
    assert "Stripe Radar" in payload["gap_summary"]
    assert payload["judge_prose"].startswith("Final verdict: REFINE")
    assert payload["agent_turns"] is not None
    assert "judge" in payload["agent_turns"]
    assert payload["agent_turns"]["judge"]["text"].startswith("Final verdict: REFINE")
    assert "created_at" in payload


def test_capture_basic_tier_no_transcript(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Basic tier has no judge / no transcript; record still archives the
    structured verdict + gap so misranked verdicts surfaced in Bazaar are
    auditable for both tiers."""
    from gecko_core.orchestration.transcripts import capture

    monkeypatch.setenv("GECKO_TRANSCRIPT_CAPTURE", "true")
    monkeypatch.setenv("GECKO_TRANSCRIPT_DIR", str(tmp_path))

    out = capture(
        session_id=uuid4(),
        idea="smoke",
        result=_result(tier="basic", verdict_token="PIVOT"),
    )
    assert out is not None
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["tier"] == "basic"
    assert payload["actual_verdict"] == "kill"
    assert payload["actual_verdict_v2"] == "PIVOT"
    # No transcript on basic tier — keys present, agent_turns is null.
    assert payload["agent_turns"] is None
    # No judge prose either; parsed_verdict falls back to UNKNOWN.
    assert payload["judge_prose"] == ""
    assert payload["parsed_verdict"] == "UNKNOWN"


def test_capture_toggle_off_via_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from gecko_core.orchestration.transcripts import capture

    monkeypatch.setenv("GECKO_TRANSCRIPT_CAPTURE", "0")
    monkeypatch.setenv("GECKO_TRANSCRIPT_DIR", str(tmp_path))

    out = capture(session_id=uuid4(), idea="smoke", result=_result())
    assert out is None
    assert list(tmp_path.iterdir()) == []
