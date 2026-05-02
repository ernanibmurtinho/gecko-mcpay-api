"""Recorded-fixture contract test for the Bazaar buyer (Pattern C gate).

S16-BAZAAR-CONSUMER-04. Mirrors the seller-side ``test_cdp_live_verify``
template (live_cdp marker) on the buyer side: the live wire is gated by
a recorded JSON cassette in
``tests/payments/cassettes/bazaar_consumer/``.

CI behavior:
  * No env var → replay from cassette. Cassette absent → test FAILS
    (regression guard, per ticket spec).
  * ``GECKO_BAZAAR_LIVE=1`` → real network, requires
    ``GECKO_BAZAAR_BUYER_PRIVATE_KEY`` (a funded Base EOA). Use to
    re-record after operator-driven manual capture; not for routine CI.

The cassette pins the *shape* of the resource server's response —
2xx + ``X-PAYMENT-RESPONSE`` carrying a settled tx. If the upstream
schema drifts, this test trips before any USDC moves; that's the
Pattern B/C promise.
"""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

import pytest
from gecko_core.payments.bazaar_discovery import PaymentRequirements
from gecko_core.payments.x402_consumer import (
    BudgetExceeded,
    CDPX402Consumer,
)

from tests.payments._cassette import LIVE_ENV, is_live_record_mode, replay_cassette

pytestmark = pytest.mark.live_bazaar


CASSETTE_PATH = (
    Path(__file__).resolve().parent / "cassettes" / "bazaar_consumer" / "cdp_tripadvisor_pay.json"
)

# TripAdvisor x402 endpoint identified by S16-BAZAAR-DISCOVERY-01 on Base.
_TRIPADVISOR_URL = "https://tripadvisor.x402.paysponge.com/api/v1/location/12345/details"
_BASE_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
_TRIPADVISOR_PAY_TO = "0x5d6dF6b10C54617dac4Bf9993ad9fA384b7B36d3"

# Burner key — only used to produce a structurally valid signature in
# replay mode. Live re-record reads GECKO_BAZAAR_BUYER_PRIVATE_KEY from
# env and ignores this value.
_REPLAY_BURNER_KEY = "0x" + "ab" * 32


def _tripadvisor_requirement() -> PaymentRequirements:
    return PaymentRequirements(
        network="eip155:8453",
        asset=_BASE_USDC,
        pay_to=_TRIPADVISOR_PAY_TO,
        amount="10000",  # 0.01 USDC atomic units
        scheme="exact",
        max_timeout_seconds=60,
        max_amount_required=Decimal("0.01"),
        raw={"resource": _TRIPADVISOR_URL},
    )


# ---------------------------------------------------------------------------
# Cassette presence — regression guard.
# ---------------------------------------------------------------------------


def test_cassette_present_in_replay_mode() -> None:
    """CI without the cassette is a hard failure (per ticket spec).

    If you saw this test fail in CI, either:
      * The cassette was deleted by mistake — restore from git.
      * Someone is running on a fresh checkout and tripped the
        live-only branch by accident — double-check ``GECKO_BAZAAR_LIVE``
        is unset.
    """
    if is_live_record_mode():
        pytest.skip(f"{LIVE_ENV}=1 set; cassette presence check is skipped on record runs")
    assert CASSETTE_PATH.exists(), (
        f"Bazaar consumer contract cassette missing at {CASSETTE_PATH}. "
        "CI must replay from a recorded cassette per S16-BAZAAR-CONSUMER-04."
    )


# ---------------------------------------------------------------------------
# Replay path — exercise CDPX402Consumer.pay() against the cassette.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cdp_consumer_pay_settles_against_cassette() -> None:
    """Drive ``CDPX402Consumer.pay()`` against the recorded TripAdvisor
    interaction. Asserts:

      * Receipt status == "success"
      * ``tx_signature`` is a Base-shaped 0x-prefixed hex string
      * ``network`` matches the requirement
      * ``facilitator_id`` == "cdp-base"
    """
    if is_live_record_mode():
        # Live mode: caller-supplied env key + real network. We don't
        # auto-record from this test (see _cassette.record_or_replay
        # docstring) — the operator captures via httpx hooks / mitmproxy
        # and commits the JSON manually.
        key = os.environ.get("GECKO_BAZAAR_BUYER_PRIVATE_KEY")
        if not key:
            pytest.skip(
                "GECKO_BAZAAR_BUYER_PRIVATE_KEY required for live re-record; "
                f"unset {LIVE_ENV} to run replay against the cassette."
            )
        consumer = CDPX402Consumer(payer_private_key=key)
        receipt = await consumer.pay(_tripadvisor_requirement(), max_usd=Decimal("0.05"))
    else:
        with replay_cassette(CASSETTE_PATH):
            consumer = CDPX402Consumer(payer_private_key=_REPLAY_BURNER_KEY)
            receipt = await consumer.pay(_tripadvisor_requirement(), max_usd=Decimal("0.05"))

    assert receipt["status"] == "success"
    assert receipt["facilitator_id"] == "cdp-base"
    assert receipt["network"] == "eip155:8453"
    tx = receipt.get("tx_signature") or ""
    assert tx.startswith("0x"), f"expected Base tx hash, got {tx!r}"
    assert len(tx) == 66, f"expected 32-byte hex hash, got {len(tx)} chars"


# ---------------------------------------------------------------------------
# Budget cap — defense against advertised-price drift between discovery
# and settle. Must short-circuit BEFORE any HTTP.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_budget_cap_breached_pre_call() -> None:
    """``max_usd`` < advertised → ``BudgetExceeded`` raised pre-call.

    No cassette needed: this path never touches HTTP. The replay context
    isn't entered, so a network call here would crash with
    ``ConnectError`` — but ``BudgetExceeded`` short-circuits first."""
    consumer = CDPX402Consumer(payer_private_key=_REPLAY_BURNER_KEY)
    requirement = _tripadvisor_requirement()  # advertised 0.01
    with pytest.raises(BudgetExceeded):
        await consumer.pay(requirement, max_usd=Decimal("0.005"))


# ---------------------------------------------------------------------------
# Frames buyer — explicitly deferred for S16. Documenting the gap so the
# follow-up ticket is discoverable from the test suite.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_frames_consumer_is_explicitly_deferred() -> None:
    """No Solana $0.01-class endpoint candidate at S16 ship-time — the
    frames buyer wire stays scaffolded behind a follow-up ticket
    (S16-BAZAAR-CONSUMER-04-FRAMES). The deferral message must contain
    that ticket so future contributors find this gate from the error."""
    from gecko_core.payments.x402_consumer import FramesX402Consumer

    consumer = FramesX402Consumer()
    with pytest.raises(NotImplementedError) as excinfo:
        await consumer.pay(_tripadvisor_requirement(), max_usd=Decimal("0.10"))
    assert "S16-BAZAAR-CONSUMER-04-FRAMES" in str(excinfo.value)
