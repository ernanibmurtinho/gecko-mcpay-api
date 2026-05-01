"""CDPX402Client — Base mainnet USDC settlement via the CDP Facilitator.

Parallel to ``LiveX402Client`` (frames.ag → Solana). Same ``X402Client``
Protocol, same ``PaymentResult`` shape — only the network and signing
backend differ.

Settlement target:
    Base mainnet USDC at ``0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913``
    (the official Circle USDC contract on Base, verified against the
    ``payment-required`` header probed in
    ``docs/strategy/sprint-12-chore-probes-2026-04-30.md``).

Wire path (live):
    1. Build a ``CDPCredentials`` from ``CDP_API_KEY_ID`` /
       ``CDP_API_KEY_SECRET`` env. Sentinels (``__unset__``) raise a
       clear "not configured" error before any network IO.
    2. Build (or reuse) the existing ``HTTPFacilitatorClient`` with the
       CDP ``AuthProvider`` (mints fresh JWTs per request).
    3. POST ``/settle`` with a ``PaymentPayload`` + ``PaymentRequirements``
       describing the Base-USDC charge.
    4. Map the returned ``SettleResponse`` to the unified
       ``PaymentResult``: ``tx_signature = response.transaction`` so the
       existing reconcile path (``bb economics --verify``) can pick it up.

Critical invariant for Sprint 12: this module must not initiate any
real CDP API calls during unit tests. Every non-stub code path takes an
injected facilitator client; tests pass a fake.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Final, Protocol

from pydantic import SecretStr

from gecko_core.payments.cdp import (
    CDPCredentials,
    build_cdp_facilitator_client,
    is_unconfigured,
)
from gecko_core.payments.models import PaymentIntent, PaymentResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Base mainnet constants. Verified against the publish.new x402v2 challenge
# probed on 2026-04-30 (see sprint-12-chore-probes). These are public
# contract addresses, not secrets.
# ---------------------------------------------------------------------------

BASE_MAINNET_USDC_CONTRACT: Final = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
BASE_MAINNET_NETWORK_ID: Final = "eip155:8453"
BASE_SEPOLIA_NETWORK_ID: Final = "eip155:84532"

CDP_FACILITATOR_BASE_URL: Final = "https://api.cdp.coinbase.com/platform/v2/x402"

# USDC on Base/EVM uses 6 decimals, same as Solana SPL USDC.
_USDC_DECIMALS: Final = 6


# ---------------------------------------------------------------------------
# Typed errors — surfaced verbatim by the gate.
# ---------------------------------------------------------------------------


class CDPX402Error(RuntimeError):
    """Base for CDP facilitator settlement failures."""


class CDPNotConfiguredError(CDPX402Error):
    """``CDP_API_KEY_ID`` / ``CDP_API_KEY_SECRET`` missing or sentinel."""


class CDPSettleError(CDPX402Error):
    """Facilitator returned ``success=false`` or non-2xx."""


# ---------------------------------------------------------------------------
# Settle-call abstraction so tests can inject a fake without touching the
# network. Mirrors the subset of ``HTTPFacilitatorClient`` we use.
# ---------------------------------------------------------------------------


class _FacilitatorLike(Protocol):
    async def settle(self, payload: Any, requirements: Any) -> Any: ...


def _usdc_smallest_units(amount_usd: Decimal) -> int:
    """6-decimal USDC on Base. Quantize so we never pass a fractional unit."""
    return int((amount_usd * Decimal(10**_USDC_DECIMALS)).quantize(Decimal(1)))


def _build_payment_requirements(
    *,
    amount_usd: Decimal,
    pay_to: str,
    network: str,
    asset: str,
    resource_url: str,
    max_timeout_seconds: int,
) -> Any:
    """Build a ``PaymentRequirements`` instance for the CDP settle call.

    Imported lazily so module import doesn't require the x402 lib at
    test-collection time when stub-mode tests run.
    """
    from x402.schemas import PaymentRequirements

    # Pydantic v2 field names are snake_case; the wire aliases (``payTo``,
    # ``maxTimeoutSeconds``) are produced by ``model_dump(by_alias=True)``
    # at the HTTP boundary, not the constructor.
    return PaymentRequirements(
        scheme="exact",
        network=network,
        asset=asset,
        amount=str(_usdc_smallest_units(amount_usd)),
        pay_to=pay_to,
        max_timeout_seconds=max_timeout_seconds,
        extra={},
    )


def _build_payment_payload(
    *,
    intent_id: str,
    requirements: Any,
    payer_private_key: str | None = None,
) -> Any:
    """Build a signed x402v2 ``PaymentPayload`` for a CDP settle call.

    When ``payer_private_key`` is supplied, signs a real EIP-3009
    ``transferWithAuthorization`` over the requirements via the x402 SDK's
    ``ExactEvmScheme`` (Track C live-smoke path). Without a key, returns a
    placeholder shape used only by stub-mode tests; CDP rejects placeholder
    payloads with HTTP 400 ('signature' / 'transaction' required), which
    is the intended fail-fast for "production with no signer wired."

    ``payer_private_key`` is the buyer wallet's secret. For Gecko's V1
    pay-yourself flow it is ``TWITSH_WALLET_PRIVATE_KEY`` (a server-managed
    EOA), and the receiving wallet (requirements.pay_to) is also TWITSH —
    so net spend is zero gas + dust slippage. For real buyer flows in V2+,
    this comes from the X-PAYMENT header instead of the env.
    """
    from x402.schemas import PaymentPayload

    if not payer_private_key:
        # Stub-mode shape — preserved for tests that flow through the
        # facilitator-call boundary without signing. CDP will 400 if this
        # ever reaches a real settle endpoint, which is what we want.
        return PaymentPayload(
            x402_version=2,
            accepted=requirements,
            payload={"intent_id": intent_id, "amount": requirements.amount},
        )

    from eth_account import Account
    from x402.mechanisms.evm.exact.client import ExactEvmScheme

    account = Account.from_key(payer_private_key)
    inner = ExactEvmScheme(account).create_payment_payload(requirements)

    # ``inner`` is the dict-shaped {authorization, signature} body. Wrap
    # it in the v2 PaymentPayload envelope CDP /settle expects.
    return PaymentPayload(
        x402_version=2,
        accepted=requirements,
        payload=inner,
    )


# ---------------------------------------------------------------------------
# CDPX402Client
# ---------------------------------------------------------------------------


class CDPX402Client:
    """Settle x402 charges on Base mainnet through the CDP Facilitator.

    Constructor accepts either:
      * ``credentials`` — pre-built ``CDPCredentials`` (the live path);
      * ``facilitator`` — an injected facilitator-like (the test path).

    If neither is supplied, ``charge()`` raises ``CDPNotConfiguredError``
    on the first call. We never crash at import time so the factory tests
    stay green even on dev boxes without CDP keys.

    ``treasury_address`` is the Base ``payTo`` (the wallet the user-facing
    settlement lands in). Pulled from ``GECKO_WALLET_ADDRESS_BASE`` if not
    supplied; the gate-side error mentions the env var explicitly.
    """

    facilitator_id: Final = "cdp-base"

    def __init__(
        self,
        *,
        credentials: CDPCredentials | None = None,
        facilitator: _FacilitatorLike | None = None,
        api_key_id: str | None = None,
        api_key_secret: SecretStr | str | None = None,
        treasury_address: str | None = None,
        payer_private_key: SecretStr | str | None = None,
        network: str = BASE_MAINNET_NETWORK_ID,
        asset_contract: str = BASE_MAINNET_USDC_CONTRACT,
        base_url: str = CDP_FACILITATOR_BASE_URL,
        resource_url: str = "https://geckovision.tech/research",
        max_timeout_seconds: int = 60,
    ) -> None:
        self._credentials = credentials
        self._facilitator = facilitator
        self._api_key_id = api_key_id
        # Normalize SecretStr → plain string lazily. We keep it private and
        # never log it. The CDP signing path expects the PEM body.
        if isinstance(api_key_secret, SecretStr):
            self._api_key_secret: str | None = api_key_secret.get_secret_value()
        else:
            self._api_key_secret = api_key_secret
        # Buyer-side EIP-3009 signer key. For Gecko's V1 pay-yourself flow
        # this is TWITSH_WALLET_PRIVATE_KEY (server-managed EOA on Base).
        # Tests pass None and stay on the stub payload path.
        if isinstance(payer_private_key, SecretStr):
            self._payer_private_key: str | None = payer_private_key.get_secret_value()
        else:
            self._payer_private_key = payer_private_key
        self._treasury = treasury_address
        self._network = network
        self._asset_contract = asset_contract
        self._base_url = base_url
        self._resource_url = resource_url
        self._max_timeout_seconds = max_timeout_seconds

    # --- public --------------------------------------------------------

    async def charge(self, intent: PaymentIntent) -> PaymentResult:
        """Settle ``intent`` on Base mainnet via CDP. Returns a unified result.

        The returned ``PaymentResult.tx_signature`` is the EVM transaction
        hash that ``bb economics --verify`` and ``creator_settlements``
        reconcile against — same field name, different chain.
        """
        if not self._treasury:
            raise CDPNotConfiguredError(
                "CDP path requires GECKO_WALLET_ADDRESS_BASE (or treasury_address) "
                "to be set to a Base mainnet wallet you control."
            )

        amount_units = _usdc_smallest_units(intent.amount_usd)
        if amount_units <= 0:
            raise CDPX402Error(
                f"intent amount_usd={intent.amount_usd} rounds to 0 USDC smallest units"
            )

        facilitator = self._facilitator or self._build_facilitator()

        requirements = _build_payment_requirements(
            amount_usd=intent.amount_usd,
            pay_to=self._treasury,
            network=self._network,
            asset=self._asset_contract,
            resource_url=self._resource_url,
            max_timeout_seconds=self._max_timeout_seconds,
        )
        payload = _build_payment_payload(
            intent_id=intent.intent_id,
            requirements=requirements,
            payer_private_key=self._payer_private_key,
        )

        try:
            response = await facilitator.settle(payload, requirements)
        except Exception as exc:
            # Verbatim policy: log the error class only, not its body —
            # CDP error bodies can echo signed payloads we don't want
            # in logs. The exception itself bubbles up untouched.
            logger.warning(
                "cdp settle raised intent_id=%s class=%s",
                intent.intent_id,
                type(exc).__name__,
            )
            raise

        return self._map_response(intent=intent, response=response)

    # --- internals -----------------------------------------------------

    def _build_facilitator(self) -> _FacilitatorLike:
        """Build the production CDP facilitator client. Lazy — only on charge."""
        if self._credentials is None:
            key_id = self._api_key_id
            key_secret = self._api_key_secret
            if is_unconfigured(key_id) or is_unconfigured(key_secret):
                raise CDPNotConfiguredError(
                    "CDP_API_KEY_ID / CDP_API_KEY_SECRET are not set "
                    "(SSM sentinel or empty value detected). "
                    "Set both before flipping X402_MODE=cdp."
                )
            assert key_id is not None and key_secret is not None
            self._credentials = CDPCredentials(key_id=key_id, key_secret=key_secret)
        return build_cdp_facilitator_client(
            self._credentials,
            base_url=self._base_url,
            identifier="cdp-base-mainnet",
        )

    def _map_response(
        self,
        *,
        intent: PaymentIntent,
        response: Any,
    ) -> PaymentResult:
        """Map a CDP ``SettleResponse`` to the unified ``PaymentResult``.

        Both frames.ag and CDP land here via different facilitator-side
        shapes; this is the unification point for ``bb economics --verify``.
        """
        success = bool(getattr(response, "success", False))
        tx_hash = getattr(response, "transaction", None) or None
        error_reason = getattr(response, "error_reason", None)
        error_message = getattr(response, "error_message", None)

        if not success:
            reason = error_message or error_reason or "cdp settle returned success=false"
            # Surface verbatim by raising — gate maps to PaymentRequiredError.
            raise CDPSettleError(f"cdp settle failed: {reason}")

        if not tx_hash:
            raise CDPSettleError(
                f"cdp settle returned success without transaction hash for intent {intent.intent_id}"
            )

        logger.info(
            "cdp charge confirmed intent_id=%s tx=%s network=%s",
            intent.intent_id,
            tx_hash,
            self._network,
        )
        return PaymentResult(
            intent_id=intent.intent_id,
            status="success",
            tx_signature=str(tx_hash),
            error=None,
        )


__all__ = [
    "BASE_MAINNET_NETWORK_ID",
    "BASE_MAINNET_USDC_CONTRACT",
    "BASE_SEPOLIA_NETWORK_ID",
    "CDP_FACILITATOR_BASE_URL",
    "CDPNotConfiguredError",
    "CDPSettleError",
    "CDPX402Client",
    "CDPX402Error",
]
