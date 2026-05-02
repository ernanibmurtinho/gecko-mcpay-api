"""X402Consumer Protocol + conformers (Sprint 16 Track B, outbound buyer).

S16-BAZAAR-CONSUMER-01 + S16-BAZAAR-CONSUMER-04.

Buyer-side counterpart to seller-side ``X402Client`` (see ``protocol.py``).
Vocabulary is intentionally inverse:

  * ``X402Client.charge(intent)`` — "I am being paid."
  * ``X402Consumer.pay(requirements, max_usd)`` — "I am paying."

The two seams fail independently. ``X402_CONSUMER_MODE`` is separate from
seller-side ``X402_MODE`` (canonical declaration in
``gecko_core.payments.modes``) so stub-buyer + live-seller is a real dev
configuration — don't burn USDC fetching while iterating on the receive
flow.

Pattern B / C (CLAUDE.md):

  * ``CDPX402Consumer.pay()`` lands a real x402-buyer wire body
    (S16-BAZAAR-CONSUMER-04) — gated by a recorded-fixture cassette
    contract test (``tests/payments/test_bazaar_consumer_contract.py``).
    First-class targets: TripAdvisor on Base ($0.01 USDC).
  * ``FramesX402Consumer.pay()`` stays scaffolded — no Solana-side
    $0.01-class endpoint candidate identified at S16 ship-time. The
    body raises with an explicit deferral message; follow-up ticket
    ``S16-BAZAAR-CONSUMER-04-FRAMES`` tracks live wiring.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4

import httpx

from gecko_core.models import PaymentReceipt
from gecko_core.payments.bazaar_discovery import PaymentRequirements
from gecko_core.payments.modes import ConsumerMode

logger = logging.getLogger(__name__)

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
# Live CDP conformer — S16-BAZAAR-CONSUMER-04
#
# Wire: x402 v2 buyer dance against an arbitrary resource URL on Base.
#   1. Caller-side budget check (raises ``BudgetExceeded`` before network).
#   2. Build a signed ``PaymentPayload`` for the resource's
#      ``PaymentRequirements`` (reusing ``cdp_x402_client._build_payment_payload``
#      so the EIP-3009 vs Permit2 fix from 19d0a83 stays one place).
#   3. Base64-encode the payload as ``X-PAYMENT`` and GET the resource.
#   4. Resource server settles via its own facilitator, returns the body
#      plus an ``X-PAYMENT-RESPONSE`` header carrying the settle result
#      (transaction hash, success flag).
#   5. Parse → ``PaymentReceipt``.
#
# Pattern B safety: Live body is gated by the recorded-fixture cassette
# in ``tests/payments/cassettes/bazaar_consumer/`` + the local simulation
# harness in ``tests/payments/test_bazaar_consumer_simulation.py``. The
# simulation falsifies the verify→settle dispatch *before* any real
# USDC moves — Sprint 12 Track A's ~10-iteration mainnet-debug cycle
# does not happen here.
# ---------------------------------------------------------------------------


# Frames buyer is deferred — see S16-BAZAAR-CONSUMER-04-FRAMES follow-up.
_FRAMES_DEFER_REASON = (
    "S16-BAZAAR-CONSUMER-04-FRAMES — pending Solana endpoint candidate. "
    "No $0.01-class agentic.market-listed Solana endpoint reachable at "
    "S16 ship-time; would require burning $0.05+ to record without a "
    "cheap proving target. Track follow-up: file 'frames buyer cassette' "
    "ticket + identify a Solana x402 endpoint < $0.05 advertised."
)


class CDPNotConfiguredError(RuntimeError):
    """Buyer signing key (``GECKO_BAZAAR_BUYER_PRIVATE_KEY``) missing."""


class ResourceFetchError(RuntimeError):
    """Resource server returned a non-2xx, non-402 status (e.g. 5xx)."""


class SettlementFailedError(RuntimeError):
    """``X-PAYMENT-RESPONSE`` reported ``success=false`` on the resource call."""


_BUYER_KEY_ENV = "GECKO_BAZAAR_BUYER_PRIVATE_KEY"


def _build_x_payment_header(
    *,
    requirements: PaymentRequirements,
    intent_id: str,
    payer_private_key: str,
    resource_url: str,
) -> str:
    """Build a base64-encoded x402 v2 ``X-PAYMENT`` header.

    Reuses ``cdp_x402_client._build_payment_requirements`` and
    ``_build_payment_payload`` so the buyer-side signing path matches
    the seller-side payload shape exactly. Without this reuse the buyer
    would re-introduce the EIP-3009 vs Permit2 confusion that 19d0a83
    fixed on the seller side.
    """
    # Lazy imports — avoids dragging the x402 SDK into module-import for
    # stub-mode tests.
    from gecko_core.payments.cdp_x402_client import (
        _build_payment_payload,
        _build_payment_requirements,
    )

    # Convert PaymentRequirements (discovery shape) → x402 SDK
    # PaymentRequirements (signing shape). Discovery's ``amount`` is
    # already atomic-units; ``_build_payment_requirements`` re-derives
    # from a USD Decimal, so we go through the USD Decimal path that
    # ``max_amount_required`` carries.
    amount_usd = requirements.max_amount_required
    if amount_usd is None:
        # Fall back to atomic-string parsing — assumes USDC 6-decimal.
        amount_usd = Decimal(int(requirements.amount or "0")) / Decimal(10**6)

    sdk_requirements = _build_payment_requirements(
        amount_usd=amount_usd,
        pay_to=requirements.pay_to,
        network=requirements.network,
        asset=requirements.asset,
        resource_url=resource_url,
        max_timeout_seconds=requirements.max_timeout_seconds,
    )
    sdk_payload = _build_payment_payload(
        intent_id=intent_id,
        requirements=sdk_requirements,
        payer_private_key=payer_private_key,
        resource_url=resource_url,
    )

    # x402 v2 X-PAYMENT header = base64(JSON(payload, by_alias=True)).
    # ``model_dump`` from Pydantic v2 produces wire-shaped fields.
    payload_dict = sdk_payload.model_dump(by_alias=True, mode="json")
    body = json.dumps(payload_dict, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(body).decode("ascii")


def _parse_payment_response_header(
    header_value: str,
) -> dict[str, Any]:
    """Decode the ``X-PAYMENT-RESPONSE`` header (base64 JSON).

    Shape: ``{"success": bool, "transaction": "0x...", "network": "...",
    "payer": "0x..."}`` (x402 v2). Any decode error surfaces as
    ``SettlementFailedError`` so the caller sees the verbatim message.
    """
    try:
        decoded = base64.b64decode(header_value).decode("utf-8")
        parsed: dict[str, Any] = json.loads(decoded)
        return parsed
    except Exception as exc:
        raise SettlementFailedError(
            f"X-PAYMENT-RESPONSE header could not be decoded: {exc}"
        ) from exc


class CDPX402Consumer:
    """Outbound x402 buyer over CDP / Base USDC.

    The wire path is the standard x402 v2 buyer dance:

      * Build a signed ``PaymentPayload`` for the resource's advertised
        ``PaymentRequirements``.
      * Encode as ``X-PAYMENT`` and call the resource URL.
      * Resource server settles via its facilitator and returns
        ``X-PAYMENT-RESPONSE`` with the settle outcome.

    Buyer signing key is read from ``GECKO_BAZAAR_BUYER_PRIVATE_KEY``
    at construction time (or passed explicitly for tests). Without a
    key, ``pay()`` raises :class:`CDPNotConfiguredError` — never falls
    back to a stubbed payload that would 400 against a real facilitator.

    For stub-mode dev (no key), use :class:`StubX402Consumer` via
    ``X402_CONSUMER_MODE=stub``.
    """

    name = "cdp-consumer"
    mode: ConsumerMode = "cdp"

    def __init__(
        self,
        *,
        payer_private_key: str | None = None,
        client: httpx.AsyncClient | None = None,
        timeout_s: float = 30.0,
    ) -> None:
        self._payer_private_key = payer_private_key or os.environ.get(_BUYER_KEY_ENV)
        self._client = client
        self._timeout_s = timeout_s

    async def pay(
        self,
        requirements: PaymentRequirements,
        *,
        max_usd: Decimal,
    ) -> PaymentReceipt:
        # Pre-flight: budget cap. Raises BEFORE any network call.
        _enforce_budget(requirements, max_usd)

        if not self._payer_private_key:
            raise CDPNotConfiguredError(
                f"{_BUYER_KEY_ENV} not set; CDP buyer requires a funded "
                "Base EOA private key. Use X402_CONSUMER_MODE=stub for dev."
            )

        # Resource URL is carried on PaymentRequirements.raw for CDP
        # discovery (we stash the full row); for agentic.market-sourced
        # requirements it lives on the BazaarResource — caller must pass
        # via .raw["resource"] or we reject.
        resource_url = (
            requirements.raw.get("resource")
            or requirements.raw.get("resourceUrl")
            or requirements.raw.get("url")
        )
        if not resource_url:
            raise ResourceFetchError(
                "PaymentRequirements.raw missing resource URL; cannot "
                "build buyer call. Pass it via raw['resource']."
            )

        intent_id = f"bazaar-{uuid4().hex[:16]}"
        x_payment = _build_x_payment_header(
            requirements=requirements,
            intent_id=intent_id,
            payer_private_key=self._payer_private_key,
            resource_url=str(resource_url),
        )

        # First call carries X-PAYMENT directly. Some resources answer
        # 402 even with a header (challenge mismatch); on that we surface
        # verbatim — re-fetching the challenge and re-signing is a future
        # refinement (S17, multi-attempt retry).
        headers = {"X-PAYMENT": x_payment, "Accept": "application/json"}

        async def _fetch(client: httpx.AsyncClient) -> httpx.Response:
            return await client.get(str(resource_url), headers=headers, timeout=self._timeout_s)

        if self._client is not None:
            response = await _fetch(self._client)
        else:
            async with httpx.AsyncClient() as fresh:
                response = await _fetch(fresh)

        if response.status_code == 402:
            # Server still demanding payment — surface verbatim, do not
            # silently retry (we don't know if the new challenge changed
            # the price).
            raise SettlementFailedError(
                f"resource {resource_url} returned 402 even with X-PAYMENT; "
                f"body={response.text[:512]}"
            )
        if response.status_code >= 400:
            raise ResourceFetchError(
                f"resource {resource_url} returned {response.status_code}: {response.text[:512]}"
            )

        settle_header = response.headers.get("X-PAYMENT-RESPONSE")
        if not settle_header:
            raise SettlementFailedError(
                f"resource {resource_url} returned 2xx without X-PAYMENT-RESPONSE; "
                "facilitator did not confirm settlement."
            )
        settle = _parse_payment_response_header(settle_header)

        if not settle.get("success"):
            reason = settle.get("errorReason") or settle.get("error") or "unknown"
            raise SettlementFailedError(f"settlement reported failure: {reason}")

        tx = settle.get("transaction") or settle.get("txHash") or ""
        receipt: PaymentReceipt = {
            "intent_id": intent_id,
            "status": "success",
            "tx_signature": str(tx) if tx else None,
            "facilitator_id": "cdp-base",
            "network": requirements.network,
        }
        logger.info(
            "bazaar buy settled intent_id=%s tx=%s network=%s",
            intent_id,
            tx,
            requirements.network,
        )
        return receipt


class FramesX402Consumer:
    """Outbound x402 buyer over frames.ag on Solana.

    **Deferred** — S16-BAZAAR-CONSUMER-04 ships the CDP/Base wire body;
    the frames-buyer body is parked behind a follow-up ticket because no
    $0.01-class Solana x402 endpoint was identifiable at S16 ship-time
    (see :data:`_FRAMES_DEFER_REASON`).
    """

    name = "frames-consumer"
    mode: ConsumerMode = "live"

    async def pay(
        self,
        requirements: PaymentRequirements,
        *,
        max_usd: Decimal,
    ) -> PaymentReceipt:
        # Budget cap first — preserve error ordering when this lands.
        _enforce_budget(requirements, max_usd)
        raise NotImplementedError(_FRAMES_DEFER_REASON)


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
    "CDPNotConfiguredError",
    "CDPX402Consumer",
    "ConsumerMode",
    "FramesX402Consumer",
    "ResourceFetchError",
    "SettlementFailedError",
    "StubX402Consumer",
    "X402Consumer",
    "resolve_consumer_client",
]
