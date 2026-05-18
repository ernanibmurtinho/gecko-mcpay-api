"""S35-#97/#98 — unit tests for the rubric scorer's run-hardening guards.

Context: the S35 N=30 ship-gate was contaminated — one of 3 pooled
sub-runs was 100% RateLimitError (every panel turn 429'd) yet the
scorer pooled all 10 garbage rows into the N=30 aggregate as if valid
(11/30 rows were rate-limit garbage). See the S35-#95 diagnosis.

These tests cover the *pure* hardening logic only — no LLM, no network,
no panel call, no rubric. Per the over-mocking guidance: synthetic
dict-shaped rows / plain ints; nothing is monkeypatched.

#97 guards under test:
  1. `_row_is_rate_limited` — detect a fully-rate-limited garbage row.
  2. `assess_contamination` — flag a run whose drop-rate exceeds 20%.
  3. `_panel_backoff` — full-jitter exponential, bounded by the cap.

#98 guards under test (the residual holes #97 left open):
  4. `_count_degraded_turns` / `assess_row_degradation` — count degraded
     turns, drop a PARTIALLY degraded row (>1/3 turns 429'd) the way
     #97 drops a fully-degraded one.
  5. `assert_artifact_poolable` / `load_poolable_rows` — reject a
     `contaminated: true` artifact from a multi-run pooling path.
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

import pytest

from tests.eval.scripts.score_defi_trade_rubric import (
    _PANEL_MAX_ATTEMPTS,
    _PANEL_RETRY_CAP_S,
    CONTAMINATION_THRESHOLD,
    PARTIAL_DEGRADATION_THRESHOLD,
    ContaminatedArtifactError,
    _count_degraded_turns,
    _panel_backoff,
    _row_is_rate_limited,
    assert_artifact_poolable,
    assess_contamination,
    assess_row_degradation,
    load_poolable_rows,
)

_RL = "(voice failed: RateLimitError)"
_GOOD = "The macro regime for crypto risk assets appears risk-off ..."


def _panel(*contents: str) -> dict[str, object]:
    """Synthetic serialized panel block — only the keys the detector reads."""
    return {"turns": [{"agent": f"v{i}", "content": c} for i, c in enumerate(contents)]}


# --------------------- guard 1: garbage-row detection ---------------------


def test_all_rate_limited_row_is_detected() -> None:
    """A row whose 7 turns are all RateLimitError stubs is rate-limited —
    this is exactly the S35-#95 garbage shape (10 fixtures x 7 voices)."""
    panel = _panel(*([_RL] * 7))
    assert _row_is_rate_limited(panel) is True


def test_clean_row_is_not_rate_limited() -> None:
    panel = _panel(*([_GOOD] * 7))
    assert _row_is_rate_limited(panel) is False


def test_partial_rate_limited_row_is_kept() -> None:
    """One genuine turn among rate-limited stubs still yields a real (if
    degraded) verdict — keep it, don't drop it."""
    panel = _panel(_RL, _RL, _GOOD, _RL, _RL, _RL, _RL)
    assert _row_is_rate_limited(panel) is False


def test_empty_panel_is_treated_as_degenerate() -> None:
    """A panel with zero turns has no voice output to score — degenerate."""
    assert _row_is_rate_limited({"turns": []}) is True


def test_detector_reads_live_object_shape() -> None:
    """The detector also tolerates a live verdict object (`.turns` of
    objects with `.content`), not just serialized dict rows."""

    class _Turn:
        def __init__(self, content: str) -> None:
            self.content = content

    class _Verdict:
        def __init__(self, contents: list[str]) -> None:
            self.turns = [_Turn(c) for c in contents]

    assert _row_is_rate_limited(_Verdict([_RL] * 7)) is True
    assert _row_is_rate_limited(_Verdict([_GOOD] * 7)) is False


# --------------------- guard 3: contaminated-run flag ---------------------


def test_clean_run_is_not_contaminated() -> None:
    """30 attempted, 0 dropped — a clean ship-gate run, unaffected."""
    result = assess_contamination(n_attempted=30, n_dropped=0)
    assert result["contaminated"] is False
    assert result["n_valid"] == 30
    assert result["drop_rate"] == 0.0


def test_s35_contamination_scenario_is_flagged() -> None:
    """The exact S35 incident: 11/30 rows were rate-limit garbage. That is
    a 36% drop-rate — well over the 20% threshold — so the run is flagged
    contaminated and must not masquerade as a clean N=30."""
    result = assess_contamination(n_attempted=30, n_dropped=11)
    assert result["contaminated"] is True
    assert result["n_valid"] == 19
    assert result["drop_rate"] > CONTAMINATION_THRESHOLD


def test_drop_rate_below_threshold_is_not_contaminated() -> None:
    """A few dropped rows within tolerance (5/30 = 16.7% <= 20%) — the run
    survives, not contaminated, surviving N reported honestly."""
    result = assess_contamination(n_attempted=30, n_dropped=5)
    assert result["contaminated"] is False
    assert result["n_valid"] == 25


