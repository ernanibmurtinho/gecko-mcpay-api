"""S20-C-CITATION-CONTRACT-01 — synth-citation parser tests."""

from __future__ import annotations

import logging

import pytest
from gecko_core.judges.synth_citations import extract_citation_markers
from gecko_core.models import CitationMarker, ResearchResult


def _raw(idx: int, doc_id: str, url: str, span: str | None = None) -> dict[str, object]:
    return {"idx": idx, "doc_id": doc_id, "url": url, "span": span}


def test_golden_three_chunks_two_markers() -> None:
    """Two valid `[1]` `[2]` markers map to two CitationMarker entries."""
    allowed = {"chunk-a", "chunk-b", "chunk-c"}
    raw = [
        _raw(1, "chunk-a", "https://example.com/a", "passage A"),
        _raw(2, "chunk-b", "https://example.com/b", "passage B"),
    ]
    prose = ["The wedge is X [1] and the consequence is Y [2]."]

    markers, cited = extract_citation_markers(
        raw_citations=raw, allowed_doc_ids=allowed, prose_surfaces=prose
    )

    assert [m.idx for m in markers] == [1, 2]
    assert cited == ["chunk-a", "chunk-b"]
    assert all(isinstance(m, CitationMarker) for m in markers)


def test_hallucinated_doc_id_dropped(caplog: pytest.LogCaptureFixture) -> None:
    """A doc_id outside allowed set is dropped; hallucination_rate logged."""
    allowed = {"chunk-real"}
    raw = [
        _raw(1, "chunk-real", "https://example.com/real"),
        _raw(2, "chunk-fake", "https://example.com/fake"),
    ]
    prose = ["claim one [1] claim two [2]"]

    with caplog.at_level(logging.INFO, logger="gecko_core.judges.synth_citations"):
        markers, cited = extract_citation_markers(
            raw_citations=raw, allowed_doc_ids=allowed, prose_surfaces=prose
        )

    assert [m.doc_id for m in markers] == ["chunk-real"]
    assert cited == ["chunk-real"]
    msgs = "\n".join(r.getMessage() for r in caplog.records)
    assert "synth.citation.hallucination_rate" in msgs
    assert "dropped=1" in msgs
    assert "total=2" in msgs


