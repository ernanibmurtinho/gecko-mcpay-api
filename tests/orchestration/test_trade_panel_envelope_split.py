"""S35-#99 — verdict-envelope split unit tests.

Covers the two pure functions that implement the split, with light fakes
only — no LLM call, no panel run, no rubric judge:

  - ``partition_emitted_citations`` (gecko_core.orchestration.trade_panel):
    partitions retrieved chunks into (evidence, framework) emit-sets.
    Evidence = protocol/market chunks a turn referenced via [N]; framework
    = canon_* chunks (NOT relevance-trimmed).
  - ``_provider_kind_coverage`` (score_defi_trade_rubric): the decoupled
    coverage scorer — protocol kinds counted against evidence_citations,
    canon kinds against framework_context.

Why this matters: the N=30 ship-gate proved citation_relevance and
provider_kind_coverage were anti-correlated *by construction* because the
old single citations[] mixed "the data" with "the lens". These tests lock
the partition contract so the two dimensions stay decoupled.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

from gecko_core.orchestration.trade_panel import (
    TradePanelTurn,
    partition_emitted_citations,
)


def _chunk(idx: str, provider_kind: str) -> dict[str, Any]:
    """Minimal chunk dict — only the keys the partition reads."""
    return {
        "id": idx,
        "provider_kind": provider_kind,
        "source_url": f"https://example.test/{idx}",
        "text": f"chunk {idx} body",
    }


def _turn(content: str) -> TradePanelTurn:
    return TradePanelTurn(agent="technical_analyst", content=content)


# --------------------- partition_emitted_citations ---------------------


def test_partition_empty_chunks_returns_two_empty_lists() -> None:
    evidence, framework = partition_emitted_citations([], [])
    assert evidence == []
    assert framework == []


def test_partition_splits_evidence_from_canon() -> None:
    """canon_* -> framework; protocol/market -> evidence (used-only)."""
    chunks = [
        _chunk("c1", "protocol_native"),
        _chunk("c2", "canon_marks"),
        _chunk("c3", "market_data"),
        _chunk("c4", "canon_damodaran"),
    ]
    # Turn references markers [1] and [3] — both evidence-kind.
    turns = [_turn("Kamino params per [1]; price action per [3].")]
    evidence, framework = partition_emitted_citations(turns, chunks)

    assert [c["id"] for c in evidence] == ["c1", "c3"]
    # framework is ALL canon, marker-usage irrelevant.
    assert [c["id"] for c in framework] == ["c2", "c4"]


def test_partition_evidence_is_used_only() -> None:
    """An evidence-kind chunk no turn referenced is dropped from evidence."""
    chunks = [
        _chunk("c1", "protocol_native"),
        _chunk("c2", "protocol_native"),
        _chunk("c3", "market_data"),
    ]
    # Only [2] is referenced.
    turns = [_turn("The vault doc [2] is the only thing I lean on.")]
    evidence, framework = partition_emitted_citations(turns, chunks)

    assert [c["id"] for c in evidence] == ["c2"]
    assert framework == []


def test_partition_framework_is_not_trimmed() -> None:
    """All canon chunks survive even when zero canon markers were written."""
    chunks = [
        _chunk("c1", "protocol_native"),
        _chunk("c2", "canon_marks"),
        _chunk("c3", "canon_berkshire"),
    ]
    turns = [_turn("I only cite the protocol doc [1].")]
    evidence, framework = partition_emitted_citations(turns, chunks)

    assert [c["id"] for c in evidence] == ["c1"]
    # canon_marks + canon_berkshire both kept despite no [2]/[3] marker.
    assert [c["id"] for c in framework] == ["c2", "c3"]


def test_partition_no_markers_keeps_all_evidence() -> None:
    """Used-detection unreliable (zero markers) -> keep every evidence chunk."""
    chunks = [
        _chunk("c1", "protocol_native"),
        _chunk("c2", "market_data"),
        _chunk("c3", "canon_marks"),
    ]
    turns = [_turn("No bracket markers at all in this turn.")]
    evidence, framework = partition_emitted_citations(turns, chunks)

    assert [c["id"] for c in evidence] == ["c1", "c2"]
    assert [c["id"] for c in framework] == ["c3"]


def test_partition_canon_only_markers_falls_back_to_all_evidence() -> None:
    """When markers point only at canon chunks, evidence must not empty out.

    Used-only would otherwise drop every evidence chunk and floor coverage.
    """
    chunks = [
        _chunk("c1", "protocol_native"),
        _chunk("c2", "canon_marks"),
        _chunk("c3", "market_data"),
    ]
    # [2] is canon; no evidence-kind marker referenced.
    turns = [_turn("Marks on cycles [2] frames my whole read.")]
    evidence, framework = partition_emitted_citations(turns, chunks)

    # Fallback: all evidence-kind chunks kept rather than an empty list.
    assert [c["id"] for c in evidence] == ["c1", "c3"]
    assert [c["id"] for c in framework] == ["c2"]


def test_partition_out_of_range_marker_ignored() -> None:
    """A [99] marker (no such chunk) is not a valid used-marker."""
    chunks = [_chunk("c1", "protocol_native"), _chunk("c2", "market_data")]
    # [99] is junk; [1] is valid.
    turns = [_turn("See [1] and the 2024 figure [99].")]
    evidence, _framework = partition_emitted_citations(turns, chunks)
    assert [c["id"] for c in evidence] == ["c1"]


def test_partition_non_canon_non_evidence_kind_follows_evidence_path() -> None:
    """A `web` chunk is data, not lens — used-only evidence path."""
    chunks = [_chunk("c1", "web"), _chunk("c2", "canon_marks")]
    turns = [_turn("Background blog [1].")]
    evidence, framework = partition_emitted_citations(turns, chunks)
    assert [c["id"] for c in evidence] == ["c1"]
    assert [c["id"] for c in framework] == ["c2"]


# --------------------- rubric _provider_kind_coverage ---------------------


def _load_rubric_scorer() -> Any:
    """Import the scorer module by path (tests/eval/scripts is not a pkg)."""
    path = Path(__file__).resolve().parents[1] / "eval" / "scripts" / "score_defi_trade_rubric.py"
    spec = importlib.util.spec_from_file_location("_score_rubric_under_test", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _cite(provider_kind: str) -> dict[str, Any]:
    return {"provider_kind": provider_kind, "source": "test"}


def test_coverage_full_when_both_lists_satisfy_required() -> None:
    scorer = _load_rubric_scorer()
    fixture = {
        "must_cite_evidence_kinds": ["protocol_native"],
        "must_cite_framework_kinds": ["canon_marks", "canon_damodaran"],
    }
    evidence = [_cite("protocol_native")]
    framework = [_cite("canon_marks"), _cite("canon_damodaran")]
    assert scorer._provider_kind_coverage(evidence, framework, fixture) == 1.0


def test_coverage_partial_when_one_canon_kind_missing() -> None:
    scorer = _load_rubric_scorer()
    fixture = {
        "must_cite_evidence_kinds": ["protocol_native"],
        "must_cite_framework_kinds": ["canon_marks", "canon_damodaran"],
    }
    evidence = [_cite("protocol_native")]
    framework = [_cite("canon_marks")]  # canon_damodaran missing
    # 2 of 3 required kinds satisfied.
    assert scorer._provider_kind_coverage(evidence, framework, fixture) == 2 / 3


def test_coverage_canon_in_wrong_list_does_not_count() -> None:
    """A canon kind that lands in evidence_citations is NOT credited.

    This is the whole point of the split — canon belongs in framework.
    """
    scorer = _load_rubric_scorer()
    fixture = {
        "must_cite_evidence_kinds": ["protocol_native"],
        "must_cite_framework_kinds": ["canon_marks"],
    }
    # canon_marks misplaced into the evidence list.
    evidence = [_cite("protocol_native"), _cite("canon_marks")]
    framework: list[dict[str, Any]] = []
    # Only protocol_native counts -> 1 of 2.
    assert scorer._provider_kind_coverage(evidence, framework, fixture) == 0.5


def test_coverage_legacy_flat_list_auto_splits_by_canon_prefix() -> None:
    """Fixtures without the explicit keys fall back to canon_-prefix split."""
    scorer = _load_rubric_scorer()
    fixture = {
        "must_cite_provider_kinds": ["protocol_native", "canon_marks"],
    }
    evidence = [_cite("protocol_native")]
    framework = [_cite("canon_marks")]
    assert scorer._provider_kind_coverage(evidence, framework, fixture) == 1.0


def test_coverage_no_required_kinds_returns_one() -> None:
    scorer = _load_rubric_scorer()
    assert scorer._provider_kind_coverage([], [], {}) == 1.0


def test_coverage_zero_when_required_kinds_unmet() -> None:
    scorer = _load_rubric_scorer()
    fixture = {"must_cite_evidence_kinds": ["protocol_native"]}
    assert scorer._provider_kind_coverage([], [], fixture) == 0.0
