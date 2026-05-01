"""S12.5-TEST-04 — gated integration test against the real CDP facilitator.

Sprint 12 Track A unit tests passed because they used a stub facilitator
and a placeholder payload. The actual CDP wire path was never exercised
end-to-end pre-prod. Subsequent fixes f9f0135, c850b7d, 2e93b27, 2ca40b9
each addressed a payload-shape or signing-key-format bug that would have
been caught by a single round-trip against `/verify`.

CDP `/verify` is read-only and free: it accepts the same payload as
`/settle` and reports whether it would have settled, without moving funds
or burning gas. This test runs the production code path
(`_build_payment_requirements` + `_build_payment_payload` + the real CDP
`HTTPFacilitatorClient`) and posts to `/verify`. Any 4xx error response
class that maps to "your payload is malformed" fails the test loudly.

Marker-gated: skipped unless `CDP_API_KEY_ID` is set AND
`GECKO_CDP_LIVE_VERIFY=1` is explicit-opt-in. Standard CI never burns the
credential. Operator runs this manually before any deploy that touches
`gecko_core.payments.cdp_x402_client` or `cdp.py`.

Usage:
    GECKO_CDP_LIVE_VERIFY=1 \
    CDP_API_KEY_ID=<id> CDP_API_KEY_SECRET=<secret> \
    uv run pytest packages/gecko-core/tests/test_cdp_live_verify.py -m live_cdp -v
"""

from __future__ import annotations

import os
from decimal import Decimal

import pytest
from gecko_core.payments.cdp import (
    CDPCredentials,
    build_cdp_facilitator_client,
)
from gecko_core.payments.cdp_x402_client import (
    BASE_MAINNET_NETWORK_ID,
    BASE_MAINNET_USDC_CONTRACT,
    CDP_FACILITATOR_BASE_URL,
    _build_payment_payload,
    _build_payment_requirements,
)

# A burner EOA private key used ONLY to produce a structurally-valid
# EIP-3009 signature. Funds are never required because we hit /verify, not
# /settle. /verify will return success=False ("insufficient balance" / "no
# allowance") — that's expected and proves the payload reached the right
# code path. Generated locally; not in any wallet, not funded.
_BURNER_KEY = "0x" + "ab" * 32

_TREASURY = "0x000000000000000000000000000000000000dead"

pytestmark = pytest.mark.live_cdp


def _live_verify_enabled() -> bool:
    return (
        os.environ.get("GECKO_CDP_LIVE_VERIFY") == "1"
        and bool(os.environ.get("CDP_API_KEY_ID"))
        and bool(os.environ.get("CDP_API_KEY_SECRET"))
    )


@pytest.mark.skipif(
    not _live_verify_enabled(),
    reason=(
        "live CDP /verify probe disabled. Set GECKO_CDP_LIVE_VERIFY=1 + "
        "CDP_API_KEY_ID + CDP_API_KEY_SECRET to opt in. /verify is free."
    ),
)
async def test_cdp_verify_accepts_production_payload_shape() -> None:
    """End-to-end: build the same payload `CDPX402Client.charge` would build,
    JWT-sign with the real CDP credentials, hit `/verify`.

    We do NOT assert success=True. /verify will say success=False because
    the burner wallet is unfunded; we assert that the response is well-
    formed (no 400 'invalid_payload' / 'missing field' / 'malformed
    signature' errors). Any 4xx-shape failure means the payload structure
    has drifted — which is exactly the bug class 2e93b27 (missing
    ResourceInfo), 2ca40b9 (missing EIP-712 domain), and a13c65e (missing
    signature) introduced in production."""
    creds = CDPCredentials.from_env_values(
        os.environ["CDP_API_KEY_ID"],
        os.environ["CDP_API_KEY_SECRET"],
    )
    facilitator = build_cdp_facilitator_client(
        creds, base_url=CDP_FACILITATOR_BASE_URL, identifier="cdp-live-verify-test"
    )

    requirements = _build_payment_requirements(
        amount_usd=Decimal("0.01"),
        pay_to=_TREASURY,
        network=BASE_MAINNET_NETWORK_ID,
        asset=BASE_MAINNET_USDC_CONTRACT,
        resource_url="https://geckovision.tech/research",
        max_timeout_seconds=60,
    )
    payload = _build_payment_payload(
        intent_id="cdp-verify-smoke",
        requirements=requirements,
        payer_private_key=_BURNER_KEY,
        resource_url="https://geckovision.tech/research",
    )

    # Real network call. /verify is free.
    try:
        response = await facilitator.verify(payload, requirements)
    finally:
        await facilitator.aclose()

    # CDP responds with a structured object regardless of accept/reject —
    # if our payload was malformed, the SDK raises before getting here
    # (HTTP 400 with 'invalid_payload' surfaces as an exception). Reaching
    # this point means the shape passed CDP's pre-flight validation.
    assert response is not None
    # The response shape is `{success: bool, errorReason?: str, ...}`.
    # We DO assert that if success=False, the error is a fund-related
    # reason (not a payload-shape complaint). Anything mentioning
    # "invalid_payload", "missing", "malformed", or "domain" is a
    # regression we want to catch.
    success = bool(getattr(response, "success", False))
    if not success:
        reason = (
            getattr(response, "error_reason", None) or getattr(response, "errorReason", None) or ""
        )
        message = (
            getattr(response, "error_message", None)
            or getattr(response, "errorMessage", None)
            or ""
        )
        bad_shape_markers = (
            "invalid_payload",
            "missing",
            "malformed",
            "domain",
            "resource",
            "extra",
        )
        text = f"{reason} {message}".lower()
        for marker in bad_shape_markers:
            assert marker not in text, (
                f"CDP /verify rejected payload shape (reason={reason!r}, "
                f"message={message!r}). This is exactly the bug class "
                "f9f0135 / 2e93b27 / 2ca40b9 fixed — payload structure has "
                "drifted from what CDP expects. Inspect "
                "gecko_core.payments.cdp_x402_client._build_payment_payload "
                "and _build_payment_requirements."
            )
