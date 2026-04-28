"""Per-model price table for Pro-tier per-agent cost attribution.

Prices are USD per 1M tokens (input / output), matching the units providers
publish. ``compute_cost_usd`` returns a Decimal quantized to micro-USD so
session_costs rows survive aggregation without float drift.

The table is intentionally small — just the models the per-agent matrix
(``router.model_matrix``) can produce. Add a new entry when the matrix
expands. OpenRouter passthrough uses the OpenAI direct rate; ClawRouter's
``blockrun/auto`` is a midpoint placeholder until the proxy reports
upstream-provider cost in response headers.
"""

from __future__ import annotations

from decimal import Decimal

_PER_MILLION = Decimal(1_000_000)

# (input_per_1M, output_per_1M). Decimals so arithmetic is exact.
_PRICE_TABLE: dict[str, tuple[Decimal, Decimal]] = {
    # OpenAI direct ----------------------------------------------------------
    "gpt-4o-mini": (Decimal("0.15"), Decimal("0.60")),
    "gpt-4o": (Decimal("2.50"), Decimal("10.00")),
    # OpenRouter prefixes — passthrough, same rates as OpenAI direct. (We
    # ignore OpenRouter's ~5% margin; reconciliation against invoices is a
    # separate accounting concern, not a per-turn ledger concern.)
    "openai/gpt-4o-mini": (Decimal("0.15"), Decimal("0.60")),
    "openai/gpt-4o": (Decimal("2.50"), Decimal("10.00")),
    # ClawRouter — auto-routed; midpoint until the proxy exposes actuals.
    "blockrun/auto": (Decimal("0.30"), Decimal("1.20")),
}

# Fallback when the model string is unknown. We pick the cheapest model's
# rate so unknown routes never inflate the per-agent cost rollup.
_FALLBACK = (Decimal("0.15"), Decimal("0.60"))

_MICRO = Decimal("0.000001")


def compute_cost_usd(model: str, tokens_in: int, tokens_out: int) -> Decimal:
    """Compute USD cost for a single LLM call.

    Returns a Decimal quantized to 6 decimal places. Negative token counts
    are clamped to zero — defensive only; AG2 should never emit negatives.
    """
    rate_in, rate_out = _PRICE_TABLE.get(model, _FALLBACK)
    safe_in = max(int(tokens_in), 0)
    safe_out = max(int(tokens_out), 0)
    cost = (rate_in * safe_in + rate_out * safe_out) / _PER_MILLION
    return cost.quantize(_MICRO)


def known_models() -> list[str]:
    """Return the list of model strings priced exactly (sorted, deterministic)."""
    return sorted(_PRICE_TABLE.keys())


__all__ = ["compute_cost_usd", "known_models"]
