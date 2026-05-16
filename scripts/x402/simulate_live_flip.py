"""S25 WC-LIVE-SMOKE — no-spend simulator for the x402 live-flip path.

Pattern B (CLAUDE.md): the first deliverable for any wire-protocol
integration is a free local simulation that can falsify the implementation
without spending money. This script is that simulator for the live Solana
buyer-side x402 dispatch.

What it exercises (all entirely in-process, NO real RPC):

  1. Gate guard (``assert_live_settlement_allowed``) admits the configured
     allowlisted tool and rejects every other tool when ``X402_MODE=live``
     and ``LIVE_TOOLS_ALLOWLIST`` is narrowed to one tool.
  2. Kill-switch (``GECKO_X402_KILL_SWITCH=true``) fails-closed regardless
     of allowlist/mode.
  3. Receipt rendering (``build_receipt``) emits the Solscan URL only when
     a non-stub signature comes back from the recorded cassette.
  4. The full ``LiveBuyerX402Client.charge`` happy-path runs against the
     ``tests/integration/fixtures/live_buyer/buyer_charge_success.json``
     cassette via monkey-patched RPC submission helpers.

Run::

    uv run python scripts/x402/simulate_live_flip.py

Exit code 0 = simulation passes; the live-flip code path is internally
consistent. Exit code 1 = at least one assertion failed; do NOT proceed
to live flip without diagnosing.

This script does NOT replace the recorded-fixture contract test under
``tests/integration/test_x402_live_flip_contract.py`` — that's the
Pattern C gate run by pytest. This script is the operator-facing
"smoke before you smoke" check; the contract test is the CI gate.

Hard constraints honoured here:
  * No env var named ``X402_MODE`` is written with value ``live`` — the
    gate guard is called with ``requested_mode="live"`` as an argument,
    not via the process env. The script can NEVER cause a real charge.
  * No real network calls. ``httpx`` / ``solana-py`` are never imported.
  * No SSM access. Operator wallet provisioning lives in the runbook.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = (
    REPO_ROOT / "tests" / "integration" / "fixtures" / "live_buyer" / "buyer_charge_success.json"
)


# ---------------------------------------------------------------------------
# Phase 1 — gate guard checks.
# ---------------------------------------------------------------------------


def check_gate_guard() -> list[str]:
    """Return a list of failure strings; empty list means PASS."""
    failures: list[str] = []
    # Import inside function so this script can fail with a clean message
    # if the worktree is missing the payments module.
    from gecko_core.payments import assert_live_settlement_allowed

    # Save + clear the two guard env vars so tests are deterministic.
    saved_kill = os.environ.pop("GECKO_X402_KILL_SWITCH", None)
    saved_tools = os.environ.pop("GECKO_X402_LIVE_TOOLS", None)

    try:
        # 1. allowlist narrows live to trade_research only.
        os.environ["GECKO_X402_KILL_SWITCH"] = "false"
        os.environ["GECKO_X402_LIVE_TOOLS"] = "gecko_trade_research"

        if assert_live_settlement_allowed("gecko_trade_research", "live") != "live":
            failures.append("gate: allowlisted tool 'gecko_trade_research' was NOT admitted")
        if assert_live_settlement_allowed("gecko_research", "live") != "stub":
            failures.append("gate: non-allowlisted tool 'gecko_research' was admitted (LEAK)")
        if assert_live_settlement_allowed("gecko_trade_research_pro", "live") != "stub":
            failures.append(
                "gate: 'gecko_trade_research_pro' admitted under single-tool allowlist (LEAK)"
            )

        # 2. kill-switch wins over allowlist.
        os.environ["GECKO_X402_KILL_SWITCH"] = "true"
        if assert_live_settlement_allowed("gecko_trade_research", "live") != "stub":
            failures.append("gate: kill-switch did NOT fail-closed for allowlisted tool")
        if assert_live_settlement_allowed("gecko_trade_research", "frames") != "stub":
            failures.append("gate: kill-switch did NOT fail-closed for frames mode")

        # 3. non-live modes always pass through.
        if assert_live_settlement_allowed("anything", "stub") != "stub":
            failures.append("gate: stub mode mutated (must be identity)")
        if assert_live_settlement_allowed("anything", "cdp") != "cdp":
            failures.append("gate: cdp mode mutated (must be identity)")
    finally:
        if saved_kill is None:
            os.environ.pop("GECKO_X402_KILL_SWITCH", None)
        else:
            os.environ["GECKO_X402_KILL_SWITCH"] = saved_kill
        if saved_tools is None:
            os.environ.pop("GECKO_X402_LIVE_TOOLS", None)
        else:
            os.environ["GECKO_X402_LIVE_TOOLS"] = saved_tools

    return failures


# ---------------------------------------------------------------------------
# Phase 2 — receipt rendering.
# ---------------------------------------------------------------------------


def check_receipts() -> list[str]:
    failures: list[str] = []
    from gecko_core.payments.receipts import build_receipt

    # Stub mode: never claim a Solscan URL.
    r = build_receipt(tx_signature=None, x402_mode="stub")
    if r["settlement_mode"] != "stub" or r["solscan_url"] is not None:
        failures.append(f"receipt: stub mode leaked URL: {r!r}")

    # Stub artifact (defensive): still stub even if mode says live.
    r = build_receipt(tx_signature="stub-deadbeef", x402_mode="live")
    if r["settlement_mode"] != "stub" or r["solscan_url"] is not None:
        failures.append(f"receipt: stub artifact leaked URL: {r!r}")

    # Live + real sig: Solscan URL is rendered.
    sig = "5KJp" + "1" * 80
    r = build_receipt(tx_signature=sig, x402_mode="live")
    if r["settlement_mode"] != "live":
        failures.append(f"receipt: live sig produced wrong mode: {r!r}")
    if not r["solscan_url"] or "solscan.io/tx/" not in r["solscan_url"]:
        failures.append(f"receipt: missing Solscan URL: {r!r}")
    if r["tx_signature"] != sig:
        failures.append(f"receipt: sig roundtrip failed: {r!r}")

    return failures


# ---------------------------------------------------------------------------
# Phase 3 — replay the buyer charge against the recorded cassette.
# ---------------------------------------------------------------------------


async def check_buyer_replay() -> list[str]:
    """Drive LiveBuyerX402Client through the cassette without network."""
    failures: list[str] = []

    if not FIXTURE.exists():
        return [f"replay: cassette missing at {FIXTURE}"]

    cassette = json.loads(FIXTURE.read_text())
    expected_sig = cassette["tx_signature"]

    # We patch the two seams that would otherwise hit a real RPC:
    #   (a) the buyer-side ``build_and_submit_usdc_transfer`` import target;
    #   (b) the seller-side confirmation poller ``_wait_for_confirmation``.
    # Both are exercised inside LiveBuyerX402Client.charge — patching one
    # without the other would silently leave a real-RPC seam open, which is
    # exactly the Pattern B/C failure mode this script defends against.

    import sys as _sys
    import types

    # (a) Fake the lazy-imported solana buyer module BEFORE the client tries
    # to import it. The real module is optional and not installed in dev.
    fake_mod = types.ModuleType("gecko_core.payments._solana_buyer")

    async def _fake_submit(**kwargs: object) -> str:
        # Sanity: every kwarg the production code passes must be present.
        required = {
            "keypair_bytes",
            "from_pubkey",
            "to_pubkey",
            "amount_units",
            "cluster",
            "helius_api_key",
        }
        missing = required - set(kwargs.keys())
        if missing:
            raise AssertionError(f"submit kwargs missing: {missing}")
        if kwargs["amount_units"] != 250_000:  # $0.25 → 250000 USDC units
            raise AssertionError(f"unexpected amount_units: {kwargs['amount_units']}")
        return expected_sig

    fake_mod.build_and_submit_usdc_transfer = _fake_submit  # type: ignore[attr-defined]
    _sys.modules["gecko_core.payments._solana_buyer"] = fake_mod

    # (b) Stub the confirmation poll to a no-op success.
    from gecko_core.payments import x402_client as _x402_client_mod

    async def _fake_wait(self: object, sig: str) -> None:
        return None

    original_wait = _x402_client_mod.LiveX402Client._wait_for_confirmation
    _x402_client_mod.LiveX402Client._wait_for_confirmation = _fake_wait  # type: ignore[assignment]

    # Write a temp keypair-file the client can read; contents don't matter
    # because the fake submit doesn't parse them.
    tmp_key = Path("/tmp/gecko_buyer_simkey.json")
    tmp_key.write_bytes(b"[]")

    saved_path = os.environ.get("BUYER_WALLET_PRIVATE_KEY_PATH")
    saved_pub = os.environ.get("BUYER_WALLET_PUBKEY")
    saved_payto = os.environ.get("X402_SELLER_PAYTO")
    os.environ["BUYER_WALLET_PRIVATE_KEY_PATH"] = str(tmp_key)
    os.environ["BUYER_WALLET_PUBKEY"] = cassette["buyer_pubkey"]
    os.environ["X402_SELLER_PAYTO"] = cassette["seller_pay_to"]

    try:
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

        if result.status != "success":
            failures.append(f"replay: charge status != success ({result.status})")
        if result.tx_signature != expected_sig:
            failures.append(
                f"replay: sig mismatch (got {result.tx_signature!r}, expected {expected_sig!r})"
            )

        # Receipt on the recorded sig should claim live settlement.
        from gecko_core.payments.receipts import build_receipt

        r = build_receipt(tx_signature=result.tx_signature, x402_mode="live")
        if r["settlement_mode"] != "live":
            failures.append(f"replay: receipt did not claim live: {r!r}")
        if not r["solscan_url"]:
            failures.append("replay: receipt missing Solscan URL on live sig")
    finally:
        _x402_client_mod.LiveX402Client._wait_for_confirmation = original_wait  # type: ignore[assignment]
        for k, v in (
            ("BUYER_WALLET_PRIVATE_KEY_PATH", saved_path),
            ("BUYER_WALLET_PUBKEY", saved_pub),
            ("X402_SELLER_PAYTO", saved_payto),
        ):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        with contextlib.suppress(OSError):
            tmp_key.unlink()

    return failures


# ---------------------------------------------------------------------------
# Driver.
# ---------------------------------------------------------------------------


def main() -> int:
    print("== x402 live-flip simulation (no-spend) ==")

    sections = [
        ("Phase 1: gate guard (allowlist + kill-switch)", check_gate_guard()),
        ("Phase 2: receipt rendering", check_receipts()),
        ("Phase 3: buyer charge replay against cassette", asyncio.run(check_buyer_replay())),
    ]

    total_failures = 0
    for label, failures in sections:
        if failures:
            print(f"FAIL  {label}")
            for f in failures:
                print(f"  - {f}")
            total_failures += len(failures)
        else:
            print(f"PASS  {label}")

    print()
    if total_failures:
        print(f"SIMULATION FAILED — {total_failures} assertion(s) failed. DO NOT FLIP.")
        return 1
    print("SIMULATION PASSED — live-flip code path is internally consistent.")
    print("Next gates: pytest contract test + operator runbook + founder go-ahead.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