def test_unmatched_prose_marker_not_credited(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A `[3]` in prose with no matching `citations[*].idx` is dropped from cited_doc_ids."""
    allowed = {"chunk-a", "chunk-b"}
    raw = [
        _raw(1, "chunk-a", "https://example.com/a"),
        _raw(2, "chunk-b", "https://example.com/b"),
    ]
    # Prose mentions [1], [2], AND a dangling [3] — that [3] has no entry.
    prose = ["A [1] and B [2] and ghost [3]"]

    with caplog.at_level(logging.WARNING, logger="gecko_core.judges.synth_citations"):
        markers, cited = extract_citation_markers(
            raw_citations=raw, allowed_doc_ids=allowed, prose_surfaces=prose
        )

    # Only the two matched entries survive. The dangling [3] is logged.
    assert {m.idx for m in markers} == {1, 2}
    assert set(cited) == {"chunk-a", "chunk-b"}
    msgs = "\n".join(r.getMessage() for r in caplog.records)
    assert "synth.citation.dropped" in msgs
    assert "unmatched_prose_markers=1" in msgs


def test_empty_rag_context_returns_empty() -> None:
    """No allowed doc_ids and no citations → empty result, no error."""
    markers, cited = extract_citation_markers(
        raw_citations=[], allowed_doc_ids=set(), prose_surfaces=[]
    )
    assert markers == []
    assert cited == []

    # And: None input round-trips cleanly.
    markers2, cited2 = extract_citation_markers(
        raw_citations=None, allowed_doc_ids=set(), prose_surfaces=[]
    )
    assert markers2 == []
    assert cited2 == []


def test_research_result_roundtrip_preserves_fields() -> None:
    """Verdict roundtrips through pydantic preserving the new fields."""
    from gecko_core.models import (
        PRD,
        BusinessPlan,
        ValidationReport,
    )

    bp = BusinessPlan(
        problem="p",
        icp="i",
        solution="s",
        market="m",
        business_model="bm",
        channels="c",
        risks=[],
        citations=[],
    )
    vr = ValidationReport(
        market_size_signal="x",
        competitor_analysis="x",
        demand_evidence="x",
        risk_flags=[],
        citations=[],
        gap_classification="Partial:UX",
        gap_summary="x",
        gap_explanation="x [1]",
    )
    prd = PRD(
        v1_scope=["x"],
        v2_scope=["x"],
        v3_scope=["x"],
        acceptance_criteria=["x"],
        non_functional=["x"],
        success_metrics=["x"],
        citations=[],
    )
    markers = [CitationMarker(idx=1, doc_id="chunk-a", url="https://example.com/a", span=None)]
    rr = ResearchResult(
        session_id="00000000-0000-0000-0000-000000000000",
        tier="basic",
        business_plan=bp,
        validation_report=vr,
        prd=prd,
        sources=[],
        cited_doc_ids=["chunk-a"],
        citation_markers=markers,
    )
    rr2 = ResearchResult.model_validate(rr.model_dump())
    assert rr2.cited_doc_ids == ["chunk-a"]
    assert len(rr2.citation_markers) == 1
    assert rr2.citation_markers[0].doc_id == "chunk-a"
    assert rr2.citation_markers[0].idx == 1


class _FakeChunk:
    """Minimal RagChunk-shape stand-in for back-fill tests (id + url only)."""

    def __init__(self, chunk_id: str, source_url: str) -> None:
        self.chunk_id = chunk_id
        self.source_url = source_url


def test_back_fill_happy_path_with_structured_citations() -> None:
    """S20-FIX-04 regression check: structured citations + prose markers
    take the normal path — back-fill does NOT fire when entries already exist."""
    allowed = {"chunk-a", "chunk-b"}
    raw = [
        _raw(1, "chunk-a", "https://example.com/a", "passage A"),
        _raw(2, "chunk-b", "https://example.com/b", "passage B"),
    ]
    chunks = [
        _FakeChunk("chunk-a", "https://example.com/a"),
        _FakeChunk("chunk-b", "https://example.com/b"),
    ]
    prose = ["A [1] and B [2]."]

    markers, cited = extract_citation_markers(
        raw_citations=raw,
        allowed_doc_ids=allowed,
        prose_surfaces=prose,
        rag_context_chunks=chunks,
    )
    assert {m.idx for m in markers} == {1, 2}
    assert set(cited) == {"chunk-a", "chunk-b"}


def test_back_fill_kicks_in_on_empty_citations(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """S20-FIX-04: empty `citations` + prose `[1][2]` → back-filled, WARN fires."""
    allowed = {"chunk-a", "chunk-b"}
    chunks = [
        _FakeChunk("chunk-a", "https://example.com/a"),
        _FakeChunk("chunk-b", "https://example.com/b"),
    ]
    prose = ["wedge claim [1] and consequence [2]."]

    with caplog.at_level(logging.WARNING, logger="gecko_core.judges.synth_citations"):
        markers, cited = extract_citation_markers(
            raw_citations=[],
            allowed_doc_ids=allowed,
            prose_surfaces=prose,
            rag_context_chunks=chunks,
        )

    assert {m.idx for m in markers} == {1, 2}
    assert {m.doc_id for m in markers} == {"chunk-a", "chunk-b"}
    assert set(cited) == {"chunk-a", "chunk-b"}
    msgs = "\n".join(r.getMessage() for r in caplog.records)
    assert "synth.citation.back_fill_used" in msgs
    assert "count=2" in msgs


def test_back_fill_caps_when_prose_exceeds_chunks(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """5 prose markers + 2 chunks → back-fill caps at 2, cap_hit=3 in log."""
    allowed = {"chunk-a", "chunk-b"}
    chunks = [
        _FakeChunk("chunk-a", "https://example.com/a"),
        _FakeChunk("chunk-b", "https://example.com/b"),
    ]
    prose = ["A [1] B [2] C [3] D [4] E [5]."]

    with caplog.at_level(logging.WARNING, logger="gecko_core.judges.synth_citations"):
        markers, cited = extract_citation_markers(
            raw_citations=[],
            allowed_doc_ids=allowed,
            prose_surfaces=prose,
            rag_context_chunks=chunks,
        )

    # Only the in-range [1][2] back-fill; [3][4][5] are dropped.
    assert {m.idx for m in markers} == {1, 2}
    assert set(cited) == {"chunk-a", "chunk-b"}
    msgs = "\n".join(r.getMessage() for r in caplog.records)
    assert "synth.citation.back_fill_used" in msgs
    assert "cap_hit=3" in msgs


def test_hallucinated_with_back_fill_drops_only_the_bad_one(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Hallucinated doc_id still drops; valid entries (incl. back-filled) survive."""
    allowed = {"chunk-a", "chunk-b"}
    raw = [
        # Model emitted citations[0] for [1], with a hallucinated doc_id.
        _raw(1, "chunk-GHOST", "https://example.com/ghost"),
        # Model emitted citations[1] for [2], grounded.
        _raw(2, "chunk-b", "https://example.com/b"),
    ]
    chunks = [
        _FakeChunk("chunk-a", "https://example.com/a"),
        _FakeChunk("chunk-b", "https://example.com/b"),
    ]
    prose = ["A [1] B [2]."]

    with caplog.at_level(logging.INFO, logger="gecko_core.judges.synth_citations"):
        markers, cited = extract_citation_markers(
            raw_citations=raw,
            allowed_doc_ids=allowed,
            prose_surfaces=prose,
            rag_context_chunks=chunks,
        )

    # Hallucinated [1] dropped at the grounding pass; [2]'s real entry kept.
    # Back-fill does NOT re-add [1] because the model DID emit an entry for
    # idx=1 (it just had the wrong doc_id) — back-fill only fires for
    # missing_idxs (prose [N] with no citations[*].idx == N).
    assert {m.idx for m in markers} == {2}
    assert cited == ["chunk-b"]
    msgs = "\n".join(r.getMessage() for r in caplog.records)
    assert "synth.citation.hallucination_rate" in msgs


def test_span_truncated_to_200_chars() -> None:
    """Defensive: a span > 200 chars is truncated, not raised."""
    allowed = {"chunk-a"}
    long_span = "x" * 500
    raw = [_raw(1, "chunk-a", "https://example.com/a", long_span)]
    prose = ["claim [1]"]
    markers, _ = extract_citation_markers(
        raw_citations=raw, allowed_doc_ids=allowed, prose_surfaces=prose
    )
    assert len(markers) == 1
    assert markers[0].span is not None
    assert len(markers[0].span) <= 200
