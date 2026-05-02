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
async def test_cdp_consumer_pay_is_gated_on_contract_test_ticket() -> None:
    """Intentional: the live wire stays NotImplementedError until the
    recorded-fixture contract test (S16-BAZAAR-CONSUMER-04) lands.

    Per CLAUDE.md Pattern B — no stubbed-but-shipped wire integrations.
    Removing this guard requires the contract test to land first.
    """
    consumer = CDPX402Consumer()
    with pytest.raises(NotImplementedError) as excinfo:
        await consumer.pay(_req("0.01"), max_usd=Decimal("0.10"))
    assert "S16-BAZAAR-CONSUMER-04" in str(excinfo.value)


@pytest.mark.asyncio
async def test_frames_consumer_pay_is_gated_on_contract_test_ticket() -> None:
    consumer = FramesX402Consumer()
    with pytest.raises(NotImplementedError) as excinfo:
        await consumer.pay(_req("0.01"), max_usd=Decimal("0.10"))
    assert "S16-BAZAAR-CONSUMER-04" in str(excinfo.value)


@pytest.mark.asyncio
async def test_live_conformers_enforce_budget_before_raising_notimpl() -> None:
    """When the live body lands, error ordering must stay stable: budget
    cap must reject before any network call. Test that today by asserting
    BudgetExceeded wins over NotImplementedError on the scaffold."""
    for consumer in (CDPX402Consumer(), FramesX402Consumer()):
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
