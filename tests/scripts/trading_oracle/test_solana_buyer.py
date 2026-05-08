"""Tests for the Phase 7 Solana-side x402 buyer.

Light fakes only — see feedback_lighter_tests.md. Pure helpers are
exercised directly (network detection, accepts[] selection, payload
shape). The end-to-end ``request`` flow is NOT tested here — it would
require fakes for the SDK's RPC client + signer, which over-mocks. The
pure helpers + network branch routing are sufficient to defend the
public seam.
"""

from __future__ import annotations

import importlib.util
import sys
from decimal import Decimal
from pathlib import Path

import pytest

# Load the script-as-module pair the same way other trading_oracle tests do.
_SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "scripts" / "trading_oracle"

# Make scripts/trading_oracle/ importable so ``solana_buyer`` can find its
# sibling-module imports. test_service_call_specs.py uses this pattern.
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

_RUN_PATH = _SCRIPTS_DIR / "run.py"
_run_spec = importlib.util.spec_from_file_location("trading_oracle_run", _RUN_PATH)
assert _run_spec is not None and _run_spec.loader is not None
run_mod = importlib.util.module_from_spec(_run_spec)
sys.modules["trading_oracle_run"] = run_mod
_run_spec.loader.exec_module(run_mod)

_BUYER_PATH = _SCRIPTS_DIR / "solana_buyer.py"
_buyer_spec = importlib.util.spec_from_file_location("solana_buyer", _BUYER_PATH)
assert _buyer_spec is not None and _buyer_spec.loader is not None
buyer_mod = importlib.util.module_from_spec(_buyer_spec)
sys.modules["solana_buyer"] = buyer_mod
_buyer_spec.loader.exec_module(buyer_mod)


# ---------------------------------------------------------------------------
# 1. Keypair loading via solders.
# ---------------------------------------------------------------------------


def test_solana_keypair_loads_from_base58() -> None:
    """``load_solana_signer`` returns a signer whose ``address`` is the pubkey.

    Generates a fresh keypair (no real funds), exports its base58 secret,
    feeds it through ``load_solana_signer``, and asserts the signer's
    address matches the keypair's pubkey. Skips cleanly if ``solders`` /
    ``x402[svm]`` aren't installed in this environment.
    """
    pytest.importorskip("solders")
    pytest.importorskip("x402.mechanisms.svm.signers")
    from solders.keypair import Keypair  # type: ignore[import-not-found]

    kp = Keypair()
    secret_b58 = str(kp)  # solders ``__str__`` returns the base58 keypair
    expected_pubkey = str(kp.pubkey())

    signer = buyer_mod.load_solana_signer(secret_b58)
    assert str(signer.address) == expected_pubkey


# ---------------------------------------------------------------------------
# 2. + 3. Network branch routing — _build_paid_requester picks the right adapter.
# ---------------------------------------------------------------------------


