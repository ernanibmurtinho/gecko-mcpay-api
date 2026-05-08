"""Trading-oracle live ingest orchestrator.

Two surfaces:
    - plan_ingest(listings, cap_usd) -> IngestPlan   (pure, deterministic, tested)
    - execute_plan(plan, *, ...)                     (Task 6: wires LiveX402Client + Mongo)

Splitting these means we prove the planner is correct before any USDC
moves. The live execute call lands in Task 6 once the buyer wallet is
funded.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from gecko_core.ingestion.budget_guard import BudgetGuard
from gecko_core.ingestion.trading_oracle.prompt import is_solana_defi_relevant


@dataclass(frozen=True)
class PlannedCall:
    name: str
    price_usd: Decimal
    listing: Mapping[str, Any]


@dataclass(frozen=True)
class SkippedCall:
    name: str
    reason: str  # "filter:not_solana_defi" | "budget:would_exceed_cap" | "no_price"


@dataclass(frozen=True)
class IngestPlan:
    calls: tuple[PlannedCall, ...]
    skipped: tuple[SkippedCall, ...]
    projected_total_usd: Decimal


def plan_ingest(
    listings: Sequence[Mapping[str, Any]],
    *,
    cap_usd: Decimal,
) -> IngestPlan:
    guard = BudgetGuard(cap_usd=cap_usd)
    calls: list[PlannedCall] = []
    skipped: list[SkippedCall] = []
    for listing in listings:
        name = str(listing.get("name", "<unknown>"))
        price = listing.get("price_usd")
        if not isinstance(price, Decimal):
            skipped.append(SkippedCall(name=name, reason="no_price"))
            continue
        if not is_solana_defi_relevant(listing):
            skipped.append(SkippedCall(name=name, reason="filter:not_solana_defi"))
            continue
        if not guard.can_afford(price):
            skipped.append(SkippedCall(name=name, reason="budget:would_exceed_cap"))
            continue
        guard.charge(price, label=name)
        calls.append(PlannedCall(name=name, price_usd=price, listing=listing))
    return IngestPlan(
        calls=tuple(calls),
        skipped=tuple(skipped),
        projected_total_usd=guard.spent(),
    )
