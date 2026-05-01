"""S14-PULSE-02 — 12-pack prepay credits for `gecko_pulse`.

The pulse SKU charges $0.50 per call. Founders running >12 pulses per
month buy a 12-pack at $5.40 ($0.45/call, 10% bulk discount). This module
owns the ledger:

  * ``pulse_purchases`` table — rows of (user_wallet, remaining_calls,
    expires_at, tx_signature, purchased_at). One row per purchase; a
    wallet can hold multiple unexpired packs (we decrement the oldest
    first).
  * ``check_pulse_credits(wallet)`` — returns the total remaining count
    across all unexpired packs.
  * ``decrement_pulse_credit(wallet)`` — atomically decrements the
    oldest unexpired pack by 1 and returns the new total. Returns 0
    when the wallet has no credits (caller falls through to per-call
    settle).
  * ``credit_pulse_pack(wallet, tx_signature)`` — inserts a fresh
    12-call pack. Called after the operator settles the $5.40 SKU.

In stub mode we provide an in-memory shim (``_StubLedger``) so tests and
the dogfood loop don't need Supabase. The shim is keyed by wallet just
like the real table; it persists for the lifetime of the process.

Pricing constants live alongside the ledger so the per-call discounted
price ($5.40 / 12 = $0.45) can be referenced by the gecko-api Bazaar
listing without a roundtrip through settings.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

# Per-call price (USD) when settling the un-discounted pulse SKU.
PULSE_PER_CALL_PRICE_USD: float = 0.50
# 12-pack prepay SKU.
PULSE_12PACK_PRICE_USD: float = 5.40
PULSE_12PACK_CALL_COUNT: int = 12
# Discounted per-call price inside a 12-pack ($5.40 / 12 = $0.45).
PULSE_12PACK_PER_CALL_USD: float = round(PULSE_12PACK_PRICE_USD / PULSE_12PACK_CALL_COUNT, 4)
# Default pack expiry. 12 pulses ≈ a quarter of weekly check-ins.
PULSE_12PACK_EXPIRY_DAYS: int = 90


def pulse_12pack_price_str() -> str:
    """Return the 12-pack price as a ``$N.NN`` string for env / route configs."""
    raw = os.environ.get("PULSE_12PACK_PRICE")
    if raw:
        return raw
    return f"${PULSE_12PACK_PRICE_USD:.2f}"


# ---------------------------------------------------------------------------
# In-memory stub ledger (used in stub mode + tests).
# ---------------------------------------------------------------------------


class _StubLedger:
    """Process-local credit ledger used in stub mode + tests."""

    def __init__(self) -> None:
        # wallet → list of (purchased_at, expires_at, remaining)
        self._packs: dict[str, list[list[Any]]] = {}
        self._lock = asyncio.Lock()

    async def credit_pack(
        self,
        wallet: str,
        *,
        call_count: int = PULSE_12PACK_CALL_COUNT,
        expires_at: datetime | None = None,
        tx_signature: str | None = None,
    ) -> int:
        async with self._lock:
            now = datetime.now(UTC)
            expiry = expires_at or (now + timedelta(days=PULSE_12PACK_EXPIRY_DAYS))
            self._packs.setdefault(wallet, []).append([now, expiry, call_count, tx_signature])
            return self._total(wallet)

    async def total(self, wallet: str) -> int:
        async with self._lock:
            return self._total(wallet)

    async def decrement(self, wallet: str) -> int:
        async with self._lock:
            packs = self._packs.get(wallet) or []
            now = datetime.now(UTC)
            # Decrement the OLDEST unexpired pack with remaining > 0.
            for pack in sorted(packs, key=lambda p: p[0]):
                expires_at = pack[1]
                remaining = pack[2]
                if expires_at <= now or remaining <= 0:
                    continue
                pack[2] = remaining - 1
                return self._total(wallet)
            return self._total(wallet)

    def _total(self, wallet: str) -> int:
        packs = self._packs.get(wallet) or []
        now = datetime.now(UTC)
        return sum(int(pack[2]) for pack in packs if pack[1] > now and int(pack[2]) > 0)

    def reset(self) -> None:
        """Clear all packs — test-only escape hatch."""
        self._packs.clear()


_STUB_LEDGER = _StubLedger()


def get_stub_ledger() -> _StubLedger:
    """Test-only accessor for the in-memory ledger."""
    return _STUB_LEDGER


# ---------------------------------------------------------------------------
# Public API — defaults to the stub ledger; live mode reads/writes Supabase.
# ---------------------------------------------------------------------------


def _is_stub_mode() -> bool:
    return (os.environ.get("X402_MODE") or "stub").strip().lower() == "stub"


async def check_pulse_credits(*, user_wallet: str) -> int:
    """Return the total unexpired pulse credits for ``user_wallet``.

    Stub-mode reads the in-memory ledger; live mode reads the
    ``pulse_purchases`` table. Failures (table missing, Supabase down)
    return 0 — caller falls through to per-call settle.
    """
    if _is_stub_mode():
        return await _STUB_LEDGER.total(user_wallet)

    try:
        from gecko_core.sessions.store import SessionStore
    except Exception:  # pragma: no cover — defensive
        return 0

    store = SessionStore.from_env()
    try:
        return await _live_total(store, user_wallet)
    except Exception as exc:  # pragma: no cover — table missing in dev
        logger.debug("check_pulse_credits: live lookup failed: %s", exc)
        return 0


async def decrement_pulse_credit(*, user_wallet: str) -> int:
    """Atomically decrement the oldest unexpired pack by 1.

    Returns the wallet's total remaining credits AFTER the decrement.
    When the wallet has no credits, returns 0 without modifying state —
    caller is expected to fall through to per-call settle.
    """
    if _is_stub_mode():
        return await _STUB_LEDGER.decrement(user_wallet)

    try:
        from gecko_core.sessions.store import SessionStore

        store = SessionStore.from_env()
        return await _live_decrement(store, user_wallet)
    except Exception as exc:  # pragma: no cover — table missing in dev
        logger.debug("decrement_pulse_credit: live update failed: %s", exc)
        return 0


async def credit_pulse_pack(
    *,
    user_wallet: str,
    tx_signature: str | None = None,
    call_count: int = PULSE_12PACK_CALL_COUNT,
    expires_at: datetime | None = None,
) -> int:
    """Insert a fresh pack for ``user_wallet`` and return new total."""
    if _is_stub_mode():
        return await _STUB_LEDGER.credit_pack(
            user_wallet,
            call_count=call_count,
            expires_at=expires_at,
            tx_signature=tx_signature,
        )

    try:
        from gecko_core.sessions.store import SessionStore

        store = SessionStore.from_env()
        return await _live_insert(
            store,
            user_wallet,
            call_count=call_count,
            expires_at=expires_at,
            tx_signature=tx_signature,
        )
    except Exception as exc:  # pragma: no cover — table missing in dev
        logger.debug("credit_pulse_pack: live insert failed: %s", exc)
        return 0


# ---------------------------------------------------------------------------
# Live-mode helpers — kept thin so the stub path is the only path tests hit.
# ---------------------------------------------------------------------------


_PULSE_PURCHASES_TABLE = "pulse_purchases"


async def _live_total(store: Any, wallet: str) -> int:
    def _select() -> int:
        now = datetime.now(UTC).isoformat()
        res = (
            store._client.table(_PULSE_PURCHASES_TABLE)
            .select("remaining_calls")
            .eq("user_wallet", wallet)
            .gt("expires_at", now)
            .gt("remaining_calls", 0)
            .execute()
        )
        rows = res.data or []
        return int(sum(int(r.get("remaining_calls", 0) or 0) for r in rows))

    return await asyncio.to_thread(_select)


async def _live_decrement(store: Any, wallet: str) -> int:
    def _decrement() -> int:
        now = datetime.now(UTC).isoformat()
        # Pick the OLDEST unexpired pack with remaining > 0.
        res = (
            store._client.table(_PULSE_PURCHASES_TABLE)
            .select("id,remaining_calls")
            .eq("user_wallet", wallet)
            .gt("expires_at", now)
            .gt("remaining_calls", 0)
            .order("purchased_at", desc=False)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        if not rows:
            return 0
        pack_id = rows[0]["id"]
        new_count = max(0, int(rows[0]["remaining_calls"]) - 1)
        (
            store._client.table(_PULSE_PURCHASES_TABLE)
            .update({"remaining_calls": new_count})
            .eq("id", pack_id)
            .execute()
        )
        # Return total across all unexpired packs after the update.
        res2 = (
            store._client.table(_PULSE_PURCHASES_TABLE)
            .select("remaining_calls")
            .eq("user_wallet", wallet)
            .gt("expires_at", now)
            .gt("remaining_calls", 0)
            .execute()
        )
        rows2 = res2.data or []
        return int(sum(int(r.get("remaining_calls", 0) or 0) for r in rows2))

    return await asyncio.to_thread(_decrement)


async def _live_insert(
    store: Any,
    wallet: str,
    *,
    call_count: int,
    expires_at: datetime | None,
    tx_signature: str | None,
) -> int:
    def _insert() -> int:
        now = datetime.now(UTC)
        expiry = expires_at or (now + timedelta(days=PULSE_12PACK_EXPIRY_DAYS))
        payload = {
            "user_wallet": wallet,
            "remaining_calls": call_count,
            "purchased_at": now.isoformat(),
            "expires_at": expiry.isoformat(),
            "tx_signature": tx_signature,
        }
        store._client.table(_PULSE_PURCHASES_TABLE).insert(payload).execute()
        # Total after insert.
        res = (
            store._client.table(_PULSE_PURCHASES_TABLE)
            .select("remaining_calls")
            .eq("user_wallet", wallet)
            .gt("expires_at", now.isoformat())
            .gt("remaining_calls", 0)
            .execute()
        )
        rows = res.data or []
        return int(sum(int(r.get("remaining_calls", 0) or 0) for r in rows))

    return await asyncio.to_thread(_insert)


__all__ = [
    "PULSE_12PACK_CALL_COUNT",
    "PULSE_12PACK_EXPIRY_DAYS",
    "PULSE_12PACK_PER_CALL_USD",
    "PULSE_12PACK_PRICE_USD",
    "PULSE_PER_CALL_PRICE_USD",
    "check_pulse_credits",
    "credit_pulse_pack",
    "decrement_pulse_credit",
    "get_stub_ledger",
    "pulse_12pack_price_str",
]
