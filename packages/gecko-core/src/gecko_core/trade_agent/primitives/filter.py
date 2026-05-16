"""Pre-entry filter — token / liquidity / safety gates from spec.filter."""

from __future__ import annotations

from typing import Any

from gecko_core.trade_agent.spec import FilterBlock


def passes_filter(
    spec_filter: FilterBlock | None,
    event: dict[str, Any],
    verdict: dict[str, Any] | None = None,
) -> tuple[bool, str | None]:
    """Return ``(passes, reason_if_blocked)``.

    All filter checks are best-effort: a missing field on the event is
    treated as "data not available" and the check is skipped. This keeps
    the v0.1 surface tolerant of partial hot-path payloads while AIML-2
    wires the full liquidity / holder fetchers.
    """
    if spec_filter is None:
        return True, None

    if spec_filter.min_liquidity_usd is not None:
        liq = event.get("liquidity_usd")
        if liq is not None and float(liq) < spec_filter.min_liquidity_usd:
            return False, "min_liquidity"

    if spec_filter.max_holder_concentration_pct is not None:
        conc = event.get("holder_concentration_pct")
        if conc is not None and float(conc) > spec_filter.max_holder_concentration_pct:
            return False, "holder_concentration"

    if spec_filter.block_honeypot and event.get("is_honeypot"):
        return False, "honeypot"

    if spec_filter.require_oracle_act and (verdict is None or verdict.get("verdict") != "act"):
        return False, "oracle_not_act"

    return True, None


__all__ = ["passes_filter"]
