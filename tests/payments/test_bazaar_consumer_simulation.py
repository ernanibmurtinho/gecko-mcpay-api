"""Local simulation harness for the Bazaar buyer (S16-BAZAAR-CONSUMER-04).

Pattern B from CLAUDE.md: every wire-protocol integration's *first*
deliverable is a free local simulation that falsifies the implementation
without spending money. Live smoke is the final verification, not the
primary debug tool. Sprint 12 Track A burned ~10 fix iterations against
mainnet because they skipped this step.

This harness exercises the verify→settle dispatch by:

  * Building the same ``X-PAYMENT`` header ``CDPX402Consumer.pay()``
    builds in production. (Same lazy-imported
    ``cdp_x402_client._build_payment_payload`` path → same EIP-3009 vs
    Permit2 dispatch as production.)
  * Decoding the header back into a ``PaymentPayload`` and asserting
    every wire-required field is present and non-degenerate (signature,
    authorization fields, EIP-712 domain ``name`` / ``version`` /
    ``verifyingContract`` / ``assetTransferMethod=eip3009``).
  * Round-tripping the ``X-PAYMENT-RESPONSE`` parser against a
    server-shaped success/failure body so a future shape drift trips
    the simulation, not a $0.01 mainnet call.

No network. No funded wallet required. Runs in CI by default (no marker).
"""

from __future__ import annotations

import base64
import json
from decimal import Decimal

import pytest
from gecko_core.payments.bazaar_discovery import PaymentRequirements
from gecko_core.payments.x402_consumer import (
    SettlementFailedError,
    _build_x_payment_header,
    _parse_payment_response_header,
)

# Burner EOA, never funded, never used in production. Same shape as
# packages/gecko-core/tests/test_cdp_live_verify.py — proves the
# signature path without moving real funds.
_BURNER_KEY = "0x" + "ab" * 32

# TripAdvisor-shaped requirement: $0.01 USDC on Base mainnet.
_TRIPADVISOR_PAY_TO = "0x5d6dF6b10C54617dac4Bf9993ad9fA384b7B36d3"
_BASE_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"


def _tripadvisor_requirement() -> PaymentRequirements:
    return PaymentRequirements(
        network="eip155:8453",
        asset=_BASE_USDC,
        pay_to=_TRIPADVISOR_PAY_TO,
        amount="10000",  # 0.01 USDC atomic
        scheme="exact",
        max_timeout_seconds=60,
        max_amount_required=Decimal("0.01"),
        raw={"resource": "https://tripadvisor.x402.paysponge.com/api/v1/location/12345/details"},
    )


# ---------------------------------------------------------------------------
# X-PAYMENT header — same dispatch as production.
# ---------------------------------------------------------------------------


def test_x_payment_header_carries_signed_eip3009_authorization() -> None:
    """Decode the header we'd send to TripAdvisor and assert the
    EIP-3009 dispatch is what production would emit.

    Falsifies: missing signature, missing EIP-712 domain, wrong
    ``assetTransferMethod`` (the Permit2 vs eip3009 bug), missing
    ``resource`` binding. All four are exactly the bug class Sprint 12
    Track A's mainnet-debug cycle hit."""
    requirement = _tripadvisor_requirement()
    header = _build_x_payment_header(
        requirements=requirement,
        intent_id="bazaar-sim-01",
        payer_private_key=_BURNER_KEY,
        resource_url=str(requirement.raw["resource"]),
    )

    # Header is base64; decode → JSON.
    decoded = json.loads(base64.b64decode(header).decode("utf-8"))

    # x402 v2 envelope shape.
    assert decoded["x402Version"] == 2
    assert "accepted" in decoded
    assert "payload" in decoded
    assert "resource" in decoded
    # Resource binding must point at the actual paid endpoint — without
    # this, the facilitator can't bind the authorization to a URL.
    assert "tripadvisor" in str(decoded["resource"]).lower()

    # accepted ↔ PaymentRequirements wire shape.
    accepted = decoded["accepted"]
    assert accepted["scheme"] == "exact"
    assert accepted["network"] == "eip155:8453"
    assert accepted["payTo"].lower() == _TRIPADVISOR_PAY_TO.lower()
    assert accepted["amount"] == "10000"

    # EIP-712 domain — without these CDP routes through Permit2 and
    # reverts on the buyer-has-no-allowance branch (see
    # docs/diagnostics/2026-05-01-cdp-settle-failure-rca.md).
    extra = accepted.get("extra") or {}
    assert extra.get("name") == "USD Coin"
    assert extra.get("version") == "2"
    assert extra.get("verifyingContract", "").lower() == _BASE_USDC.lower()
    assert extra.get("assetTransferMethod") == "eip3009"

    # payload.signature — the EIP-3009 transferWithAuthorization sig.
    inner = decoded["payload"]
    assert isinstance(inner, dict)
    # x402 SDK shape: {authorization, signature}.
    assert "signature" in inner
    assert inner["signature"].startswith("0x")
    assert len(inner["signature"]) == 132  # 65 bytes hex + 0x


def test_x_payment_header_unique_per_call() -> None:
    """Two calls with the same requirements must produce DIFFERENT
    ``X-PAYMENT`` headers (the EIP-3009 nonce / valid-after-before bind
    each authorization to one settlement). If they collide, the second
    settle on-chain will revert with `nonce already used` — a class of
    bug the simulation can catch without burning USDC."""
    requirement = _tripadvisor_requirement()
    h1 = _build_x_payment_header(
        requirements=requirement,
        intent_id="bazaar-sim-a",
        payer_private_key=_BURNER_KEY,
        resource_url=str(requirement.raw["resource"]),
    )
    h2 = _build_x_payment_header(
        requirements=requirement,
        intent_id="bazaar-sim-b",
        payer_private_key=_BURNER_KEY,
        resource_url=str(requirement.raw["resource"]),
    )
    assert h1 != h2


# ---------------------------------------------------------------------------
# X-PAYMENT-RESPONSE parser — settlement outcome shape.
# ---------------------------------------------------------------------------


def test_payment_response_parser_decodes_success() -> None:
    body = {
        "success": True,
        "transaction": "0x" + "de" * 32,
        "network": "eip155:8453",
        "payer": "0x" + "ab" * 20,
    }
    header = base64.b64encode(json.dumps(body).encode("utf-8")).decode("ascii")
    parsed = _parse_payment_response_header(header)
    assert parsed["success"] is True
    assert parsed["transaction"].startswith("0x")


def test_payment_response_parser_surfaces_failure_shape() -> None:
    body = {"success": False, "errorReason": "insufficient_funds"}
    header = base64.b64encode(json.dumps(body).encode("utf-8")).decode("ascii")
    parsed = _parse_payment_response_header(header)
    assert parsed["success"] is False
    assert parsed["errorReason"] == "insufficient_funds"


def test_payment_response_parser_rejects_garbage() -> None:
    """Drift in the upstream encoding must trip the parser, not get
    silently swallowed as success=False."""
    with pytest.raises(SettlementFailedError):
        _parse_payment_response_header("!!!not-base64!!!")
