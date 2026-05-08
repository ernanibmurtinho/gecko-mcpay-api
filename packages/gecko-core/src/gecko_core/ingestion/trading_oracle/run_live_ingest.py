"""Trading-oracle live ingest orchestrator.

Two surfaces:
    - plan_ingest(listings, cap_usd) -> IngestPlan   (pure, deterministic, tested)
    - execute_plan(plan, *, ...)                     (Task 6: wires LiveX402Client + Mongo)

Splitting these means we prove the planner is correct before any USDC
moves. The live execute call lands in Task 6 once the buyer wallet is
funded.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
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


@dataclass(frozen=True)
class IngestReport:
    spent_usd: Decimal
    chunks_written: int
    failures: tuple[str, ...]


async def execute_plan(
    plan: IngestPlan,
    *,
    charge_and_fetch: Callable[[PlannedCall], Awaitable[Mapping[str, Any]]],
    write_chunk: Callable[..., Awaitable[None]],
    vertical: str,
) -> IngestReport:
    """Execute a planned ingest. DI'd for testing.

    `charge_and_fetch(call)` performs the live x402 call.
    `write_chunk(...)` writes one chunk to MongoDB with freshness_tier="daily".
    """
    written = 0
    failures: list[str] = []
    spent = Decimal("0")
    for call in plan.calls:
        try:
            response = await charge_and_fetch(call)
            text = response.get("body") if isinstance(response, Mapping) else None
            if not isinstance(text, str) or not text.strip():
                failures.append(f"{call.name}: empty body")
                continue
            await write_chunk(
                text=text,
                provider_kind=call.listing.get("provider_kind", "paysh_live"),
                vertical=vertical,
                freshness_tier="daily",
                source_url=call.listing.get("fqn", ""),
            )
            written += 1
            spent += call.price_usd
        except Exception as exc:
            failures.append(f"{call.name}: {type(exc).__name__}: {exc}")
    return IngestReport(spent_usd=spent, chunks_written=written, failures=tuple(failures))
