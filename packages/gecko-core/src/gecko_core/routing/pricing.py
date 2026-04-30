"""Per-endpoint x Tier pricing snapshot (S6-TIER-01).

Informational view that powers `/pricing` and `bb pricing`. Source of truth
is the curated catalog (`routing.catalog`) plus the `MODEL_PRICING` view —
this module composes both into a flat table:

    {endpoint: {tier_preset: {price_usd, est_latency_ms, model_summary}}}

`price_usd` is x402-charged at the API gate (a constant per endpoint —
see `gecko_api.settings`); `est_latency_ms` and `model_summary` are
heuristic snapshots derived from the curated tier model selection.

This is an informational surface only; no payment flow consults it.
"""

from __future__ import annotations

from typing import TypedDict

from gecko_core.orchestration.advisor.models import PANEL_VOICE_ORDER
from gecko_core.routing.catalog import AgentRole, Tier, models_for_role
from gecko_core.routing.matrix import ROUTING_MATRIX


class TierQuote(TypedDict):
    """One cell of the tier x endpoint pricing matrix."""

    price_usd: float
    est_latency_ms: int
    model_summary: str


# Wall-clock latency budgets per tier. These are heuristic — quality picks
# heavier models (Opus / GPT-5.5) which take longer; free picks
# Nemotron / Poolside which are fast but smaller. Tune in one place.
_TIER_LATENCY_MS: dict[Tier, dict[str, int]] = {
    Tier.quality: {"research": 90_000, "plan": 18_000, "route": 5_000},
    Tier.balanced: {"research": 60_000, "plan": 9_000, "route": 2_000},
    Tier.budget: {"research": 45_000, "plan": 6_000, "route": 1_500},
    Tier.free: {"research": 30_000, "plan": 4_000, "route": 1_000},
}


def _summarize_panel(tier: Tier) -> str:
    """Compact model summary across the 5 panel voices."""
    names: list[str] = []
    for role in PANEL_VOICE_ORDER:
        try:
            entry = models_for_role(role)[tier]
        except KeyError:
            continue
        names.append(entry.name)
    # Dedup while preserving order — many tiers reuse the same model across
    # multiple panel voices.
    seen: set[str] = set()
    unique: list[str] = []
    for n in names:
        if n in seen:
            continue
        seen.add(n)
        unique.append(n)
    if not unique:
        return "(no curated panel for tier)"
    return ", ".join(unique[:3]) + (f" +{len(unique) - 3}" if len(unique) > 3 else "")


def _summarize_research(tier: Tier) -> str:
    """Pro-debate uses analyst/critic/architect/scoper/judge."""
    debate_roles = (
        AgentRole.analyst,
        AgentRole.critic,
        AgentRole.architect,
        AgentRole.scoper,
        AgentRole.judge,
    )
    names: list[str] = []
    for role in debate_roles:
        try:
            entry = models_for_role(role)[tier]
        except KeyError:
            continue
        names.append(entry.name)
    seen: set[str] = set()
    unique: list[str] = []
    for n in names:
        if n in seen:
            continue
        seen.add(n)
        unique.append(n)
    if not unique:
        return "(no curated debate for tier)"
    return ", ".join(unique[:3]) + (f" +{len(unique) - 3}" if len(unique) > 3 else "")


def _summarize_route(tier: Tier) -> str:
    """`/route` exposes the legacy 2-column matrix (default/premium). We
    map quality→premium and everything else→default for the user-facing
    summary so a single string is consistent across tiers.
    """
    matrix = ROUTING_MATRIX.get("default")
    if matrix is None:
        return "(routing matrix unavailable)"
    column: str = "premium" if tier == Tier.quality else "default"
    return str(matrix.get(column) or matrix.get("default") or "(unknown)")


def build_pricing_table(prices_by_endpoint: dict[str, float]) -> dict[str, dict[str, TierQuote]]:
    """Compose the {endpoint: {tier: TierQuote}} table.

    `prices_by_endpoint` maps endpoint logical name → x402-advertised USD
    price (e.g. {"research": 20.0, "plan": 0.25, "route": 0.01}). The
    caller (gecko_api.main) supplies the prices from its Settings so the
    pricing surface stays in lock-step with the actual x402 routes.
    """
    out: dict[str, dict[str, TierQuote]] = {}
    for endpoint, base_price in prices_by_endpoint.items():
        per_tier: dict[str, TierQuote] = {}
        for tier in Tier:
            latency = _TIER_LATENCY_MS.get(tier, {}).get(endpoint, 5_000)
            if endpoint == "research":
                summary = _summarize_research(tier)
            elif endpoint == "plan":
                summary = _summarize_panel(tier)
            elif endpoint == "route":
                summary = _summarize_route(tier)
            else:
                summary = "(unknown endpoint)"
            per_tier[tier.value] = TierQuote(
                price_usd=float(base_price),
                est_latency_ms=int(latency),
                model_summary=summary,
            )
        out[endpoint] = per_tier
    return out


__all__ = ["TierQuote", "build_pricing_table"]