def test_threshold_is_strict_greater_than() -> None:
    """Exactly at the 20% threshold is NOT contaminated; just over IS.
    6/30 = 0.20 (boundary, not contaminated); 7/30 ≈ 0.233 (contaminated)."""
    at_bound = assess_contamination(n_attempted=30, n_dropped=6)
    over_bound = assess_contamination(n_attempted=30, n_dropped=7)
    assert at_bound["drop_rate"] == 0.20
    assert at_bound["contaminated"] is False
    assert over_bound["contaminated"] is True


def test_fully_rate_limited_subrun_is_flagged() -> None:
    """The r3 sub-run shape: all 10 of 10 rows dropped — 100% garbage."""
    result = assess_contamination(n_attempted=10, n_dropped=10)
    assert result["contaminated"] is True
    assert result["n_valid"] == 0
    assert result["drop_rate"] == 1.0


def test_zero_attempted_does_not_divide_by_zero() -> None:
    result = assess_contamination(n_attempted=0, n_dropped=0)
    assert result["contaminated"] is False
    assert result["drop_rate"] == 0.0


def test_custom_threshold_is_honored() -> None:
    """A caller can tighten the threshold; 5/30 trips a 10% bar."""
    result = assess_contamination(n_attempted=30, n_dropped=5, threshold=0.10)
    assert result["contaminated"] is True


# --------------------- guard 2: retry backoff shape ---------------------


def test_panel_backoff_is_bounded_by_cap() -> None:
    """Full-jitter draws stay within [0, min(cap, base * 2^(n-1))] for
    every attempt — never negative, never above the cap."""
    rng_state = random.getstate()
    try:
        random.seed(1234)
        for attempt in range(1, _PANEL_MAX_ATTEMPTS + 1):
            for _ in range(200):
                delay = _panel_backoff(attempt)
                assert 0.0 <= delay <= _PANEL_RETRY_CAP_S
    finally:
        random.setstate(rng_state)


def test_panel_backoff_ceiling_grows_then_caps() -> None:
    """The jitter ceiling grows exponentially with attempt then saturates
    at the cap — later attempts can draw larger max delays than early ones."""
    rng_state = random.getstate()
    try:
        random.seed(99)
        # attempt 1 ceiling = base*1 = 2s; sampled max should stay small.
        early = max(_panel_backoff(1) for _ in range(500))
        # attempt 4 ceiling = min(cap, base*8) = min(30, 16) = 16s.
        late = max(_panel_backoff(4) for _ in range(500))
        assert early <= 2.0 + 1e-9
        assert late > early
        assert late <= _PANEL_RETRY_CAP_S
    finally:
        random.setstate(rng_state)


# =============== S35-#98 — partial-degradation guard ===============
# Reuses the `_panel` synthetic-row helper defined for the #97 block.


def test_count_degraded_turns_counts_only_marker_turns() -> None:
    """`_count_degraded_turns` is the per-turn count of RateLimitError
    stubs — distinct from the ALL-turns `_row_is_rate_limited` boolean."""
    panel = _panel(_RL, _GOOD, _RL, _GOOD, _GOOD, _RL, _GOOD)
    assert _count_degraded_turns(panel) == 3


def test_count_degraded_turns_zero_for_clean_panel() -> None:
    assert _count_degraded_turns(_panel(*([_GOOD] * 7))) == 0


def test_three_of_seven_degraded_row_is_kept() -> None:
    """3/7 ≈ 0.429 > 1/3 — over the partial threshold, so DROPPED.

    (The ticket phrases the cases as '3/7 kept, 4/7 dropped'; with a
    7-voice panel and a strict 1/3 bar the actual boundary is 2 kept /
    3 dropped — 2/7≈0.286<=1/3, 3/7≈0.429>1/3. The two paragraphs below
    pin the real boundary explicitly.)"""
    panel = _panel(_RL, _RL, _RL, _GOOD, _GOOD, _GOOD, _GOOD)
    result = assess_row_degradation(panel)
    assert result["degraded_turn_count"] == 3
    assert result["drop"] is True
    assert result["reason"] == "partial_degradation"


def test_two_of_seven_degraded_row_is_kept() -> None:
    """2/7 ≈ 0.286 <= 1/3 — within tolerance, the row is KEPT and scored."""
    panel = _panel(_RL, _RL, _GOOD, _GOOD, _GOOD, _GOOD, _GOOD)
    result = assess_row_degradation(panel)
    assert result["degraded_turn_count"] == 2
    assert result["drop"] is False
    assert result["reason"] == "ok"
    assert result["partial_drop"] is False


def test_four_of_seven_degraded_row_is_dropped() -> None:
    """4/7 ≈ 0.571 > 1/3 — well over the bar, DROPPED as partial garbage.
    This is the ticket's '4/7 panel turns rate-limited' silent-corruption
    case that #97 pooled and #98 now drops."""
    panel = _panel(_RL, _RL, _RL, _RL, _GOOD, _GOOD, _GOOD)
    result = assess_row_degradation(panel)
    assert result["degraded_turn_count"] == 4
    assert result["drop"] is True
    assert result["reason"] == "partial_degradation"
    assert result["fully_rate_limited"] is False


