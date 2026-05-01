"""Payments — x402 on Solana with stub/live/frames modes.

Owned by `web3-engineer`. Stub by default; live mode requires explicit
env config. See `docs/implementation-plan.md` Phase 4.
"""

from gecko_core.payments.cdp import (
    CDPAuthProvider,
    CDPCredentials,
    CDPFacilitatorClient,
    build_cdp_facilitator_client,
    is_unconfigured,
)
from gecko_core.payments.cdp_x402_client import (
    BASE_MAINNET_NETWORK_ID,
    BASE_MAINNET_USDC_CONTRACT,
    BASE_SEPOLIA_NETWORK_ID,
    CDP_FACILITATOR_BASE_URL,
    CDPNotConfiguredError,
    CDPSettleError,
    CDPX402Client,
    CDPX402Error,
)
from gecko_core.payments.factory import (
    CLOUDFLARE_NETWORK_ID,
    resolve_client,
)
from gecko_core.payments.gate import run_payment_gate
from gecko_core.payments.models import (
    PaymentIntent,
    PaymentRequiredError,
    PaymentResult,
)
from gecko_core.payments.networks import (
    NETWORKS,
    NetworkConfig,
    NetworkName,
    resolve_network,
)
from gecko_core.payments.pricing import price_for
from gecko_core.payments.protocol import (
    ConfirmationStatus,
    PaymentReceipt,
)
from gecko_core.payments.verifier import (
    VerifyResult,
    VerifyTarget,
    is_stub_signature,
    resolve_rpc_url,
    summarize,
    verify_target,
    verify_targets,
)
from gecko_core.payments.x402_client import (
    PAYMENT_MODES,
    FramesX402Client,
    LiveX402Client,
    NetworkKind,
    PaymentMode,
    StubX402Client,
    X402Client,
    X402Mode,
    facilitator_id_for_network,
    get_client,
    resolve_client_for_network,
)

__all__ = [
    "BASE_MAINNET_NETWORK_ID",
    "BASE_MAINNET_USDC_CONTRACT",
    "BASE_SEPOLIA_NETWORK_ID",
    "CDP_FACILITATOR_BASE_URL",
    "CLOUDFLARE_NETWORK_ID",
    "NETWORKS",
    "PAYMENT_MODES",
    "CDPAuthProvider",
    "CDPCredentials",
    "CDPFacilitatorClient",
    "CDPNotConfiguredError",
    "CDPSettleError",
    "CDPX402Client",
    "CDPX402Error",
    "ConfirmationStatus",
    "FramesX402Client",
    "LiveX402Client",
    "NetworkConfig",
    "NetworkKind",
    "NetworkName",
    "PaymentIntent",
    "PaymentMode",
    "PaymentReceipt",
    "PaymentRequiredError",
    "PaymentResult",
    "StubX402Client",
    "VerifyResult",
    "VerifyTarget",
    "X402Client",
    "X402Mode",
    "build_cdp_facilitator_client",
    "facilitator_id_for_network",
    "get_client",
    "is_stub_signature",
    "is_unconfigured",
    "price_for",
    "resolve_client",
    "resolve_client_for_network",
    "resolve_network",
    "resolve_rpc_url",
    "run_payment_gate",
    "summarize",
    "verify_target",
    "verify_targets",
]
