"""S25 WC-LIVE-SMOKE — Pattern C contract test for the x402 live-flip path.

Marked ``live_x402_verdict`` so the conftest auto-marker rule deselects it
from the default ``uv run pytest`` invocation (per pyproject ``addopts``
``-m 'not ... and not live_x402_verdict and ...'``). Opt in with::

    uv run pytest tests/integration/test_x402_live_flip_contract.py \\
        -m live_x402_verdict --no-header

This is the Pattern C gate. Sprint 12 CDP shipped passing tests against
``/verify`` (a pure signature check) while ``/settle`` (the dispatch
branch) failed in prod. To prevent that here:

  * We exercise the FULL ``LiveBuyerX402Client.charge`` dispatch — submit
    AND confirmation poll. Verify-only is not enough.
  * The recorded cassette under
    ``tests/integration/fixtures/live_buyer/buyer_charge_success.json``
    holds the expected RPC method sequence (``sendTransaction`` +
    ``getSignatureStatuses``). Both must be exercised by the test.
  * Gate guard + receipt rendering are tested with the same env state the
    runbook §3 sets in the ECS task definition, so a flip-time misconfig
    falsifies this test before it falsifies prod.

Replay-only by default — does NOT touch real Solana RPC or facilitator.
Live re-record is the operator-side ``GECKO_LIVE_SOLANA=1`` path in the
sibling ``test_live_buyer_path.py`` module; this test never opts into
that even with the env set, because its job is fixture-replay only.
"""

from __future__ import annotations

import json
import sys
import types
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

pytestmark = pytest.mark.live_x402_verdict


FIXTURE = Path(__file__).parent / "fixtures" / "live_buyer" / "buyer_charge_success.json"


@pytest.fixture
def cassette() -> dict:
    assert FIXTURE.exists(), f"missing cassette: {FIXTURE}"
    return json.loads(FIXTURE.read_text())


