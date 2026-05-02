"""S20-VERDICT-PARSE-REGRESSION-01 — pin the 6 D3 parse-failure transcripts.

Background. The 2026-05-02 D3 holdout-live baseline (commit b96a516,
`tests/eval/live_runs/2026-05-02-s19-d3-postcutover-baseline.json`) regressed
from S17's perfect verdict-accuracy to 0.88. All 6 misses were
`actual_verdict=unknown` — pure parse failures, no polarity flips.

The drift pattern. Post-cutover, the judge agent's prose for these 6 ideas
omits the `Verdict:` / `Final verdict:` line entirely and ends with a
Scoper-shaped `V1_FEASIBLE_IN_4_DAYS: yes|no` token instead. The legacy
parser had no fallback for this and returned "unknown".

The fix in `tests/eval/rubric.py::extract_verdict` (mirrored in
`packages/gecko-core/.../flywheel/__init__.py::extract_verdict`) adds a
last-ditch heuristic: when no canonical verdict line is present, scan for a
`V1_FEASIBLE_IN_4_DAYS: yes|no` token and map yes→ship / no→kill. The same
fallback is added to `extract_verdict_v2` mapping yes→GO / no→PIVOT.

This test pins each of the 6 drifted transcripts so a future prompt
revert (or another shape change) can't silently re-break parsing.

NOTE for the upcoming KILL-verdict ticket (S20 #2): the regex set here
intentionally covers only GO/REFINE/PIVOT (+ legacy SHIP/BUILD/KILL
spellings). Do NOT bake a fifth token expectation in here — when KILL
re-enters as a distinct fifth bucket, the rubric vocab change happens
in `_V2_VERDICT_RE` / `_V2_NORMALIZE` and this test should be revisited
to confirm the V1_FEASIBLE fallback maps "no" to the right post-KILL
bucket (probably KILL, not PIVOT).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.eval.rubric import extract_verdict, extract_verdict_v2

_REPO_ROOT = Path(__file__).resolve().parents[1]
_LIVE_RUNS = _REPO_ROOT / "tests" / "eval" / "live_runs"


# (suite, idea_id, expected_v1, expected_v2). Pulled from the D3 baseline
# JSON: ideas where actual_verdict == "unknown" but expected_verdict is set.
_REGRESSED_IDEAS: list[tuple[str, str, str, str]] = [
    ("general", "good-mcp-airtable-typed", "ship", "GO"),
    ("crypto", "crypto-ship-defi-vault-rebalance", "ship", "GO"),
    ("crypto", "crypto-kill-eth-dex-aggregator", "kill", "PIVOT"),
    ("saas", "saas-ship-mcp-postgres-rls-tester", "ship", "GO"),
    ("saas", "saas-ship-deposition-summarizer", "ship", "GO"),
    ("saas", "saas-ship-mcp-datadog-explainer", "ship", "GO"),
]


def _load_judge_prose(suite: str, idea_id: str) -> str:
    path = _LIVE_RUNS / f"2026-05-02-{suite}-transcripts" / f"{idea_id}.json"
    with path.open() as fh:
        data = json.load(fh)
    return data["judge_prose"]


@pytest.mark.parametrize(
    "suite,idea_id,expected_v1,expected_v2",
    _REGRESSED_IDEAS,
    ids=[i[1] for i in _REGRESSED_IDEAS],
)
def test_d3_drift_v1_parses(suite: str, idea_id: str, expected_v1: str, expected_v2: str) -> None:
    prose = _load_judge_prose(suite, idea_id)
    actual = extract_verdict(prose)
    assert actual != "unknown", (
        f"{idea_id}: parser still returns unknown for D3-drift prose; "
        f"V1_FEASIBLE_IN_4_DAYS fallback regressed."
    )
    assert actual == expected_v1, f"{idea_id}: expected v1={expected_v1!r} got {actual!r}"


@pytest.mark.parametrize(
    "suite,idea_id,expected_v1,expected_v2",
    _REGRESSED_IDEAS,
    ids=[i[1] for i in _REGRESSED_IDEAS],
)
def test_d3_drift_v2_parses(suite: str, idea_id: str, expected_v1: str, expected_v2: str) -> None:
    prose = _load_judge_prose(suite, idea_id)
    actual = extract_verdict_v2(prose)
    assert actual != "UNKNOWN", f"{idea_id}: v2 parser still returns UNKNOWN for D3-drift prose."
    assert actual == expected_v2, f"{idea_id}: expected v2={expected_v2!r} got {actual!r}"


def test_v1_feasible_yes_maps_to_ship_when_no_canonical_verdict() -> None:
    """Synthetic guard: V1_FEASIBLE: yes alone should yield ship/GO."""
    prose = "Some scoper prose.\n\nV1_FEASIBLE_IN_4_DAYS: yes\n\nWrap-up sentence."
    assert extract_verdict(prose) == "ship"
    assert extract_verdict_v2(prose) == "GO"


def test_v1_feasible_no_maps_to_kill() -> None:
    """Synthetic guard: V1_FEASIBLE: no should yield kill/PIVOT."""
    prose = "**V1_FEASIBLE_IN_4_DAYS**: No\n\nNot feasible."
    assert extract_verdict(prose) == "kill"
    assert extract_verdict_v2(prose) == "PIVOT"


def test_canonical_verdict_takes_precedence_over_v1_feasible() -> None:
    """Don't let the fallback over-fire when the canonical line IS present."""
    prose = (
        "Verdict: KILL — saturated market.\n"
        "V1_FEASIBLE_IN_4_DAYS: yes\n"  # contradicts; canonical wins
        "Final verdict: PIVOT\n"
    )
    # Canonical "Verdict: KILL" wins for v1 (legacy parser).
    assert extract_verdict(prose) == "kill"
    # v2 prefers "Final verdict: PIVOT" line.
    assert extract_verdict_v2(prose) == "PIVOT"