def _set_min_env(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    """Set the minimum env required for a successful ``_build_paid_requester``."""
    base = {
        "GECKO_X402_MODE": "live",
        "TWITSH_WALLET_ADDRESS": "0x0000000000000000000000000000000000000001",
        "TWITSH_WALLET_PRIVATE_KEY": "0x" + "11" * 32,  # syntactically valid EVM key
    }
    base.update(overrides)
    for k, v in base.items():
        monkeypatch.setenv(k, v)
    # Reset the singleton so each call re-reads env.
    run_mod._PAID_REQUESTER_SINGLETON = None


def test_solana_buyer_routing_picks_solana_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    """``X402_NETWORK=solana-mainnet`` → ``LiveSolanaPaidRequester``."""
    pytest.importorskip("solders")
    from solders.keypair import Keypair  # type: ignore[import-not-found]

    kp = Keypair()
    _set_min_env(
        monkeypatch,
        X402_NETWORK="solana-mainnet",
        GECKO_SOLANA_WALLET_ADDRESS=str(kp.pubkey()),
        GECKO_SOLANA_WALLET_PRIVATE_KEY=str(kp),
    )
    requester = run_mod._build_paid_requester()
    assert isinstance(requester, buyer_mod.LiveSolanaPaidRequester)


def test_solana_buyer_routing_picks_evm_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    """``X402_NETWORK=base-mainnet`` (default) → ``_LiveX402PaidRequester``."""
    _set_min_env(monkeypatch, X402_NETWORK="base-mainnet")
    requester = run_mod._build_paid_requester()
    # The EVM adapter is the existing Phase 6 class — sanity-check the type
    # name without importing it (it's a private nested class accessed by
    # attribute on run_mod).
    assert isinstance(requester, run_mod._LiveX402PaidRequester)
    assert not isinstance(requester, buyer_mod.LiveSolanaPaidRequester)


# ---------------------------------------------------------------------------
# 4. accepts[] selection + payload shape.
# ---------------------------------------------------------------------------


# This is the exact accepts[] entry observed against
# https://x402.api.agentmail.to/v0/inboxes on 2026-05-08 (Phase 7 probe).
# The presence of EVM siblings forces the buyer to filter by network.
_REAL_PAYSH_ACCEPTS: list[dict[str, object]] = [
    {
        "scheme": "exact",
        "network": "eip155:8453",
        "amount": "0",
        "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "payTo": "0x6e3184C204e596dED89E8A5693B602097F4Ab687",
        "maxTimeoutSeconds": 300,
        "extra": {"name": "USD Coin", "version": "2"},
    },
    {
        "scheme": "exact",
        "network": "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",
        "amount": "10000",  # 0.01 USDC at 6 decimals
        "asset": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        "payTo": "7r4e5dwNS68MDaxbw7N8jbzHq7RCMBp9z6smHFH4NXWw",
        "maxTimeoutSeconds": 300,
        "extra": {"feePayer": "2wKupLR9q6wXYppw8Gr2NvWxKBUqm4PPJKkQfoxHDBg4"},
    },
]


def test_pick_solana_accepts_entry_filters_by_network() -> None:
    """``pick_solana_accepts_entry`` returns the Solana row from a mixed list."""
    chosen = buyer_mod.pick_solana_accepts_entry(_REAL_PAYSH_ACCEPTS)
    assert str(chosen["network"]).startswith("solana:")
    assert chosen["asset"] == "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def test_pick_solana_accepts_entry_raises_when_evm_only() -> None:
    """When no Solana entry is advertised, raise rather than misroute."""
    evm_only = [_REAL_PAYSH_ACCEPTS[0]]
    with pytest.raises(RuntimeError, match="no Solana network"):
        buyer_mod.pick_solana_accepts_entry(evm_only)


def test_is_solana_network_env() -> None:
    """Detector handles CLI-style names AND raw CAIP-2."""
    assert buyer_mod.is_solana_network_env("solana-mainnet")
    assert buyer_mod.is_solana_network_env("solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp")
    assert not buyer_mod.is_solana_network_env("base-mainnet")
    assert not buyer_mod.is_solana_network_env("eip155:8453")
    assert not buyer_mod.is_solana_network_env(None)
    assert not buyer_mod.is_solana_network_env("")


def test_solana_payment_payload_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """Payload shape: PaymentRequirements is built from accepts[] correctly.

    We mock the ``ExactSvmScheme.create_payment_payload`` (which would
    otherwise hit Solana RPC for mint metadata) to a fixed dict, and
    assert the surrounding wrapper carries the expected v2 shape +
    Solana-specific fields. This is the seam test, NOT an end-to-end
    signing test.
    """
    pytest.importorskip("x402.schemas")
    from x402.mechanisms.svm.exact import client as svm_client_mod  # type: ignore[import-not-found]

    captured: dict[str, object] = {}

    def fake_create_payment_payload(self: object, requirements: object) -> dict[str, object]:
        captured["requirements"] = requirements
        return {"transaction": "base64-encoded-fake-tx-for-test"}

    monkeypatch.setattr(
        svm_client_mod.ExactSvmScheme,
        "create_payment_payload",
        fake_create_payment_payload,
    )

    # Tiny stand-in signer — ExactSvmScheme.__init__ only stores the ref;
    # our fake payload-builder ignores it.
    class _FakeSigner:
        address = "11111111111111111111111111111111"

    chosen = buyer_mod.pick_solana_accepts_entry(_REAL_PAYSH_ACCEPTS)
    payload = buyer_mod.build_solana_payment_payload(
        accepts_entry=chosen,
        signer=_FakeSigner(),
        resource_url="https://example.com/x402",
    )

    # v2 envelope shape.
    assert payload.x402_version == 2
    assert payload.accepted.scheme == "exact"
    assert str(payload.accepted.network) == "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"
    assert payload.accepted.asset == "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    assert payload.accepted.pay_to == "7r4e5dwNS68MDaxbw7N8jbzHq7RCMBp9z6smHFH4NXWw"
    # extra.feePayer must round-trip into PaymentRequirements — the SDK
    # raises ValueError without it.
    assert payload.accepted.extra["feePayer"] == "2wKupLR9q6wXYppw8Gr2NvWxKBUqm4PPJKkQfoxHDBg4"
    # Inner SVM payload (transaction blob) is what the mock returned.
    assert payload.payload == {"transaction": "base64-encoded-fake-tx-for-test"}
    assert payload.resource is not None
    assert str(payload.resource.url) == "https://example.com/x402"
    # And the requirements actually went through the SDK as expected.
    assert captured["requirements"] is payload.accepted


def test_address_keypair_mismatch_rejects(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mismatched declared address vs keypair pubkey is a hard error."""
    pytest.importorskip("solders")
    from solders.keypair import Keypair  # type: ignore[import-not-found]

    kp = Keypair()
    bogus_addr = "11111111111111111111111111111111"  # not the keypair's pubkey
    with pytest.raises(ValueError, match="does not match"):
        buyer_mod.LiveSolanaPaidRequester(
            payer_private_key_b58=str(kp),
            payer_address=bogus_addr,
            network="solana-mainnet",
            per_call_hard_limit_usd=Decimal("0.10"),
        )
