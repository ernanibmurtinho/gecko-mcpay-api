"""S14-PULSE-02 — pulse 12-pack credit ledger tests (stub-mode).

The stub ledger is in-memory; tests exercise the credit-pack lifecycle
without Supabase. Live-mode helpers are kept thin and not covered here
(they're integration-tested under the gated `live_cdp` marker once
Sprint 14's Track D contract gate lands).
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from gecko_core.payments.pulse_credits import (
    PULSE_12PACK_CALL_COUNT,
    check_pulse_credits,
    credit_pulse_pack,
    decrement_pulse_credit,
    get_stub_ledger,
)


@pytest.fixture(autouse=True)
def _stub_mode_and_clean() -> Iterator[None]:
    os.environ["X402_MODE"] = "stub"
    get_stub_ledger().reset()
    yield
    get_stub_ledger().reset()


@pytest.mark.asyncio
async def test_check_pulse_credits_zero_for_unknown_wallet() -> None:
    assert await check_pulse_credits(user_wallet="0xUNKNOWN") == 0


@pytest.mark.asyncio
async def test_credit_pulse_pack_seeds_twelve_calls() -> None:
    total = await credit_pulse_pack(user_wallet="WALLET_A", tx_signature="stubtx1")
    assert total == PULSE_12PACK_CALL_COUNT
    assert await check_pulse_credits(user_wallet="WALLET_A") == PULSE_12PACK_CALL_COUNT


@pytest.mark.asyncio
async def test_decrement_pulse_credit_walks_pack_to_zero() -> None:
    """A 12-pack decrements one call at a time across 12 calls; 13th is 0."""
    await credit_pulse_pack(user_wallet="WALLET_B")

    for expected_remaining in range(11, -1, -1):
        new_total = await decrement_pulse_credit(user_wallet="WALLET_B")
        assert new_total == expected_remaining

    # 13th call: nothing left to decrement, total stays at 0.
    assert await decrement_pulse_credit(user_wallet="WALLET_B") == 0
    assert await check_pulse_credits(user_wallet="WALLET_B") == 0


@pytest.mark.asyncio
async def test_decrement_picks_oldest_pack_first() -> None:
    """Two packs on the same wallet — the older pack drains before the newer."""
    await credit_pulse_pack(user_wallet="WALLET_C", call_count=2)
    await credit_pulse_pack(user_wallet="WALLET_C", call_count=2)

    # Total: 4. Drain 2 → oldest pack should be empty, newer pack still has 2.
    await decrement_pulse_credit(user_wallet="WALLET_C")
    await decrement_pulse_credit(user_wallet="WALLET_C")
    assert await check_pulse_credits(user_wallet="WALLET_C") == 2


@pytest.mark.asyncio
async def test_credits_are_isolated_per_wallet() -> None:
    await credit_pulse_pack(user_wallet="WALLET_D")
    await credit_pulse_pack(user_wallet="WALLET_E", call_count=3)

    assert await check_pulse_credits(user_wallet="WALLET_D") == 12
    assert await check_pulse_credits(user_wallet="WALLET_E") == 3