def test_partial_threshold_boundary_is_strict_greater_than() -> None:
    """A 3-turn panel makes the 1/3 boundary exact: 1/3 == threshold is
    NOT dropped (strict >), 2/3 > threshold IS dropped."""
    at_bound = assess_row_degradation(_panel(_RL, _GOOD, _GOOD))
    over_bound = assess_row_degradation(_panel(_RL, _RL, _GOOD))
    assert at_bound["degraded_fraction"] == pytest.approx(1.0 / 3.0, abs=1e-4)
    assert at_bound["drop"] is False
    assert over_bound["drop"] is True
    assert over_bound["reason"] == "partial_degradation"


def test_fully_rate_limited_row_drop_reason_is_distinct() -> None:
    """A fully-rate-limited row still drops, but reason is the #97 case —
    not 'partial_degradation' — so the two paths stay distinguishable."""
    result = assess_row_degradation(_panel(*([_RL] * 7)))
    assert result["drop"] is True
    assert result["fully_rate_limited"] is True
    assert result["reason"] == "fully_rate_limited"
    assert result["partial_drop"] is False


def test_clean_row_is_not_dropped() -> None:
    result = assess_row_degradation(_panel(*([_GOOD] * 7)))
    assert result["drop"] is False
    assert result["degraded_turn_count"] == 0
    assert result["reason"] == "ok"


def test_empty_panel_drops_as_degenerate() -> None:
    result = assess_row_degradation({"turns": []})
    assert result["drop"] is True
    assert result["reason"] == "empty_panel"


def test_degradation_threshold_constant_is_one_third() -> None:
    assert pytest.approx(1.0 / 3.0) == PARTIAL_DEGRADATION_THRESHOLD


# =============== S35-#98 — pooling-script contamination guard ===============


def _artifact(*, contaminated: bool, n_rows: int = 10) -> dict[str, Any]:
    """Synthetic run artifact — only the keys the pooling guard reads."""
    return {
        "contaminated": contaminated,
        "n_attempted": n_rows,
        "n_dropped": 11 if contaminated else 0,
        "contamination": {"drop_rate": 0.36 if contaminated else 0.0},
        "rows": [{"id": f"fx{i}", "scores": {"verdict_accuracy": 1.0}} for i in range(n_rows)],
    }


def test_assert_artifact_poolable_passes_clean_artifact() -> None:
    """A clean artifact (contaminated=false) is poolable — no raise."""
    assert_artifact_poolable(_artifact(contaminated=False), source="clean.json")


def test_assert_artifact_poolable_rejects_contaminated_artifact() -> None:
    """A contaminated artifact raises — its surviving rows must not pool."""
    with pytest.raises(ContaminatedArtifactError, match="contaminated"):
        assert_artifact_poolable(_artifact(contaminated=True), source="dirty.json")


def test_legacy_artifact_without_flag_is_poolable() -> None:
    """An artifact predating #97 lacks the `contaminated` key entirely —
    it cannot be retro-flagged, so it is treated as poolable."""
    legacy = {"rows": [{"id": "fx0", "scores": {}}]}  # no `contaminated` key
    assert_artifact_poolable(legacy, source="legacy.json")


def test_load_poolable_rows_strict_aborts_on_contaminated(tmp_path: Path) -> None:
    """A strict pool given one contaminated artifact among clean ones
    raises — pooling aborts rather than silently mixing in the garbage."""
    clean = tmp_path / "clean.json"
    dirty = tmp_path / "dirty.json"
    clean.write_text(json.dumps(_artifact(contaminated=False)))
    dirty.write_text(json.dumps(_artifact(contaminated=True)))
    with pytest.raises(ContaminatedArtifactError):
        load_poolable_rows([clean, dirty], strict=True)


def test_load_poolable_rows_pools_clean_artifacts(tmp_path: Path) -> None:
    """Two clean artifacts pool into one rows list, each row src-tagged."""
    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    a.write_text(json.dumps(_artifact(contaminated=False, n_rows=10)))
    b.write_text(json.dumps(_artifact(contaminated=False, n_rows=8)))
    pooled, excluded = load_poolable_rows([a, b], strict=True)
    assert len(pooled) == 18
    assert excluded == []
    assert {r["src"] for r in pooled} == {"a.json", "b.json"}


def test_load_poolable_rows_nonstrict_excludes_contaminated(tmp_path: Path) -> None:
    """In non-strict mode a contaminated artifact is hard-warned + EXCLUDED
    (not raised) — the clean remainder still pools, the dirty run named."""
    clean = tmp_path / "clean.json"
    dirty = tmp_path / "dirty.json"
    clean.write_text(json.dumps(_artifact(contaminated=False, n_rows=10)))
    dirty.write_text(json.dumps(_artifact(contaminated=True, n_rows=10)))
    pooled, excluded = load_poolable_rows([clean, dirty], strict=False)
    assert len(pooled) == 10  # only the clean artifact's rows
    assert excluded == ["dirty.json"]
    assert all(r["src"] == "clean.json" for r in pooled)
