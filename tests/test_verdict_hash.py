"""Tests for deterministic verdict hash (S18-VERDICT-HASH-01)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from gecko_core.models import (
    PRD,
    BusinessPlan,
    ResearchResult,
    SourceInfo,
    Tier,
    ValidationReport,
    Verdict,
)
from gecko_core.verdict_hash import verdict_hash, verdict_hash_short


def _make_result(
    *,
    sources: list[str],
    verdict: Verdict = Verdict.REFINE,
    gap: str = "Partial:UX",
    tier: Tier = "basic",
    low_grounding: bool = False,
) -> ResearchResult:
    """Build a minimal ResearchResult for hash testing."""
    src_objs = [
        SourceInfo(
            url=u,
            type="web",
            chunk_count=3,
            indexed_at=datetime(2026, 5, 2, tzinfo=UTC),
        )
        for u in sources
    ]
    return ResearchResult(
        session_id="00000000-0000-0000-0000-000000000000",
        tier=tier,
        business_plan=BusinessPlan(
            problem="p",
            icp="i",
            solution="s",
            market="m",
            business_model="b",
            channels="c",
            risks=["r"],
            citations=[],
        ),
        validation_report=ValidationReport(
            market_size_signal="x",
            competitor_analysis="x",
            demand_evidence="x",
            risk_flags=["x"],
            citations=[],
            gap_classification=gap,  # type: ignore[arg-type]
        ),
        prd=PRD(
            v1_scope=["v1"],
            v2_scope=["v2"],
            v3_scope=["v3"],
            acceptance_criteria=["a"],
            non_functional=["n"],
            success_metrics=["s"],
            citations=[],
        ),
        sources=src_objs,
        verdict=verdict,
        low_grounding=low_grounding,
    )


class TestVerdictHash:
    def test_deterministic_same_inputs(self) -> None:
        idea = "platform for trading judgments"
        r = _make_result(sources=["https://example.com/a", "https://example.com/b"])
        h1 = verdict_hash(idea, r)
        h2 = verdict_hash(idea, r)
        assert h1 == h2
        assert len(h1) == 64

    def test_idea_whitespace_normalised(self) -> None:
        r = _make_result(sources=["https://x.com"])
        a = verdict_hash("buy a guide", r)
        b = verdict_hash("  buy   a   guide  ", r)
        c = verdict_hash("buy\na\tguide", r)
        assert a == b == c

    def test_source_order_independent(self) -> None:
        r1 = _make_result(sources=["https://x.com/a", "https://x.com/b"])
        r2 = _make_result(sources=["https://x.com/b", "https://x.com/a"])
        assert verdict_hash("idea", r1) == verdict_hash("idea", r2)

    def test_source_dedup(self) -> None:
        r1 = _make_result(sources=["https://x.com/a", "https://x.com/a"])
        r2 = _make_result(sources=["https://x.com/a"])
        assert verdict_hash("idea", r1) == verdict_hash("idea", r2)

    def test_different_idea_different_hash(self) -> None:
        r = _make_result(sources=["https://x.com"])
        assert verdict_hash("idea-A", r) != verdict_hash("idea-B", r)

    def test_different_sources_different_hash(self) -> None:
        r1 = _make_result(sources=["https://x.com/a"])
        r2 = _make_result(sources=["https://x.com/b"])
        assert verdict_hash("idea", r1) != verdict_hash("idea", r2)

    def test_different_verdict_different_hash(self) -> None:
        r1 = _make_result(sources=["https://x.com"], verdict=Verdict.GO)
        r2 = _make_result(sources=["https://x.com"], verdict=Verdict.PIVOT)
        assert verdict_hash("idea", r1) != verdict_hash("idea", r2)

    def test_different_gap_classification_different_hash(self) -> None:
        r1 = _make_result(sources=["https://x.com"], gap="Partial:UX")
        r2 = _make_result(sources=["https://x.com"], gap="Partial:segment")
        assert verdict_hash("idea", r1) != verdict_hash("idea", r2)

    def test_low_grounding_flag_changes_hash(self) -> None:
        r1 = _make_result(sources=["https://x.com"], low_grounding=False)
        r2 = _make_result(sources=["https://x.com"], low_grounding=True)
        assert verdict_hash("idea", r1) != verdict_hash("idea", r2)

    def test_short_format(self) -> None:
        r = _make_result(sources=["https://x.com"])
        short = verdict_hash_short("idea", r)
        assert short.startswith("verdict@")
        assert len(short) == len("verdict@") + 12
        # Deterministic
        assert short == verdict_hash_short("idea", r)

    def test_empty_sources_stable(self) -> None:
        r = _make_result(sources=[])
        h = verdict_hash("idea", r)
        assert len(h) == 64

    def test_prose_drift_does_not_change_hash(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """If we ever attach pro_session_summary, hash must NOT include it
        — prose drifts between reruns even when the structural verdict
        is identical, and S18-D4's whole point is identity stability."""
        r1 = _make_result(sources=["https://x.com"])
        r2 = _make_result(sources=["https://x.com"])
        r2.pro_session_summary = "different prose blob"
        assert verdict_hash("idea", r1) == verdict_hash("idea", r2)