@pytest.fixture
def patched_solana_buyer(cassette: dict, monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Patch the lazy-imported solana buyer module + capture RPC method calls.

    Returns the list of recorded RPC method names so a test can assert the
    *dispatch branch* fired — not just signature validation.
    """
    rpc_calls: list[dict] = []

    fake_mod = types.ModuleType("gecko_core.payments._solana_buyer")

    async def _fake_submit(**kwargs: object) -> str:
        rpc_calls.append({"method": "sendTransaction", "kwargs": dict(kwargs)})
        return cassette["tx_signature"]

    fake_mod.build_and_submit_usdc_transfer = _fake_submit  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "gecko_core.payments._solana_buyer", fake_mod)

    # Patch the seller-side confirmation poller — this is the second RPC call
    # in the cassette (``getSignatureStatuses``). Sprint 12 missed exactly
    # this seam in CDP; we record it here so a regression that skips
    # confirmation shows up as an empty rpc_calls list rather than a green
    # test.
    from gecko_core.payments import x402_client as _x402_client_mod

    async def _fake_wait(self: object, sig: str) -> None:
        rpc_calls.append({"method": "getSignatureStatuses", "sig": sig})

    monkeypatch.setattr(
        _x402_client_mod.LiveX402Client,
        "_wait_for_confirmation",
        _fake_wait,
    )

    return rpc_calls


@pytest.fixture
def buyer_env(tmp_path: Path, cassette: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    """Mount the buyer wallet env the way the ECS task does in live mode."""
    keyfile = tmp_path / "buyer.json"
    keyfile.write_bytes(b"[]")
    monkeypatch.setenv("BUYER_WALLET_PRIVATE_KEY_PATH", str(keyfile))
    monkeypatch.setenv("BUYER_WALLET_PUBKEY", cassette["buyer_pubkey"])
    monkeypatch.setenv("X402_SELLER_PAYTO", cassette["seller_pay_to"])
    monkeypatch.setenv("X402_NETWORK", "solana-mainnet")


# ---------------------------------------------------------------------------
# Dispatch-branch contract — verify AND settle, not just verify.
# ---------------------------------------------------------------------------


async def test_buyer_charge_exercises_full_dispatch_branch(
    cassette: dict,
    patched_solana_buyer: list[dict],
    buyer_env: None,
) -> None:
    """The cassette declares two RPC calls (sendTransaction + getSignatureStatuses).
    A passing test MUST exercise both — Sprint 12 lesson."""
    from gecko_core.payments.live_buyer import LiveBuyerX402Client
    from gecko_core.payments.models import PaymentIntent

    client = LiveBuyerX402Client()
    intent = PaymentIntent(
        intent_id=cassette["intent"]["intent_id"],
        session_id=uuid4(),
        tier=cassette["intent"]["tier"],
        amount_usd=Decimal(cassette["intent"]["amount_usd"]),
    )

    result = await client.charge(intent)

    assert result.status == "success"
    assert result.tx_signature == cassette["tx_signature"]

    methods = [call["method"] for call in patched_solana_buyer]
    expected_methods = [c["method"] for c in cassette["rpc_calls"]]
    assert methods == expected_methods, (
        f"dispatch branch did not match cassette: got {methods}, expected {expected_methods}"
    )

    # Confirm the submit call carried the right amount + cluster — these are
    # the most common Sprint-12-style silent-misconfig vectors.
    submit = patched_solana_buyer[0]["kwargs"]
    assert submit["amount_units"] == 250_000  # $0.25 → 250000 USDC smallest units
    assert submit["cluster"] == "mainnet-beta"
    assert submit["to_pubkey"] == cassette["seller_pay_to"]
    assert submit["from_pubkey"] == cassette["buyer_pubkey"]


# ---------------------------------------------------------------------------
# Gate guard + receipt — runbook §3 env in, expected behaviour out.
# ---------------------------------------------------------------------------


def test_runbook_env_admits_trade_research_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replicate the ECS task definition env the runbook §3 sets. The
    guard MUST admit gecko_trade_research(+_pro) and reject everything
    else, including the /research surface."""
    from gecko_core.payments import assert_live_settlement_allowed

    monkeypatch.setenv("GECKO_X402_KILL_SWITCH", "false")
    monkeypatch.setenv("GECKO_X402_LIVE_TOOLS", "gecko_trade_research,gecko_trade_research_pro")

    assert assert_live_settlement_allowed("gecko_trade_research", "live") == "live"
    assert assert_live_settlement_allowed("gecko_trade_research_pro", "live") == "live"
    assert assert_live_settlement_allowed("gecko_research", "live") == "stub"
    assert assert_live_settlement_allowed("unknown_tool", "live") == "stub"


def test_kill_switch_engaged_fails_closed_for_all_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rollback lever — runbook §7 flips this. All live requests fall
    back to stub regardless of allowlist contents."""
    from gecko_core.payments import assert_live_settlement_allowed

    monkeypatch.setenv("GECKO_X402_KILL_SWITCH", "true")
    monkeypatch.setenv("GECKO_X402_LIVE_TOOLS", "gecko_trade_research,gecko_trade_research_pro")

    assert assert_live_settlement_allowed("gecko_trade_research", "live") == "stub"
    assert assert_live_settlement_allowed("gecko_trade_research_pro", "live") == "stub"
    assert assert_live_settlement_allowed("gecko_trade_research", "frames") == "stub"


def test_receipt_envelope_only_claims_live_on_real_signature(
    cassette: dict,
) -> None:
    """Stub-mode + stub-prefixed sigs must NEVER claim a Solscan URL.
    Real sigs must roundtrip with a working deep link."""
    from gecko_core.payments.receipts import build_receipt

    stub = build_receipt(tx_signature=None, x402_mode="stub")
    assert stub["settlement_mode"] == "stub"
    assert stub["solscan_url"] is None

    leaky = build_receipt(tx_signature="stub-cafebabe", x402_mode="live")
    assert leaky["settlement_mode"] == "stub", "stub artifact must not promote to live"
    assert leaky["solscan_url"] is None

    real = build_receipt(tx_signature=cassette["tx_signature"], x402_mode="live")
    assert real["settlement_mode"] == "live"
    assert real["solscan_url"] is not None
    assert real["solscan_url"].endswith(cassette["tx_signature"])
    assert real["solscan_url"].startswith("https://solscan.io/tx/")
