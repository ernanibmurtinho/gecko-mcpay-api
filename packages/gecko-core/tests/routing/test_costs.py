"""Catalog-backed pricing spot-checks (S4-MATRIX-01).

These complement `test_matrix.py` — focused specifically on `costs.py`
resolving prices through the curated catalog rather than the legacy
hardcoded dict.
"""

from __future__ import annotations

import pytest
from gecko_core.routing.costs import (
    MODEL_PRICING,
    estimate_cost_usd,
    model_pricing_for,
    price_for,
)


def test_catalog_backed_opus_input_only() -> None:
    cost = estimate_cost_usd("anthropic/claude-opus-4.7", tokens_in=1_000_000, tokens_out=0)
    assert cost == pytest.approx(5.00)


def test_catalog_backed_kimi_k26_pricing() -> None:
    in_per_m, out_per_m = model_pricing_for("moonshotai/kimi-k2.6")
    assert in_per_m == pytest.approx(0.7448)
    assert out_per_m == pytest.approx(4.655)


def test_legacy_dash_id_still_works() -> None:
    p = price_for("claude-opus-4-7")
    assert p["input_per_m"] == pytest.approx(15.00)
    assert p["output_per_m"] == pytest.approx(75.00)


def test_unknown_model_raises_keyerror() -> None:
    with pytest.raises(KeyError):
        price_for("absolutely-fake-model-id")


def test_model_pricing_view_exposes_catalog_ids() -> None:
    # The catalog-backed view should expose full ids.
    assert "anthropic/claude-opus-4.7" in MODEL_PRICING
    assert "openai/gpt-5.5" in MODEL_PRICING
    # And the bare suffix forms (so existing legacy calls keep working).
    assert "claude-opus-4.7" in MODEL_PRICING


def test_legacy_gpt_4o_mini_still_priced() -> None:
    p = price_for("gpt-4o-mini")
    assert p["input_per_m"] == pytest.approx(0.15)
    assert p["output_per_m"] == pytest.approx(0.60)
