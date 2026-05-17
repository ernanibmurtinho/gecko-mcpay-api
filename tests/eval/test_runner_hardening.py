"""S35-#97 — unit tests for the rubric scorer's run-hardening guards.

Context: the S35 N=30 ship-gate was contaminated — one of 3 pooled
sub-runs was 100% RateLimitError (every panel turn 429'd) yet the
scorer pooled all 10 garbage rows into the N=30 aggregate as if valid
(11/30 rows were rate-limit garbage). See the S35-#95 diagnosis.

These tests cover the *pure* hardening logic only — no LLM, no network,
no panel call, no rubric. Per the over-mocking guidance: synthetic
dict-shaped rows / plain ints; nothing is monkeypatched.

Three guards under test:
  1. `_row_is_rate_limited` — detect a fully-rate-limited garbage row.
  2. `assess_contamination` — flag a run whose drop-rate exceeds 20%.
  3. `_panel_backoff` — full-jitter exponential, bounded by the cap.
"""

from __future__ import annotations

import random

from tests.eval.scripts.score_defi_trade_rubric import (
    _PANEL_MAX_ATTEMPTS,
    _PANEL_RETRY_CAP_S,
    CONTAMINATION_THRESHOLD,
    _panel_backoff,
    _row_is_rate_limited,
    assess_contamination,
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
