"""S24 WS-C Task #4 — contract test for the live Solana buyer-side x402 path.

Pattern C (CLAUDE.md): every X402Client conformer ships with a recorded-
fixture contract test against the real facilitator's relevant endpoints.
Sprint 12 CDP failed because `/verify` (a pure signature check) passed
while `/settle` (the dispatch branch) failed in prod — fixture tests must
exercise the actual money-moving path, not just signature validation.

This test mirrors the `live_cdp` Pattern-C template from S12.5-TEST-04
(see `packages/gecko-core/tests/test_cdp_live_verify.py`) for the
Solana buyer lane.

Two run modes:

  * **Replay (CI default):** loads JSON fixtures under
    ``tests/integration/fixtures/live_buyer/`` and asserts the client
    produces the expected ``PaymentResult`` for each. No network access.
  * **Live re-record:** requires ``GECKO_LIVE_SOLANA=1`` + funded buyer
    wallet env + ``--record`` flag. Hits real Solana RPC + facilitator.
    Used by operator after a code change to ``live_buyer.py`` before
    deploy. Never runs in CI.

Until the Day-9 flip recording lands, the success-path fixture is a stub
placeholder and the live invocation is gated behind ``GECKO_LIVE_SOLANA``.
The test still validates the import path, marker registration, gate-guard
contract, and env-resolution helpers — i.e. everything that doesn't
require real-network state.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

pytestmark = pytest.mark.live_solana


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "live_buyer"


def _live_recording_enabled() -> bool:
    return (
        os.environ.get("GECKO_LIVE_SOLANA") == "1"
        and bool(os.environ.get("BUYER_WALLET_PRIVATE_KEY_PATH"))
        and bool(os.environ.get("BUYER_WALLET_PUBKEY"))
        and bool(os.environ.get("X402_SELLER_PAYTO"))
    )


# ---------------------------------------------------------------------------
# Replay-mode tests — run in CI without any network access.
# ---------------------------------------------------------------------------


def test_buyer_charge_success_fixture_present() -> None:
    """Sanity: the happy-path cassette exists and has the expected shape.

    A more meaningful assertion than it looks — Pattern B forbids
    `_build_payment_payload`-style stubs from shipping without a local
    falsification path. The cassette IS the local falsification path; if
    it goes missing we want the next CI run to fail loudly.
    """
    fixture = FIXTURE_DIR / "buyer_charge_success.json"
    assert fixture.exists(), f"missing cassette: {fixture}"
    data = json.loads(fixture.read_text())
    assert "tx_signature" in data
    assert "rpc_calls" in data
    methods = [call["method"] for call in data["rpc_calls"]]
    assert "sendTransaction" in methods
    assert "getSignatureStatuses" in methods


def test_buyer_env_resolution_raises_on_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """``buyer_wallet_from_env`` must fail loudly with a remediation hint
    rather than silently returning a half-resolved wallet config — the
    runbook §3 wiring is what fixes the error, so the message has to
    point there."""
    from gecko_core.payments.live_buyer import (
        BuyerWalletNotConfiguredError,
        buyer_wallet_from_env,
    )

    monkeypatch.delenv("BUYER_WALLET_PRIVATE_KEY_PATH", raising=False)
    monkeypatch.delenv("BUYER_WALLET_PUBKEY", raising=False)

    with pytest.raises(BuyerWalletNotConfiguredError) as excinfo:
        buyer_wallet_from_env()
    assert "BUYER_WALLET_PRIVATE_KEY_PATH" in str(excinfo.value)
    assert "BUYER_WALLET_PUBKEY" in str(excinfo.value)
    assert "runbook" in str(excinfo.value).lower()


def test_gate_guard_blocks_live_when_kill_switch_engaged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defence-in-depth: even with a perfectly-configured allowlist, a
    flipped kill-switch downgrades live → stub silently."""
    from gecko_core.payments import assert_live_settlement_allowed

    monkeypatch.setenv("GECKO_X402_KILL_SWITCH", "true")
    monkeypatch.setenv("GECKO_X402_LIVE_TOOLS", "gecko_trade_research")

    effective = assert_live_settlement_allowed("gecko_trade_research", "live")
    assert effective == "stub"


def test_gate_guard_blocks_live_for_tools_not_in_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`/research` MUST NOT live-settle even if X402_MODE=live globally."""
    from gecko_core.payments import assert_live_settlement_allowed

    monkeypatch.setenv("GECKO_X402_KILL_SWITCH", "false")
    monkeypatch.setenv("GECKO_X402_LIVE_TOOLS", "gecko_trade_research")

    effective = assert_live_settlement_allowed("gecko_research", "live")
    assert effective == "stub"


def test_gate_guard_passes_live_for_allowlisted_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gecko_core.payments import assert_live_settlement_allowed

    monkeypatch.setenv("GECKO_X402_KILL_SWITCH", "false")
    monkeypatch.setenv("GECKO_X402_LIVE_TOOLS", "gecko_trade_research,gecko_trade_research_pro")

    assert assert_live_settlement_allowed("gecko_trade_research", "live") == "live"
    assert assert_live_settlement_allowed("gecko_trade_research_pro", "live") == "live"


def test_gate_guard_leaves_non_live_modes_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    from gecko_core.payments import assert_live_settlement_allowed

    monkeypatch.setenv("GECKO_X402_KILL_SWITCH", "true")
    # Kill-switch is ON but mode is stub — guard must leave it alone.
    assert assert_live_settlement_allowed("anything", "stub") == "stub"
    assert assert_live_settlement_allowed("anything", "cdp") == "cdp"


# ---------------------------------------------------------------------------
# Live-record mode — opt-in, runs against real mainnet RPC + facilitator.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _live_recording_enabled(),
    reason=(
        "live Solana buyer-path probe disabled. Set GECKO_LIVE_SOLANA=1 + "
        "BUYER_WALLET_PRIVATE_KEY_PATH + BUYER_WALLET_PUBKEY + "
        "X402_SELLER_PAYTO to opt in. This burns real USDC."
    ),
)
async def test_live_buyer_charge_round_trip() -> None:
    """End-to-end: real signed USDC transfer on Solana, real RPC poll.

    Runs only on operator opt-in. Burns the smallest amount the seller's
    pricing table accepts (typically $0.01-$0.25 for trade_research).
    Used to verify a code change to ``LiveBuyerX402Client`` before flip.
    """
    from decimal import Decimal

    from gecko_core.payments.live_buyer import LiveBuyerX402Client
    from gecko_core.payments.models import PaymentIntent

    client = LiveBuyerX402Client()
    intent = PaymentIntent(
        intent_id=str(uuid4()),
        session_id=uuid4(),
        tier="basic",
        amount_usd=Decimal("0.01"),
    )
    result = await client.charge(intent)
    assert result.status == "success"
    assert result.tx_signature
    # Don't assert on signature shape beyond non-empty — Solana sigs are
    # base58, length varies. The fact that confirmation poll returned
    # is the load-bearing signal.
