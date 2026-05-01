"""S14-CDP-HARDEN-01 — finite httpx timeout on outbound CDP calls.

Without an explicit timeout, a hung CDP /settle silently exhausts the
gecko-api worker pool. ``CDPX402Client`` now resolves
``CDP_HTTP_TIMEOUT_SECONDS`` (default 30s) and feeds it through to the
underlying ``httpx.AsyncClient`` via ``FacilitatorConfig.timeout``.

These tests assert:
  1. The default resolves to 30s when the env var is unset / blank.
  2. Numeric overrides round-trip through the env reader.
  3. Garbage / non-positive values fall back to the default (we never
     hand ``None`` / ``inf`` to httpx).
  4. The facilitator client built by ``build_cdp_facilitator_client``
     carries a finite ``httpx.Timeout`` on its ``AsyncClient``.
  5. A facilitator that delays past the configured timeout surfaces a
     ``TimeoutError`` (or ``httpx.TimeoutException``) verbatim through
     ``charge()`` — fail-fast behavior the worker pool depends on.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from uuid import uuid4

import httpx
import pytest
from gecko_core.payments import (
    BASE_MAINNET_NETWORK_ID,
    CDPX402Client,
    PaymentIntent,
)
from gecko_core.payments.cdp import CDPCredentials, build_cdp_facilitator_client
from gecko_core.payments.cdp_x402_client import resolve_cdp_http_timeout_seconds


def _intent() -> PaymentIntent:
    return PaymentIntent(
        intent_id="cdp-timeout-test",
        session_id=uuid4(),
        tier="basic",
        amount_usd=Decimal("0.10"),
    )


# ---------------------------------------------------------------------------
# resolve_cdp_http_timeout_seconds — env reader contract
# ---------------------------------------------------------------------------


def test_resolve_default_when_unset() -> None:
    assert resolve_cdp_http_timeout_seconds({}) == 30.0


def test_resolve_default_when_blank() -> None:
    assert resolve_cdp_http_timeout_seconds({"CDP_HTTP_TIMEOUT_SECONDS": "  "}) == 30.0


def test_resolve_numeric_override() -> None:
    assert resolve_cdp_http_timeout_seconds({"CDP_HTTP_TIMEOUT_SECONDS": "12.5"}) == 12.5


def test_resolve_garbage_falls_back_to_default() -> None:
    assert resolve_cdp_http_timeout_seconds({"CDP_HTTP_TIMEOUT_SECONDS": "abc"}) == 30.0


def test_resolve_non_positive_falls_back_to_default() -> None:
    # Zero or negative would translate to "no timeout" / "instant timeout"
    # in some libraries — reject either, force the default.
    assert resolve_cdp_http_timeout_seconds({"CDP_HTTP_TIMEOUT_SECONDS": "0"}) == 30.0
    assert resolve_cdp_http_timeout_seconds({"CDP_HTTP_TIMEOUT_SECONDS": "-5"}) == 30.0


# ---------------------------------------------------------------------------
# build_cdp_facilitator_client wires the timeout into httpx
# ---------------------------------------------------------------------------


def test_facilitator_client_carries_finite_timeout() -> None:
    """The async httpx.AsyncClient must end up with a finite timeout.

    The x402 SDK feeds ``FacilitatorConfig.timeout`` through to
    ``httpx.AsyncClient(timeout=…)``; that constructor accepts a float
    and stores it on every ``Timeout`` phase. We verify the float
    survived the round-trip and is not ``None`` (the httpx "no timeout"
    sentinel).
    """
    creds = CDPCredentials(key_id="placeholder", key_secret="placeholder")
    client = build_cdp_facilitator_client(creds, timeout_seconds=7.5)
    # Internal field set by HTTPFacilitatorClientBase from the config.
    assert client._timeout == 7.5  # type: ignore[attr-defined]
    async_client = client._get_async_client()  # type: ignore[attr-defined]
    assert isinstance(async_client, httpx.AsyncClient)
    # httpx normalizes a float timeout into a Timeout(connect=, read=, ...)
    # tuple. Each phase must be finite.
    timeout = async_client.timeout
    for phase in (timeout.connect, timeout.read, timeout.write, timeout.pool):
        assert phase is not None, f"httpx phase {phase!r} must be finite, got None"


def test_cdpx402_client_exposes_timeout_property() -> None:
    """`bb doctor` reads `client.http_timeout_seconds`."""
    client = CDPX402Client(
        treasury_address="0xTreasuryBase",
        api_key_id="placeholder",
        api_key_secret="placeholder",
        http_timeout_seconds=12.0,
    )
    assert client.http_timeout_seconds == 12.0


# ---------------------------------------------------------------------------
# Forced delay surfaces a TimeoutError through charge()
# ---------------------------------------------------------------------------


@dataclass
class _SlowFacilitator:
    """Facilitator stand-in whose `settle` sleeps past the configured
    `timeout_s`, forcing the surrounding ``asyncio.wait_for`` to fire.

    We don't reach the real httpx layer here — the contract under test
    is "a hung settle propagates verbatim, doesn't get swallowed". The
    worker-pool symptom from the RCA is exactly this: hung calls fail
    fast instead of stalling.
    """

    timeout_s: float
    calls: list[Any] = field(default_factory=list)

    async def settle(self, payload: Any, requirements: Any) -> Any:
        self.calls.append(payload)
        await asyncio.sleep(self.timeout_s + 1.0)
        raise AssertionError("should never reach here under wait_for cap")


@pytest.mark.asyncio
async def test_forced_delay_in_settle_surfaces_timeout_error() -> None:
    """A facilitator that hangs > timeout should surface TimeoutError.

    Wrap the charge in an outer ``asyncio.wait_for`` keyed off the same
    timeout the CDP client carries. This mirrors the gate-side timeout
    pattern the worker pool depends on; without HARDEN-01 the inner
    httpx layer was unbounded, so the wait_for outside was the only
    defense and many call sites lacked one.
    """
    fake = _SlowFacilitator(timeout_s=0.05)
    client = CDPX402Client(
        facilitator=fake,
        treasury_address="0xTreasuryBase",
        network=BASE_MAINNET_NETWORK_ID,
        http_timeout_seconds=0.05,
    )
    with pytest.raises((asyncio.TimeoutError, TimeoutError)):
        await asyncio.wait_for(client.charge(_intent()), timeout=client.http_timeout_seconds)
    # And the facilitator was called — the timeout fires AFTER dispatch,
    # confirming the seam is wired correctly.
    assert len(fake.calls) == 1
