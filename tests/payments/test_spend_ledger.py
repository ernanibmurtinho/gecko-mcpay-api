"""S16-BAZAAR-CONSUMER-02 — BazaarSpendLedger unit tests.

Tests the budget gate + daily aggregation. The ledger talks to Supabase
via the standard service-role client; here we substitute a minimal
in-memory fake (mirrors the ``FakeClient`` shape from
``tests/sessions/test_store.py``) so the test stays unit-level.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from gecko_core.models import PaymentReceipt
from gecko_core.payments.spend_ledger import (
    BazaarSpendLedger,
    _yesterday_utc_midnight_iso,
)

# ---------------------------------------------------------------------------
# Fake supabase client — filterable by .gte / .eq on the recorded slice.
# ---------------------------------------------------------------------------


class _FakeQuery:
    """Chainable query that filters an in-memory list of dict rows."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        # Share the parent table's list by reference so insert() persists
        # across .table() calls. ``gte`` / ``eq`` rebind ``self._rows`` to
        # a filtered copy so subsequent chained filters compose without
        # corrupting the parent.
        self._rows = rows
        self._select: list[str] | None = None
        self._inserts: list[dict[str, Any]] = []
        self._mode: str = "select"

    def select(self, cols: str) -> _FakeQuery:
        self._select = [c.strip() for c in cols.split(",")]
        self._mode = "select"
        return self

    def insert(self, payload: dict[str, Any]) -> _FakeQuery:
        self._mode = "insert"
        self._inserts.append(payload)
        # Side-effect: the parent table sees the new row.
        self._rows.append(payload)
        return self

    def gte(self, col: str, value: str) -> _FakeQuery:
        self._rows = [r for r in self._rows if r.get(col, "") >= value]
        return self

    def eq(self, col: str, value: str) -> _FakeQuery:
        self._rows = [r for r in self._rows if str(r.get(col, "")) == str(value)]
        return self

    def execute(self) -> Any:
        resp = MagicMock()
        if self._mode == "insert":
            resp.data = self._inserts
        else:
            cols = self._select or ["*"]
            if cols == ["*"]:
                resp.data = list(self._rows)
            else:
                resp.data = [{c: r.get(c) for c in cols} for r in self._rows]
        return resp


class _FakeClient:
    """Single-table fake. The ledger only ever talks to one table."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def table(self, name: str) -> _FakeQuery:
        return _FakeQuery(self.rows)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _receipt(tx: str | None = "stub-abc123", network: str = "Base") -> PaymentReceipt:
    receipt: PaymentReceipt = {
        "intent_id": "stub-i",
        "status": "success",
        "tx_signature": tx,
        "facilitator_id": "stub-consumer",
        "network": network,
    }
    return receipt


def _today_midnight_iso() -> str:
    today = datetime.now(UTC).date()
    return datetime.combine(today, time.min, tzinfo=UTC).isoformat()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_persists_row_with_receipt_fields() -> None:
    fake = _FakeClient()
    ledger = BazaarSpendLedger(fake)
    sid = uuid4()

    await ledger.record(
        session_id=sid,
        resource_url="https://example.com/api",
        amount_usd=Decimal("0.05"),
        receipt=_receipt(),
        mode="stub",
    )

    assert len(fake.rows) == 1
    row = fake.rows[0]
    assert row["session_id"] == str(sid)
    assert row["resource_url"] == "https://example.com/api"
    assert row["amount_usd"] == "0.05"
    assert row["tx_hash"] == "stub-abc123"
    assert row["network"] == "Base"
    assert row["consumer_mode"] == "stub"


@pytest.mark.asyncio
async def test_daily_total_splits_today_vs_yesterday() -> None:
    """3 rows across UTC midnight: today's two summed, yesterday's excluded."""
    fake = _FakeClient()
    today = _today_midnight_iso()
    yesterday = _yesterday_utc_midnight_iso()
    fake.rows = [
        {"amount_usd": "1.50", "created_at": today, "session_id": "x"},
        {"amount_usd": "0.25", "created_at": today, "session_id": "y"},
        {"amount_usd": "9.99", "created_at": yesterday, "session_id": "z"},
    ]
    ledger = BazaarSpendLedger(fake)
    total = await ledger.daily_total_usd()
    assert total == Decimal("1.75")


@pytest.mark.asyncio
async def test_session_total_only_counts_one_session() -> None:
    fake = _FakeClient()
    sid = uuid4()
    other = uuid4()
    fake.rows = [
        {"amount_usd": "0.10", "session_id": str(sid)},
        {"amount_usd": "0.05", "session_id": str(sid)},
        {"amount_usd": "0.99", "session_id": str(other)},
    ]
    ledger = BazaarSpendLedger(fake)
    assert await ledger.session_total_usd(sid) == Decimal("0.15")


@pytest.mark.asyncio
async def test_would_exceed_daily_at_boundary_allows_4_99() -> None:
    """Default $5 cap: $4.99 is allowed (sum lands exactly at edge)."""
    fake = _FakeClient()
    today = _today_midnight_iso()
    fake.rows = [{"amount_usd": "0.01", "created_at": today, "session_id": "x"}]
    ledger = BazaarSpendLedger(fake)

    # current=0.01, request=4.98 → total=4.99 < 5.00 → allowed (False).
    assert (
        await ledger.would_exceed_daily(Decimal("4.98"), daily_cap_usd=Decimal("5.00"))
    ) is False


@pytest.mark.asyncio
async def test_would_exceed_daily_at_boundary_blocks_5_01() -> None:
    fake = _FakeClient()
    today = _today_midnight_iso()
    fake.rows = [{"amount_usd": "0.01", "created_at": today, "session_id": "x"}]
    ledger = BazaarSpendLedger(fake)

    # current=0.01, request=5.00 → total=5.01 > 5.00 → blocked (True).
    assert (await ledger.would_exceed_daily(Decimal("5.00"), daily_cap_usd=Decimal("5.00"))) is True


@pytest.mark.asyncio
async def test_would_exceed_daily_negative_amount_raises() -> None:
    ledger = BazaarSpendLedger(_FakeClient())
    with pytest.raises(ValueError):
        await ledger.would_exceed_daily(Decimal("-0.01"), daily_cap_usd=Decimal("5.00"))


@pytest.mark.asyncio
async def test_record_swallows_write_errors() -> None:
    """Ledger is audit, not authorization: a failed write must not propagate."""

    class _RaisingClient:
        def table(self, name: str) -> Any:
            raise RuntimeError("supabase exploded")

    ledger = BazaarSpendLedger(_RaisingClient())
    # Must NOT raise.
    await ledger.record(
        session_id=uuid4(),
        resource_url="https://example.com",
        amount_usd=Decimal("0.01"),
        receipt=_receipt(),
        mode="stub",
    )


@pytest.mark.asyncio
async def test_yesterday_iso_is_before_today_midnight() -> None:
    """Sanity check on the test helper: yesterday < today."""
    assert _yesterday_utc_midnight_iso() < _today_midnight_iso()
    # Specifically one second before midnight today.
    today = datetime.now(UTC).date()
    midnight = datetime.combine(today, time.min, tzinfo=UTC)
    assert datetime.fromisoformat(_yesterday_utc_midnight_iso()) == midnight - timedelta(seconds=1)
