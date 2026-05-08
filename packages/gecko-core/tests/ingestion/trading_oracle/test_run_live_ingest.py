from decimal import Decimal

import pytest
from gecko_core.ingestion.trading_oracle.run_live_ingest import (
    IngestPlan,
    IngestReport,
    PlannedCall,
    execute_plan,
    plan_ingest,
)


def _listing(name: str, price: float, *, tags=("solana", "defi")):
    return {
        "name": name,
        "description": f"{name} feed",
        "tags": list(tags),
        "price_usd": Decimal(str(price)),
    }


def test_plan_skips_irrelevant():
    listings = [
        _listing("Kamino TVL", 1.0),
        _listing("Hotel Search", 0.5, tags=["travel"]),
    ]
    plan = plan_ingest(listings, cap_usd=Decimal("20.00"))
    assert [c.name for c in plan.calls] == ["Kamino TVL"]


def test_plan_respects_cap():
    listings = [_listing(f"Proto-{i}", 5.0) for i in range(10)]
    plan = plan_ingest(listings, cap_usd=Decimal("20.00"))
    # 5*4 = 20.00; the 5th call would push over.
    assert len(plan.calls) == 4
    assert plan.projected_total_usd == Decimal("20.00")


def test_plan_records_skipped_reason():
    listings = [
        _listing("A", 5.0),
        _listing("B-EVM-only", 5.0, tags=["ethereum", "defi"]),
        _listing("C", 18.0),  # would exceed remaining
    ]
    plan = plan_ingest(listings, cap_usd=Decimal("20.00"))
    assert [c.name for c in plan.calls] == ["A"]
    skipped = {s.name: s.reason for s in plan.skipped}
    assert skipped["B-EVM-only"] == "filter:not_solana_defi"
    assert skipped["C"] == "budget:would_exceed_cap"


def test_plan_skips_listing_without_decimal_price():
    listings = [
        {"name": "Has Float Price", "tags": ["solana", "defi"], "price_usd": 1.0},
        {"name": "Missing Price", "tags": ["solana", "defi"]},
        {"name": "Good", "tags": ["solana", "defi"], "price_usd": Decimal("1.00")},
    ]
    plan = plan_ingest(listings, cap_usd=Decimal("20.00"))
    assert [c.name for c in plan.calls] == ["Good"]
    skipped = {s.name: s.reason for s in plan.skipped}
    assert skipped["Has Float Price"] == "no_price"
    assert skipped["Missing Price"] == "no_price"


@pytest.mark.asyncio
async def test_execute_plan_writes_chunks_with_freshness_daily():
    plan = IngestPlan(
        calls=(
            PlannedCall(
                name="Kamino TVL",
                price_usd=Decimal("1.00"),
                listing={
                    "name": "Kamino TVL",
                    "fqn": "paysh:kamino-tvl",
                    "provider_kind": "paysh_live",
                },
            ),
        ),
        skipped=(),
        projected_total_usd=Decimal("1.00"),
    )

    written: list[dict] = []

    async def fake_charge_and_fetch(call):
        return {"body": "Kamino USDC reserve TVL = 12.3M, APY 8.4%", "fqn": call.listing["fqn"]}

    async def fake_write_chunk(*, text, provider_kind, vertical, freshness_tier, source_url):
        written.append(
            {
                "vertical": vertical,
                "provider_kind": provider_kind,
                "freshness_tier": freshness_tier,
            }
        )

    report = await execute_plan(
        plan,
        charge_and_fetch=fake_charge_and_fetch,
        write_chunk=fake_write_chunk,
        vertical="defi-trading",
    )
    assert isinstance(report, IngestReport)
    assert report.spent_usd == Decimal("1.00")
    assert len(written) == 1
    assert written[0]["freshness_tier"] == "daily"
    assert written[0]["vertical"] == "defi-trading"


@pytest.mark.asyncio
async def test_execute_plan_records_failures_without_aborting_run():
    plan = IngestPlan(
        calls=(
            PlannedCall(
                name="Good",
                price_usd=Decimal("0.50"),
                listing={"name": "Good", "fqn": "p:good", "provider_kind": "paysh_live"},
            ),
            PlannedCall(
                name="Boom",
                price_usd=Decimal("0.50"),
                listing={"name": "Boom", "fqn": "p:boom", "provider_kind": "paysh_live"},
            ),
        ),
        skipped=(),
        projected_total_usd=Decimal("1.00"),
    )

    async def fake_charge_and_fetch(call):
        if call.name == "Boom":
            raise RuntimeError("boom from facilitator")
        return {"body": "ok"}

    async def fake_write_chunk(**_):
        pass

    report = await execute_plan(
        plan,
        charge_and_fetch=fake_charge_and_fetch,
        write_chunk=fake_write_chunk,
        vertical="defi-trading",
    )
    assert report.chunks_written == 1
    assert report.spent_usd == Decimal("0.50")
    assert any("Boom" in f and "RuntimeError" in f for f in report.failures)
