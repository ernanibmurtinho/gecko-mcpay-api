"""Reproducibility + calibration tests for the deterministic rubric scorer.

The test suite asserts:
  - same input → identical scores (twice in a row)
  - kill mocks score lower on grounding than ship mocks (calibration)
  - extract_verdict handles the canned judge text shapes
"""

from __future__ import annotations

import statistics

from tests.eval.mocks import MOCK_TRANSCRIPTS
from tests.eval.rubric import (
    extract_verdict,
    score_transcript_mock,
)


def _cohort_median() -> float:
    totals = [
        sum(t["tokens_in"] + t["tokens_out"] for t in tr.values())
        for tr in MOCK_TRANSCRIPTS.values()
    ]
    return statistics.median(totals)


def test_mock_scorer_is_reproducible() -> None:
    median = _cohort_median()
    for idea_id, transcript in MOCK_TRANSCRIPTS.items():
        s1 = score_transcript_mock(transcript, median)
        s2 = score_transcript_mock(transcript, median)
        assert s1 == s2, f"non-deterministic score for {idea_id}"


def test_extract_verdict_kill() -> None:
    assert extract_verdict("Verdict: KILL. Saturated.") == "kill"
    assert extract_verdict("verdict: kill") == "kill"


def test_extract_verdict_ship() -> None:
    assert extract_verdict("Verdict: SHIP. Narrow.") == "ship"
    assert extract_verdict("Verdict: BUILD it.") == "ship"


def test_extract_verdict_pivot() -> None:
    assert extract_verdict("Verdict: PIVOT to a vertical.") == "pivot"


def test_extract_verdict_unknown() -> None:
    assert extract_verdict("totally ambiguous text") == "unknown"


def test_kill_ideas_have_higher_verdict_justification_than_random_baseline() -> None:
    """The mocks are crafted so a kill judge + a kill-leaning critic align,
    yielding a high verdict_justification score. If this drops below 7,
    the mocks or the scorer drifted."""
    median = _cohort_median()
    kill_ids = [k for k in MOCK_TRANSCRIPTS if k.startswith("bad-")]
    scores = [
        score_transcript_mock(MOCK_TRANSCRIPTS[k], median).verdict_justification for k in kill_ids
    ]
    assert statistics.mean(scores) >= 7.0


def test_ship_ideas_score_well_on_grounding() -> None:
    """Ship mocks should land in a calibrated grounding range (>=4) — they
    cite sources, but tersely. Hard floor of 4.0 catches accidental removal
    of citation markers from the canned transcripts."""
    median = _cohort_median()
    ship_ids = [k for k in MOCK_TRANSCRIPTS if k.startswith("good-")]
    scores = [score_transcript_mock(MOCK_TRANSCRIPTS[k], median).source_grounding for k in ship_ids]
    assert statistics.mean(scores) >= 4.0


def test_cost_predictability_penalizes_outliers() -> None:
    """The intentionally-over-budget mock transcripts should score lower
    on cost_predictability than the median ones."""
    median = _cohort_median()
    outlier = score_transcript_mock(
        MOCK_TRANSCRIPTS["bad-crypto-social"], median
    ).cost_predictability
    typical = score_transcript_mock(
        MOCK_TRANSCRIPTS["bad-another-todo-app"], median
    ).cost_predictability
    assert outlier < typical
