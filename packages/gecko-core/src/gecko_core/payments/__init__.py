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
from gecko_core.payments.x402_client import (
    FramesX402Client,
    LiveX402Client,
    StubX402Client,
    X402Client,
    X402Mode,
    get_client,
)

__all__ = [
    "NETWORKS",
    "CDPAuthProvider",
    "CDPCredentials",
    "CDPFacilitatorClient",
    "FramesX402Client",
    "LiveX402Client",
    "NetworkConfig",
    "NetworkName",
    "PaymentIntent",
    "PaymentRequiredError",
    "PaymentResult",
    "StubX402Client",
    "X402Client",
    "X402Mode",
    "build_cdp_facilitator_client",
    "get_client",
    "is_unconfigured",
    "price_for",
    "resolve_network",
    "run_payment_gate",
]
