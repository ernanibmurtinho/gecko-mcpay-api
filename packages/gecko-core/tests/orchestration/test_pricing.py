"""Pricing table unit tests (S1-04)."""

from __future__ import annotations

from decimal import Decimal


def test_gpt_4o_mini_cost_matches_table() -> None:
    from gecko_core.orchestration.pro.pricing import compute_cost_usd

    # 1M input + 1M output tokens at $0.15 + $0.60 per 1M.
    cost = compute_cost_usd("gpt-4o-mini", 1_000_000, 1_000_000)
    assert cost == Decimal("0.750000")


def test_gpt_4o_cost_matches_table() -> None:
    from gecko_core.orchestration.pro.pricing import compute_cost_usd

    cost = compute_cost_usd("gpt-4o", 1_000_000, 1_000_000)
    assert cost == Decimal("12.500000")


def test_openrouter_passthrough_matches_openai_direct() -> None:
    from gecko_core.orchestration.pro.pricing import compute_cost_usd

    a = compute_cost_usd("gpt-4o-mini", 12_345, 6_789)
    b = compute_cost_usd("openai/gpt-4o-mini", 12_345, 6_789)
    assert a == b


def test_clawrouter_blockrun_auto_midpoint() -> None:
    from gecko_core.orchestration.pro.pricing import compute_cost_usd

    cost = compute_cost_usd("blockrun/auto", 1_000_000, 1_000_000)
    assert cost == Decimal("1.500000")


def test_unknown_model_uses_cheap_fallback() -> None:
    from gecko_core.orchestration.pro.pricing import compute_cost_usd

    cost = compute_cost_usd("unknown/model", 1_000_000, 1_000_000)
    # Fallback is gpt-4o-mini's rate.
    assert cost == Decimal("0.750000")


def test_zero_tokens_yields_zero_cost() -> None:
    from gecko_core.orchestration.pro.pricing import compute_cost_usd

    assert compute_cost_usd("gpt-4o", 0, 0) == Decimal("0.000000")


def test_negative_tokens_clamped_to_zero() -> None:
    from gecko_core.orchestration.pro.pricing import compute_cost_usd

    assert compute_cost_usd("gpt-4o", -10, -5) == Decimal("0.000000")


def test_quantized_to_micro_usd() -> None:
    from gecko_core.orchestration.pro.pricing import compute_cost_usd

    # Tiny token counts produce sub-microcent values; ensure quantization to
    # 6 decimal places (no float drift, no extra precision leaking through).
    cost = compute_cost_usd("gpt-4o-mini", 1, 1)
    assert cost.as_tuple().exponent == -6


def test_known_models_includes_legacy_pricing_anchors() -> None:
    """The legacy ``model_matrix`` was deleted in the LLM-hygiene Commit A.
    The catalog-driven matrix produces ids that aren't all in the small
    ``_PRICE_TABLE``; the table's role is per-agent cost attribution for
    callers that pass router-prefixed names through. Just confirm the legacy
    anchors are still priced — adding new model ids is on the catalog wiring,
    not on the price table."""
    from gecko_core.orchestration.pro.pricing import known_models

    priced = set(known_models())
    for anchor in ("gpt-4o-mini", "gpt-4o", "openai/gpt-4o-mini", "blockrun/auto"):
        assert anchor in priced, f"pricing anchor {anchor!r} missing from price table"
