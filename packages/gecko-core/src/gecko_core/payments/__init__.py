"""Payments — x402 on Solana with stub/live/frames modes.

Owned by `web3-engineer`. Stub by default; live mode requires explicit
env config. See `docs/implementation-plan.md` Phase 4.
"""

import logging
import os
from typing import Any, Final

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Live-flip gate guard (S24 WS-C Task #4).
#
# Defence-in-depth — even if X402_MODE=live is set globally in env, we only
# honour it when BOTH:
#
#   (a) GECKO_X402_KILL_SWITCH is not "true" (case-insensitive); and
#   (b) the called tool name appears in the GECKO_X402_LIVE_TOOLS allowlist
#       (comma-separated env var, e.g. "gecko_trade_research,gecko_trade_research_pro").
#
# Any other state → fall back to stub silently and emit a structured
# ``x402.live_blocked`` log event. This protects against:
#
#   * Misconfigured env (kill-switch flipped on but X402_MODE still live).
#   * A new tool added without an explicit allowlist entry accidentally
#     charging real money.
#   * Operator forgetting to flip kill-switch back during an incident.
#
# The /research surface is intentionally NEVER in the default allowlist
# (per S24 plan §4 WS-C: `/research` stays stub for the foreseeable).
# ---------------------------------------------------------------------------


KILL_SWITCH_ENV: Final = "GECKO_X402_KILL_SWITCH"
LIVE_TOOLS_ENV: Final = "GECKO_X402_LIVE_TOOLS"


def _kill_switch_engaged() -> bool:
    return os.environ.get(KILL_SWITCH_ENV, "").strip().lower() == "true"


def _live_tool_allowlist() -> frozenset[str]:
    raw = os.environ.get(LIVE_TOOLS_ENV, "")
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def assert_live_settlement_allowed(tool_name: str, requested_mode: str) -> str:
    """Gate-guard. Returns the effective x402 mode for this dispatch.

    If the caller requested ``"live"`` (or ``"frames"``, which aliases to
    the live bridge today) the guard checks the kill-switch + allowlist.
    On a fail it logs a structured ``x402.live_blocked`` event and returns
    ``"stub"`` so the dispatcher swaps to :class:`StubX402Client`. The
    caller never sees an exception — the swap is silent on purpose so a
    misconfig at the env layer doesn't 5xx the route.

    Any non-live ``requested_mode`` (``stub``, ``cdp``, etc.) is returned
    unchanged — the guard only narrows the live lane.

    Idempotent + import-time-cheap so callers can invoke it on every
    request without overhead.
    """
    if requested_mode not in ("live", "frames"):
        return requested_mode

    if _kill_switch_engaged():
        logger.warning(
            "x402.live_blocked tool=%s mode=%s reason=kill_switch_engaged "
            "kill_switch_env=%s live_tools_env=%s",
            tool_name,
            requested_mode,
            KILL_SWITCH_ENV,
            LIVE_TOOLS_ENV,
        )
        _record_live_blocked(tool_name, requested_mode, "kill_switch_engaged")
        return "stub"

    allowlist = _live_tool_allowlist()
    if tool_name not in allowlist:
        logger.warning(
            "x402.live_blocked tool=%s mode=%s reason=not_in_allowlist allowlist=%s",
            tool_name,
            requested_mode,
            sorted(allowlist),
        )
        _record_live_blocked(
            tool_name, requested_mode, "not_in_allowlist", allowlist=sorted(allowlist)
        )
        return "stub"

    return requested_mode


def _record_live_blocked(tool: str, requested_mode: str, reason: str, **extra: Any) -> None:
    """S24 WS-D — emit `x402.live_blocked` to the events ledger in addition
    to the existing log line. Lazy import keeps `payments/__init__.py`
    import-time cheap and avoids a circular import on cold paths.
    """
    try:
        from gecko_core.observability import emit_event_sync

        payload: dict[str, Any] = {
            "tool": tool,
            "requested_mode": requested_mode,
            "reason": reason,
        }
        payload.update(extra)
        emit_event_sync("x402.live_blocked", payload)
    except Exception:  # pragma: no cover - never block on observability
        logger.debug("x402.live_blocked ledger write failed", exc_info=True)


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
from gecko_core.payments.creator_payout import (
    DEFAULT_PER_CITE_USD as CREATOR_PAYOUT_DEFAULT_PER_CITE_USD,
)
from gecko_core.payments.creator_payout import (
    aggregate_creator_payouts,
    resolve_per_cite_amount_usd,
    settle_creator_payouts,
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
from gecko_core.payments.verdict_settle import (
    VERDICT_DETAIL_PRICE_USDC,
    VERDICT_SETTLE_LIVE_ENV,
    InvalidVerdictPaymentError,
    VerdictPaymentError,
    VerdictPaywallNotLiveError,
    is_verdict_settle_live_enabled,
    make_verdict_payment_requirement,
    resolve_verdict_settle_mode,
    verify_verdict_payment,
)
from gecko_core.payments.verdict_settle import (
    PaymentRequirements as VerdictPaymentRequirements,
)
from gecko_core.payments.verdict_settle import (
    SettlementReceipt as VerdictSettlementReceipt,
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
    "CREATOR_PAYOUT_DEFAULT_PER_CITE_USD",
    "KILL_SWITCH_ENV",
    "LIVE_TOOLS_ENV",
    "NETWORKS",
    "PAYMENT_MODES",
    "VERDICT_DETAIL_PRICE_USDC",
    "VERDICT_SETTLE_LIVE_ENV",
    "CDPAuthProvider",
    "CDPCredentials",
    "CDPFacilitatorClient",
    "CDPNotConfiguredError",
    "CDPSettleError",
    "CDPX402Client",
    "CDPX402Error",
    "ConfirmationStatus",
    "FramesX402Client",
    "InvalidVerdictPaymentError",
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
    "VerdictPaymentError",
    "VerdictPaymentRequirements",
    "VerdictPaywallNotLiveError",
    "VerdictSettlementReceipt",
    "VerifyResult",
    "VerifyTarget",
    "X402Client",
    "X402Mode",
    "aggregate_creator_payouts",
    "assert_live_settlement_allowed",
    "build_cdp_facilitator_client",
    "facilitator_id_for_network",
    "get_client",
    "is_stub_signature",
    "is_unconfigured",
    "is_verdict_settle_live_enabled",
    "make_verdict_payment_requirement",
    "price_for",
    "resolve_client",
    "resolve_client_for_network",
    "resolve_network",
    "resolve_per_cite_amount_usd",
    "resolve_rpc_url",
    "resolve_verdict_settle_mode",
    "run_payment_gate",
    "settle_creator_payouts",
    "summarize",
    "verify_target",
    "verify_targets",
    "verify_verdict_payment",
]
