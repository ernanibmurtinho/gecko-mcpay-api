"""Tests for the routing matrix + budget enforcement (S3-05).

The matrix is data, so most assertions are direct table reads. The
budget-cap downshift behavior is tested via the private `_select_model`
helper to keep this file network-free (no LLM calls, no x402 stub).
"""

from __future__ import annotations

import pytest
from gecko_core.routing import _select_model
from gecko_core.routing.costs import (
    MODEL_PRICING,
    estimate_cost_usd,
    estimate_tokens,
    price_for,
)
from gecko_core.routing.matrix import (
    ROUTING_MATRIX,
    candidate_models,
    pick_model,
)


def test_matrix_has_all_five_task_hints() -> None:
    assert set(ROUTING_MATRIX) == {
        "reasoning",
        "code",
        "extraction",
        "summary",
        "default",
    }


@pytest.mark.parametrize(
    ("task_hint", "default_model", "premium_model"),
    [
        ("reasoning", "gpt-4o", "claude-sonnet-4-6"),
        ("code", "claude-sonnet-4-6", "claude-opus-4-7"),
        ("extraction", "gpt-4o-mini", "gpt-4o"),
        ("summary", "gpt-4o-mini", "gpt-4o"),
        ("default", "gpt-4o-mini", "gpt-4o"),
    ],
)
def test_pick_model_matches_spec(task_hint: str, default_model: str, premium_model: str) -> None:
    assert pick_model(task_hint=task_hint, prefer_premium=False) == default_model  # type: ignore[arg-type]
    assert pick_model(task_hint=task_hint, prefer_premium=True) == premium_model  # type: ignore[arg-type]


def test_candidate_models_returns_preferred_first() -> None:
    cands = candidate_models(task_hint="code", prefer_premium=False)
    assert cands[0] == "claude-sonnet-4-6"
    assert "claude-opus-4-7" in cands


def test_candidate_models_with_premium_first() -> None:
    cands = candidate_models(task_hint="code", prefer_premium=True)
    assert cands[0] == "claude-opus-4-7"
    assert "claude-sonnet-4-6" in cands


def test_every_matrix_model_has_a_price_entry() -> None:
    # Guards against pricing drift: any model that lands in the matrix
    # must be priced, otherwise estimate_cost_usd raises at runtime.
    for entry in ROUTING_MATRIX.values():
        assert entry["default"] in MODEL_PRICING
        assert entry["premium"] in MODEL_PRICING


def test_price_for_unknown_model_raises() -> None:
    with pytest.raises(KeyError):
        price_for("model-that-does-not-exist")


def test_estimate_tokens_handles_empty_string() -> None:
    assert estimate_tokens("") == 0


def test_estimate_tokens_nonempty_returns_positive() -> None:
    assert estimate_tokens("hello world") > 0


def test_estimate_cost_scales_linearly() -> None:
    a = estimate_cost_usd("gpt-4o-mini", tokens_in=1000, tokens_out=1000)
    b = estimate_cost_usd("gpt-4o-mini", tokens_in=2000, tokens_out=2000)
    assert b == pytest.approx(2 * a)


def test_select_model_picks_preferred_when_budget_allows() -> None:
    # 100 in / 100 out on gpt-4o-mini ≈ negligible cost.
    chosen, cost = _select_model(
        task_hint="default",
        prefer_premium=False,
        estimated_in=100,
        estimated_out=100,
        max_cost_usd=0.05,
    )
    assert chosen == "gpt-4o-mini"
    assert cost <= 0.05


def test_select_model_downshifts_when_premium_too_expensive() -> None:
    # Prefer premium (gpt-4o) with a tight budget — should downshift to
    # gpt-4o-mini as the cheaper candidate.
    chosen, cost = _select_model(
        task_hint="default",
        prefer_premium=True,
        estimated_in=10_000,
        estimated_out=10_000,
        max_cost_usd=0.02,
    )
    assert chosen == "gpt-4o-mini"
    assert cost <= 0.02


def test_select_model_raises_when_all_candidates_over_budget() -> None:
    from gecko_core.routing import RouteBudgetError

    with pytest.raises(RouteBudgetError):
        _select_model(
            task_hint="code",
            prefer_premium=True,
            estimated_in=1_000_000,
            estimated_out=1_000_000,
            max_cost_usd=0.01,
        )


def test_estimate_cost_catalog_backed_for_claude_opus() -> None:
    """Catalog-backed pricing spot-check (S4-MATRIX-01).

    1M input tokens @ $5/1M on the catalog id should yield exactly $5.00.
    """
    cost = estimate_cost_usd("anthropic/claude-opus-4.7", tokens_in=1_000_000, tokens_out=0)
    assert cost == pytest.approx(5.00)


def test_legacy_claude_opus_dash_id_still_resolves() -> None:
    """Legacy pre-Sprint-4 callers using the dash-id keep working."""
    # Legacy hardcoded entry: $15/$75 per 1M.
    cost = estimate_cost_usd("claude-opus-4-7", tokens_in=1_000_000, tokens_out=0)
    assert cost == pytest.approx(15.00)
