"""S16-BAZAAR-CONSUMER-01 — X402Consumer Protocol + conformer tests.

Covers:
  * Stub success path (synthetic receipt).
  * BudgetExceeded raised before any settle when caller cap < advertised.
  * BudgetExceeded raised when ``max_usd <= 0``.
  * CDP / frames conformers raise NotImplementedError pinned to
    S16-BAZAAR-CONSUMER-04 (Pattern B — no stubbed-but-shipped wire).
  * Factory dispatches to the right conformer.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from gecko_core.payments.bazaar_discovery import PaymentRequirements
from gecko_core.payments.x402_consumer import (
    BudgetExceeded,
    BudgetExceededError,
    CDPX402Consumer,
    FramesX402Consumer,
    StubX402Consumer,
    X402Consumer,
    resolve_consumer_client,
)


def _req(amount_usd: str = "0.01") -> PaymentRequirements:
    return PaymentRequirements(
        network="Base",
        asset="USDC",
        pay_to="0xabc",
        amount=str(int(Decimal(amount_usd) * (Decimal(10) ** 6))),
        max_amount_required=Decimal(amount_usd),
    )


@pytest.mark.asyncio
async def test_stub_consumer_returns_synthetic_receipt() -> None:
    consumer = StubX402Consumer()
    receipt = await consumer.pay(_req("0.01"), max_usd=Decimal("0.10"))
    assert receipt["status"] == "success"
    assert receipt["facilitator_id"] == "stub-consumer"
    assert receipt["network"] == "Base"
    assert (receipt.get("tx_signature") or "").startswith("stub-")
    assert (receipt["intent_id"]).startswith("stub-")


@pytest.mark.asyncio
async def test_budget_exceeded_when_advertised_above_cap() -> None:
    consumer = StubX402Consumer()
    with pytest.raises(BudgetExceeded):
        await consumer.pay(_req("0.50"), max_usd=Decimal("0.10"))


@pytest.mark.asyncio
async def test_budget_exceeded_when_cap_non_positive() -> None:
    consumer = StubX402Consumer()
    with pytest.raises(BudgetExceeded):
        await consumer.pay(_req("0.01"), max_usd=Decimal("0"))


def test_legacy_alias_is_same_class() -> None:
    # The earlier scaffold exported BudgetExceededError; keep it as an
    # alias so the existing GenericBazaarAdapter import keeps working.
    assert BudgetExceededError is BudgetExceeded


# ---------------------------------------------------------------------------
# Pattern B gate — live conformers must raise until S16-BAZAAR-CONSUMER-04.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cdp_consumer_requires_buyer_key() -> None:
    """S16-BAZAAR-CONSUMER-04 — live body is wired, but it raises a
    typed ``CDPNotConfiguredError`` (not ``NotImplementedError``) when
    ``GECKO_BAZAAR_BUYER_PRIVATE_KEY`` is missing. No stubbed-but-shipped
    fallback — Pattern B."""
    from gecko_core.payments.x402_consumer import CDPNotConfiguredError

    consumer = CDPX402Consumer(payer_private_key=None)
    with pytest.raises(CDPNotConfiguredError) as excinfo:
        await consumer.pay(_req("0.01"), max_usd=Decimal("0.10"))
    assert "GECKO_BAZAAR_BUYER_PRIVATE_KEY" in str(excinfo.value)


@pytest.mark.asyncio
async def test_frames_consumer_pay_is_deferred_with_followup_ticket() -> None:
    """Frames-buyer wiring is deferred — see S16-BAZAAR-CONSUMER-04-FRAMES.
    No Solana $0.01-class endpoint candidate at S16 ship-time."""
    consumer = FramesX402Consumer()
    with pytest.raises(NotImplementedError) as excinfo:
        await consumer.pay(_req("0.01"), max_usd=Decimal("0.10"))
    assert "S16-BAZAAR-CONSUMER-04-FRAMES" in str(excinfo.value)


@pytest.mark.asyncio
async def test_live_conformers_enforce_budget_before_anything_else() -> None:
    """Budget cap rejects before any network call or config check."""
    for consumer in (CDPX402Consumer(payer_private_key=None), FramesX402Consumer()):
        with pytest.raises(BudgetExceeded):
            await consumer.pay(_req("0.50"), max_usd=Decimal("0.10"))


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def test_resolve_consumer_default_is_stub() -> None:
    c = resolve_consumer_client()
    assert isinstance(c, StubX402Consumer)
    assert isinstance(c, X402Consumer)


def test_resolve_consumer_dispatches_by_mode() -> None:
    assert isinstance(resolve_consumer_client("stub"), StubX402Consumer)
    assert isinstance(resolve_consumer_client("cdp"), CDPX402Consumer)
    assert isinstance(resolve_consumer_client("live"), FramesX402Consumer)
