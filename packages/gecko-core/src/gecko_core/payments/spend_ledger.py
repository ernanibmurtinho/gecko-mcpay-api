"""Outbound x402 spend ledger (S16-BAZAAR-CONSUMER-02).

Postgres-backed audit + budget gate for the Bazaar buyer path. Pre-flight
checks (``would_exceed_daily``) keep a single ``bb research`` invocation
from blowing past ``GECKO_BAZAAR_DAILY_USD_CAP``; the same table also
catches concurrent invocations because the daily aggregate is the *sum
of rows*, not a per-process counter.

Distinct from ``gecko_core.sources.twitsh_circuit`` (S14-TWITSH-05),
which is a JSON-on-disk per-host counter. The Bazaar path needs a server
truth — this is real treasury USDC leaving the wallet, and a JSON file
on a CLI host can't defend the API service that runs in parallel from
the same wallet.

Service-role only. The table has RLS enabled with deny-all to anon
(see ``20260501150000_bazaar_spend_ledger.sql``); never wire this
client through ``gecko-mcpay-app``.

Pattern B (CLAUDE.md): the live ledger writes are exercised by the
recorded-fixture contract test in S16-BAZAAR-CONSUMER-04. Stub mode
records synthetic ``tx_hash="stub-..."`` rows so dev runs reflect
realistic spend in ``bb doctor``.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

from gecko_core.models import PaymentReceipt
from gecko_core.payments.modes import ConsumerMode

logger = logging.getLogger(__name__)


# Default daily cap (USD). Aligned with the ticket's ``$5`` default.
DEFAULT_DAILY_USD_CAP: Decimal = Decimal("5.00")

# The ledger table. Single source of truth for the table name; the doctor
# row + tests reference this constant rather than hard-coding the string.
TABLE_NAME = "bazaar_spend_ledger"


def _utc_midnight_iso() -> str:
    """ISO-8601 timestamp at the most recent UTC midnight.

    Used by both ``daily_total_usd`` and ``would_exceed_daily`` so the
    rolling window is exactly the calendar day in UTC, matching the env
    contract documented on ``GECKO_BAZAAR_DAILY_USD_CAP``.
    """
    today = datetime.now(UTC).date()
    midnight = datetime.combine(today, time.min, tzinfo=UTC)
    return midnight.isoformat()


class BazaarSpendLedger:
    """Async wrapper over the ``bazaar_spend_ledger`` table.

    All methods are best-effort on the *audit* side and strict on the
    *gate* side: ``record()`` swallows write errors (the chain already
    settled — the ledger is auditing, not authorizing), but
    ``would_exceed_daily()`` re-raises read errors so callers can choose
    whether to fail-open or fail-closed. Today the provider fails-open
    (degrades the source) on the read branch as well — losing the gate
    must not stall the rest of the pipeline.
    """

    def __init__(self, client: Any) -> None:
        # ``Any`` rather than the concrete supabase ``Client`` so unit
        # tests can pass a minimal fake without importing supabase.
        self._client = client

    @classmethod
    def from_env(cls) -> BazaarSpendLedger:
        """Build a ledger using the standard service-role factory.

        Server-side only. Never call this from gecko-mcpay-app.
        """
        from gecko_core.db import create_supabase_client

        return cls(create_supabase_client())

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    async def record(
        self,
        *,
        session_id: UUID,
        resource_url: str,
        amount_usd: Decimal,
        receipt: PaymentReceipt,
        mode: ConsumerMode,
    ) -> None:
        """Persist one row of outbound spend.

        Idempotency: the migration's partial unique index on ``tx_hash``
        rejects duplicate live tx hashes; we map the resulting upstream
        error onto a debug log + swallow. Stub-mode rows (tx_hash
        ``stub-...``) are excluded from the unique index so dev replays
        don't conflict.

        Failure policy: ``record`` is *audit*. If the chain settled but
        the ledger write fails, that's a partial inconsistency we want
        loud in logs but **must not** unwind the payment — the funds are
        already gone.
        """
        tx_hash = (
            receipt.get("tx_signature")
            if isinstance(receipt, dict)
            else getattr(receipt, "tx_signature", None)
        )
        network = (
            receipt.get("network") if isinstance(receipt, dict) else getattr(receipt, "network", "")
        ) or ""

        row: dict[str, Any] = {
            "session_id": str(session_id),
            "resource_url": resource_url,
            "amount_usd": str(amount_usd),
            "tx_hash": tx_hash,
            "network": network,
            "consumer_mode": mode,
        }

        def _insert() -> None:
            self._client.table(TABLE_NAME).insert(row).execute()

        try:
            await asyncio.to_thread(_insert)
        except Exception as exc:
            logger.warning(
                "bazaar_spend_ledger: record failed for session=%s url=%s tx=%s: %s",
                session_id,
                resource_url,
                tx_hash,
                exc,
            )

    # ------------------------------------------------------------------
    # Reads / aggregates
    # ------------------------------------------------------------------

    async def daily_total_usd(self, *, since_utc_midnight: bool = True) -> Decimal:
        """Sum today's recorded spend in USD.

        ``since_utc_midnight`` exists as a kwarg for symmetry with the
        ticket spec; only the True branch is implemented. PostgREST has
        no SUM aggregate without an RPC, so we pull the column over the
        windowed slice and sum in Python — the table is small (one row
        per outbound buy, capped daily).
        """
        if not since_utc_midnight:
            raise NotImplementedError(
                "non-midnight windows are not implemented; the ticket "
                "scope is the UTC calendar-day window."
            )
        since = _utc_midnight_iso()

        def _select() -> list[dict[str, Any]]:
            res = (
                self._client.table(TABLE_NAME)
                .select("amount_usd")
                .gte("created_at", since)
                .execute()
            )
            return cast(list[dict[str, Any]], res.data or [])

        rows = await asyncio.to_thread(_select)
        total = Decimal("0")
        for r in rows:
            raw = r.get("amount_usd")
            if raw is None:
                continue
            try:
                total += Decimal(str(raw))
            except Exception:
                logger.debug("bazaar_spend_ledger: dropping unparseable amount %r", raw)
        return total

    async def session_total_usd(self, session_id: UUID) -> Decimal:
        """Sum the spend belonging to one research session."""

        def _select() -> list[dict[str, Any]]:
            res = (
                self._client.table(TABLE_NAME)
                .select("amount_usd")
                .eq("session_id", str(session_id))
                .execute()
            )
            return cast(list[dict[str, Any]], res.data or [])

        rows = await asyncio.to_thread(_select)
        total = Decimal("0")
        for r in rows:
            raw = r.get("amount_usd")
            if raw is None:
                continue
            try:
                total += Decimal(str(raw))
            except Exception:
                logger.debug("bazaar_spend_ledger: dropping unparseable amount %r", raw)
        return total

    async def would_exceed_daily(
        self,
        amount_usd: Decimal,
        *,
        daily_cap_usd: Decimal,
    ) -> bool:
        """Pre-flight: would *adding* ``amount_usd`` push today's total past the cap?

        Strict ``>`` against the cap so the boundary value (current +
        amount == cap) is *allowed*. Mirrors the twit.sh circuit
        breaker's ``current + amount > cap`` semantics so the two gates
        feel the same.
        """
        if amount_usd < 0:
            raise ValueError(f"amount_usd must be non-negative; got {amount_usd}")
        current = await self.daily_total_usd()
        return (current + amount_usd) > daily_cap_usd


__all__ = [
    "DEFAULT_DAILY_USD_CAP",
    "TABLE_NAME",
    "BazaarSpendLedger",
]


# ---------------------------------------------------------------------------
# Test/debug helper — never used in production paths.
# ---------------------------------------------------------------------------


def _yesterday_utc_midnight_iso() -> str:
    """Exposed for tests that backfill rows across the UTC midnight boundary.

    Kept private (leading underscore) so it doesn't leak into the public
    surface; the daily-aggregation test in ``tests/payments/`` imports it
    via the underscore name to inject a yesterday-stamped row.
    """
    today = datetime.now(UTC).date()
    midnight = datetime.combine(today, time.min, tzinfo=UTC)
    return (midnight - timedelta(seconds=1)).isoformat()
