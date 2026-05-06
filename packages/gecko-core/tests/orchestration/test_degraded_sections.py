"""S20-OBS-01 — unit tests for DegradedSectionTracker.

Covers the tracker's mark/clear/to_dict/apply_to surface in isolation. The
basic.generate wiring is tested separately in test_basic_degraded.py so
this file stays free of orchestration/RAG fixtures.
"""

from __future__ import annotations

from gecko_core.models import (
    PRD,
    BusinessPlan,
    ResearchResult,
    ValidationReport,
)
from gecko_core.orchestration.degraded import DegradedSectionTracker


def _empty_result() -> ResearchResult:
    """Build a minimal ResearchResult that satisfies the schema.

    We only need a shell — these tests assert on degraded_sections /
    degraded_reasons, never on the prose fields.
    """
    return ResearchResult(
        session_id="00000000-0000-0000-0000-000000000000",
        tier="basic",
        business_plan=BusinessPlan(
            problem="p",
            icp="i",
            solution="s",
            market="m",
            business_model="b",
            channels="c",
            risks=[],
            citations=[],
        ),
        validation_report=ValidationReport(
            market_size_signal="s",
            competitor_analysis="c",
            demand_evidence="d",
            risk_flags=[],
            citations=[],
        ),
        prd=PRD(
            v1_scope=[],
            v2_scope=[],
            v3_scope=[],
            acceptance_criteria=[],
            non_functional=[],
            success_metrics=[],
            citations=[],
        ),
        sources=[],
    )


def test_empty_tracker_apply_to_leaves_clean_fields() -> None:
    tracker = DegradedSectionTracker()
    result = _empty_result()

    tracker.apply_to(result)

    assert result.degraded_sections == []
    assert result.degraded_reasons == {}


def test_single_mark_populates_both_fields() -> None:
    tracker = DegradedSectionTracker()
    tracker.mark("market_landscape", "truncated")
    result = _empty_result()

    tracker.apply_to(result)

    assert result.degraded_sections == ["market_landscape"]
    assert result.degraded_reasons == {"market_landscape": "truncated"}


def test_multiple_marks_surface_all_entries() -> None:
    tracker = DegradedSectionTracker()
    tracker.mark("market_landscape", "validation_dropped_all")
    tracker.mark("next_steps", "model_timeout")
    result = _empty_result()

    tracker.apply_to(result)

    # apply_to sorts section names for deterministic ordering.
    assert result.degraded_sections == ["market_landscape", "next_steps"]
    assert result.degraded_reasons == {
        "market_landscape": "validation_dropped_all",
        "next_steps": "model_timeout",
    }


def test_clear_after_mark_returns_to_empty() -> None:
    tracker = DegradedSectionTracker()
    tracker.mark("market_landscape", "truncated")
    tracker.clear("market_landscape")
    result = _empty_result()

    tracker.apply_to(result)

    assert result.degraded_sections == []
    assert result.degraded_reasons == {}


def test_clear_unmarked_section_is_idempotent_noop() -> None:
    tracker = DegradedSectionTracker()
    # Should not raise.
    tracker.clear("never_marked")
    tracker.clear("never_marked")  # twice for good measure
    result = _empty_result()

    tracker.apply_to(result)

    assert result.degraded_sections == []
    assert result.degraded_reasons == {}


def test_to_dict_returns_independent_copy() -> None:
    tracker = DegradedSectionTracker()
    tracker.mark("market_landscape", "truncated")

    snapshot = tracker.to_dict()
    snapshot["market_landscape"] = "MUTATED"
    snapshot["new_key"] = "leak"

    # Tracker state must be untouched by mutating the returned dict.
    assert tracker.to_dict() == {"market_landscape": "truncated"}


def test_apply_to_returns_same_result_for_chaining() -> None:
    tracker = DegradedSectionTracker()
    tracker.mark("section_a", "reason_a")
    result = _empty_result()

    returned = tracker.apply_to(result)

    assert returned is result
    assert returned.degraded_sections == ["section_a"]
