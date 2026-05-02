"""X402Consumer Protocol + conformers (Sprint 16 Track B, outbound buyer).

S16-BAZAAR-CONSUMER-01.

Buyer-side counterpart to seller-side ``X402Client`` (see ``protocol.py``).
Vocabulary is intentionally inverse:

  * ``X402Client.charge(intent)`` — "I am being paid."
  * ``X402Consumer.pay(requirements, max_usd)`` — "I am paying."

The two seams fail independently. ``X402_CONSUMER_MODE`` is separate from
seller-side ``X402_MODE`` (canonical declaration in
``gecko_core.payments.modes``) so stub-buyer + live-seller is a real dev
configuration — don't burn USDC fetching while iterating on the receive
flow.

Pattern B applies (CLAUDE.md): we **do not ship a stubbed-but-shipped
live wire**. ``CDPX402Consumer.pay()`` and ``FramesX402Consumer.pay()``
raise ``NotImplementedError`` until the Pattern C recorded-fixture
contract test (S16-BAZAAR-CONSUMER-04) lands. That ticket is the gate
for filling the bodies in.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol, runtime_checkable
from uuid import uuid4

from gecko_core.models import PaymentReceipt
from gecko_core.payments.bazaar_discovery import PaymentRequirements
from gecko_core.payments.modes import ConsumerMode

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class BudgetExceeded(Exception):
    """Caller's ``max_usd`` cap is below the requirement's price.

    Caller-side defense against advertised-price drift between discovery
    and settle. Raised before any network call.
    """


# Historical alias for the earlier scaffold name. New code uses
# ``BudgetExceeded``.
BudgetExceededError = BudgetExceeded


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class X402Consumer(Protocol):
    """Outbound x402 buyer.

    ``pay`` settles a single ``PaymentRequirements`` (one element of a
    discovered resource's ``accepts[]``) and returns a typed
    ``PaymentReceipt``. ``max_usd`` is the caller-side cap that defends
    against advertised-price drift — if
    ``requirements.max_amount_required > max_usd``, the conformer raises
    :class:`BudgetExceeded` rather than settling.

    Implementations MUST NOT swallow facilitator errors — they bubble up
    typed (mirrors ``X402Client.charge`` policy).
    """

    name: str
    mode: ConsumerMode

    async def pay(
        self,
        requirements: PaymentRequirements,
        *,
        max_usd: Decimal,
    ) -> PaymentReceipt: ...


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _enforce_budget(requirements: PaymentRequirements, max_usd: Decimal) -> None:
    """Caller-side cap. Raises before any network call."""
    if max_usd <= 0:
        raise BudgetExceeded(f"max_usd must be positive; got {max_usd}")
    advertised = requirements.max_amount_required
    if advertised is not None and advertised > max_usd:
        raise BudgetExceeded(f"advertised price {advertised} USD exceeds caller cap {max_usd} USD")


# ---------------------------------------------------------------------------
# Stub
# ---------------------------------------------------------------------------


class StubX402Consumer:
    """No-network synthetic-receipt buyer.

    Default test path. Always succeeds unless ``max_usd <= 0`` or the
    budget cap is breached.
    """

    name = "stub-consumer"
    mode: ConsumerMode = "stub"

    async def pay(
        self,
        requirements: PaymentRequirements,
        *,
        max_usd: Decimal,
    ) -> PaymentReceipt:
        _enforce_budget(requirements, max_usd)
        receipt: PaymentReceipt = {
            "intent_id": f"stub-{uuid4()}",
            "status": "success",
            "tx_signature": f"stub-{uuid4().hex[:16]}",
            "facilitator_id": "stub-consumer",
            "network": requirements.network,
        }
        return receipt


# ---------------------------------------------------------------------------
# Live conformers — scaffold only. Pattern B gate.
# ---------------------------------------------------------------------------


_LIVE_GATE_TICKET = "S16-BAZAAR-CONSUMER-04"
_LIVE_GATE_REASON = (
    f"{_LIVE_GATE_TICKET} — gated on recorded-fixture contract test. "
    "Per CLAUDE.md Pattern B, no stubbed-but-shipped wire integration: "
    "the live body lands only after a recorded VCR cassette pins the "
    "facilitator's verify+settle contract."
)


class CDPX402Consumer:
    """Outbound x402 buyer over CDP facilitator on Base.

    Scaffold only. ``pay()`` raises ``NotImplementedError`` until
    S16-BAZAAR-CONSUMER-04 (recorded-fixture contract test) lands.
    """

    name = "cdp-consumer"
    mode: ConsumerMode = "cdp"

    async def pay(
        self,
        requirements: PaymentRequirements,
        *,
        max_usd: Decimal,
    ) -> PaymentReceipt:
        # Enforce budget first — keeps error ordering stable when the
        # live body lands.
        _enforce_budget(requirements, max_usd)
        raise NotImplementedError(_LIVE_GATE_REASON)


class FramesX402Consumer:
    """Outbound x402 buyer over frames.ag on Solana.

    Scaffold only. ``pay()`` raises ``NotImplementedError`` until
    S16-BAZAAR-CONSUMER-04 (recorded-fixture contract test) lands.
    """

    name = "frames-consumer"
    mode: ConsumerMode = "live"

    async def pay(
        self,
        requirements: PaymentRequirements,
        *,
        max_usd: Decimal,
    ) -> PaymentReceipt:
        _enforce_budget(requirements, max_usd)
        raise NotImplementedError(_LIVE_GATE_REASON)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def resolve_consumer_client(mode: ConsumerMode = "stub") -> X402Consumer:
    """Resolve an ``X402Consumer`` by mode.

    ``stub`` — synthetic receipt, no network. Default.
    ``live`` — frames.ag on Solana. Scaffold; raises until S16-BAZAAR-CONSUMER-04.
    ``cdp``  — CDP facilitator on Base. Scaffold; raises until S16-BAZAAR-CONSUMER-04.
    """
    if mode == "stub":
        return StubX402Consumer()
    if mode == "live":
        return FramesX402Consumer()
    if mode == "cdp":
        return CDPX402Consumer()
    raise ValueError(f"unknown consumer mode: {mode!r}")


__all__ = [
    "BudgetExceeded",
    "BudgetExceededError",
    "CDPX402Consumer",
    "ConsumerMode",
    "FramesX402Consumer",
    "StubX402Consumer",
    "X402Consumer",
    "resolve_consumer_client",
]
