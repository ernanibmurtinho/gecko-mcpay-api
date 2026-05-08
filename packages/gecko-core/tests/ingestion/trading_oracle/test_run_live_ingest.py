from decimal import Decimal

from gecko_core.ingestion.trading_oracle.run_live_ingest import (
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
