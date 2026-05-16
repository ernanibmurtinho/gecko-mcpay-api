"""Live buyer-side x402 client for Solana settlement (S24 WS-C Task #4).

This is the *buyer* side of the x402 protocol — the code that pays real
USDC on Solana mainnet when Gecko's runtime calls a paid skill (e.g.
``gecko_trade_research``). It is the counterpart to ``LiveX402Client``
(seller/facilitator path) and uses the buyer keypair held in SSM
SecureString and mounted into the ECS task via ``secrets:``.

**Pattern C contract test** (CLAUDE.md): ships with a recorded-fixture
test under ``tests/integration/test_live_buyer_path.py``, marked with
``@pytest.mark.live_solana``. CI runs the cassette replay; live re-record
requires ``GECKO_LIVE_SOLANA=1`` + a funded buyer wallet.

**Pattern B safety**: NEVER reach mainnet from a primary debug session.
Local falsification path uses recorded fixtures; live smoke is the final
verification step in the runbook at
``docs/runbooks/2026-05-20-x402-live-flip.md``.

**Gate guard** (defence-in-depth): instantiating this client does not by
itself authorise a live settlement. The dispatcher in
``gecko_core.payments`` (``assert_live_settlement_allowed``) gates
whether ``X402_MODE=live`` is honoured for a given tool name + kill-switch
state. A misconfig falls back to ``StubX402Client`` silently.

Wire flow per :meth:`charge`:

  1. Load buyer keypair from the path referenced by
     ``BUYER_WALLET_PRIVATE_KEY_PATH`` (SSM-mounted file, never logged).
  2. Build a Solana USDC transfer to the seller's ``payTo`` address for
     the wire amount declared in the x402 ``accepts`` block.
  3. Sign locally (ed25519). The private key NEVER leaves the process.
  4. Submit via Helius RPC or the configured facilitator's submission
     endpoint; surface the tx signature.
  5. Poll ``getSignatureStatuses`` (reuses :class:`LiveX402Client` helpers)
     until ``confirmed`` / ``finalized`` or the 60s budget expires.

Errors are typed (:class:`LiveBuyerError` subclasses) and surfaced
verbatim — never catch-and-rephrase the facilitator's message (S12.5
lesson).
"""

from __future__ import annotations

import logging
import os
from decimal import Decimal
from pathlib import Path
from typing import Final

from pydantic import SecretStr

from gecko_core.payments.models import PaymentIntent, PaymentResult
from gecko_core.payments.networks import NetworkConfig, resolve_network
from gecko_core.payments.protocol import ConfirmationStatus
from gecko_core.payments.x402_client import (
    ConfirmationTimeoutError,
    LiveX402Client,
    LiveX402Error,
    _usdc_smallest_units,
)

logger = logging.getLogger(__name__)


# Env contract — documented in the flip runbook §3. Values come from SSM
# SecureString and are mounted into the ECS task via the ``secrets:`` block.
# The dispatcher never reads these directly; it asks this module for the
# resolved buyer wallet via :func:`buyer_wallet_from_env`.
BUYER_PRIVATE_KEY_PATH_ENV: Final = "BUYER_WALLET_PRIVATE_KEY_PATH"
BUYER_PUBKEY_ENV: Final = "BUYER_WALLET_PUBKEY"


class LiveBuyerError(RuntimeError):
    """Base for live buyer-side payment failures."""


class BuyerWalletNotConfiguredError(LiveBuyerError):
    """Required ``BUYER_WALLET_*`` env not set or key file missing."""


class BuyerWalletKeyfileUnreadableError(LiveBuyerError):
    """Buyer key file exists but cannot be parsed as a Solana keypair."""


class FacilitatorRejectedError(LiveBuyerError):
    """Seller's facilitator rejected the buyer's submitted payment."""


class BuyerInsufficientBalanceError(LiveBuyerError):
    """Buyer wallet does not have enough USDC for the requested amount."""


def buyer_wallet_from_env() -> tuple[Path, str]:
    """Resolve buyer wallet config from env. Raises if any leg is missing.

    Returns ``(private_key_path, pubkey)``. The private key is *not*
    loaded here — only the path is validated. Actual keypair material is
    read inside :meth:`LiveBuyerX402Client.charge` and never logged.
    """
    raw_path = os.environ.get(BUYER_PRIVATE_KEY_PATH_ENV, "").strip()
    pubkey = os.environ.get(BUYER_PUBKEY_ENV, "").strip()
    if not raw_path or not pubkey:
        raise BuyerWalletNotConfiguredError(
            f"buyer wallet env incomplete: {BUYER_PRIVATE_KEY_PATH_ENV}="
            f"{'set' if raw_path else 'unset'}, {BUYER_PUBKEY_ENV}="
            f"{'set' if pubkey else 'unset'}. "
            "See docs/runbooks/2026-05-20-x402-live-flip.md §3."
        )
    path = Path(raw_path)
    if not path.exists():
        raise BuyerWalletNotConfiguredError(
            f"{BUYER_PRIVATE_KEY_PATH_ENV}={raw_path} does not exist. "
            "Verify the SSM SecureString is mounted into the ECS task "
            "(see flip runbook §3)."
        )
    return path, pubkey


class LiveBuyerX402Client:
    """Buyer-side x402 client — signs and submits real Solana USDC transfers.

    Mirrors the :class:`X402Client` Protocol so the dispatcher can swap
    it in for the seller-side :class:`LiveX402Client` without per-call
    branching. The two clients are intentionally separate classes:

      * ``LiveX402Client`` — seller path; bridges to frames.ag and waits
        for *somebody else's* tx to confirm.
      * ``LiveBuyerX402Client`` — buyer path; loads our own keypair,
        builds the tx, signs locally, submits.

    Sharing one class hid this distinction in Sprint 12; keeping them
    explicit makes the gate-guard wiring (which only ever applies to the
    buyer path) audit-clear.
    """

    supported_networks: tuple[str, ...] = ("solana-mainnet", "solana-devnet")
    facilitator_id: str = "live-buyer-solana"

    def __init__(
        self,
        *,
        network: NetworkConfig | None = None,
        private_key_path: Path | None = None,
        pubkey: str | None = None,
        helius_api_key: SecretStr | None = None,
        confirm_timeout_s: float = 60.0,
        confirm_interval_s: float = 2.0,
    ) -> None:
        self._network = network or resolve_network(os.environ.get("X402_NETWORK"))
        if private_key_path is None or pubkey is None:
            path_env, pubkey_env = buyer_wallet_from_env()
            self._private_key_path = private_key_path or path_env
            self._pubkey = pubkey or pubkey_env
        else:
            self._private_key_path = private_key_path
            self._pubkey = pubkey
        self._helius_key = helius_api_key.get_secret_value() if helius_api_key is not None else None
        self._confirm_timeout_s = confirm_timeout_s
        self._confirm_interval_s = confirm_interval_s

    async def charge(self, intent: PaymentIntent) -> PaymentResult:
        """Sign + submit a USDC transfer for ``intent.amount_usd`` USDC.

        The seller's ``payTo`` address comes from the x402 ``accepts``
        block and is carried on the intent's session record. For V1 we
        read it from ``X402_SELLER_PAYTO`` env to keep the wire shape
        minimal — V2 will plumb it through :class:`PaymentIntent`.
        """
        seller_pay_to = os.environ.get("X402_SELLER_PAYTO", "").strip()
        if not seller_pay_to:
            raise LiveBuyerError(
                "X402_SELLER_PAYTO not set — buyer-side cannot route the "
                "transfer without a seller payee. See flip runbook §3."
            )

        amount_units = _usdc_smallest_units(intent.amount_usd)
        if amount_units <= 0:
            raise LiveBuyerError(
                f"intent amount_usd={intent.amount_usd} rounds to 0 USDC smallest units"
            )

        # Lazy import — solders / solana-py only needed on the buyer path.
        # Keeps the seller-only deploys lean and the gecko-core import
        # graph unaffected for stub-mode callers.
        try:
            from gecko_core.payments._solana_buyer import (  # type: ignore[import-not-found]
                build_and_submit_usdc_transfer,
            )
        except ImportError as exc:
            raise LiveBuyerError(
                "Solana buyer dependencies not installed. Run `uv sync --extra solana-buyer`."
            ) from exc

        try:
            keypair_bytes = self._private_key_path.read_bytes()
        except OSError as exc:
            raise BuyerWalletKeyfileUnreadableError(
                f"cannot read {BUYER_PRIVATE_KEY_PATH_ENV} at {self._private_key_path}: {exc}"
            ) from exc

        tx_signature = await build_and_submit_usdc_transfer(
            keypair_bytes=keypair_bytes,
            from_pubkey=self._pubkey,
            to_pubkey=seller_pay_to,
            amount_units=amount_units,
            cluster=self._network.cluster,
            helius_api_key=self._helius_key,
        )

        # Reuse the seller-side confirmation poller — same RPC contract.
        confirmer = LiveX402Client(
            facilitator_url="",
            wallet_secret=SecretStr(""),
            network=self._network,
            treasury_address=seller_pay_to,
            helius_api_key=self._helius_key,
            confirm_timeout_s=self._confirm_timeout_s,
            confirm_interval_s=self._confirm_interval_s,
        )
        try:
            await confirmer._wait_for_confirmation(tx_signature)
        except ConfirmationTimeoutError:
            raise
        except LiveX402Error:
            raise

        logger.info(
            "live buyer charge confirmed intent_id=%s tx=%s network=%s amount=%s USDC",
            intent.intent_id,
            tx_signature,
            self._network.name,
            intent.amount_usd,
        )
        return PaymentResult(
            intent_id=intent.intent_id,
            status="success",
            tx_signature=tx_signature,
            error=None,
        )

    async def verify(self, tx_signature: str) -> ConfirmationStatus:
        """Delegate verify to the seller-side helper — same RPC, same map."""
        confirmer = LiveX402Client(
            facilitator_url="",
            wallet_secret=SecretStr(""),
            network=self._network,
            treasury_address=self._pubkey,
            helius_api_key=self._helius_key,
        )
        return await confirmer.verify(tx_signature)


__all__ = [
    "BUYER_PRIVATE_KEY_PATH_ENV",
    "BUYER_PUBKEY_ENV",
    "BuyerInsufficientBalanceError",
    "BuyerWalletKeyfileUnreadableError",
    "BuyerWalletNotConfiguredError",
    "FacilitatorRejectedError",
    "LiveBuyerError",
    "LiveBuyerX402Client",
    "buyer_wallet_from_env",
]


def _amount_to_decimal(units: int) -> Decimal:
    """Inverse of ``_usdc_smallest_units`` — kept for fixture asserts."""
    return Decimal(units) / Decimal(1_000_000)
