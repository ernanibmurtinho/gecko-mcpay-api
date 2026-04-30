"""LiveX402Client — frames.ag bridge + Helius confirmation poll.

We never hit the real frames.ag or any live Solana RPC here. respx mocks
both surfaces so the test exercises the full code path:

  PaymentIntent → POST /actions/transfer-solana → txHash
                → POST <rpc> getSignatureStatuses → confirmed
                → PaymentResult(success, tx_signature=<real>)

Coverage:
  * happy path — devnet, real-looking signature returned
  * wallet config missing
  * treasury (`GECKO_WALLET_ADDRESS`) missing
  * frames.ag → 401 (apiToken expired) → FramesAuthError
  * frames.ag → INSUFFICIENT_FUNDS → InsufficientBalanceError
  * frames.ag → NETWORK_MISMATCH → NetworkMismatchError
  * frames.ag → 5xx → FramesUnavailableError
  * Solana RPC times out → ConfirmationTimeoutError (after 1 retry)
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import respx
from gecko_core.payments import PaymentIntent
from gecko_core.payments.networks import NETWORKS
from gecko_core.payments.x402_client import (
    AGENT_WALLET_CONFIG,
    ConfirmationTimeoutError,
    FramesAuthError,
    FramesUnavailableError,
    InsufficientBalanceError,
    LiveX402Client,
    NetworkMismatchError,
    TreasuryNotConfiguredError,
    WalletNotConfiguredError,
)
from pydantic import SecretStr

# Constants used across the tests.
FRAMES_BASE = "https://frames.test/api"
TREASURY = "GeckoTreasury11111111111111111111111111111"
USERNAME = "tester"
WALLET_ADDR = "WalletAddr2222222222222222222222222222222222"
API_TOKEN = "mf_test_token_NOT_A_REAL_SECRET"
DEVNET_RPC = "https://api.devnet.solana.com"
FAKE_SIG = "5ZkS1RealLookingSignatureXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"


@pytest.fixture
def wallet_config_path(tmp_path: Path) -> Path:
    """Write a fake `~/.agentwallet/config.json` and return its path."""
    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps(
            {
                "apiToken": API_TOKEN,
                "username": USERNAME,
                "solanaAddress": WALLET_ADDR,
                "evmAddress": "0xdeadbeef",
            }
        )
    )
    return cfg


def _intent(amount: str = "0.10") -> PaymentIntent:
    return PaymentIntent(
        intent_id=f"intent-{uuid4()}",
        session_id=uuid4(),
        tier="basic",
        amount_usd=Decimal(amount),
    )


def _make_client(
    *,
    config_path: Path,
    treasury: str | None = TREASURY,
    confirm_timeout_s: float = 5.0,
    confirm_interval_s: float = 0.01,
) -> LiveX402Client:
    return LiveX402Client(
        facilitator_url="",
        wallet_secret=SecretStr(""),
        frames_base_url=FRAMES_BASE,
        network=NETWORKS["solana-devnet"],
        treasury_address=treasury,
        helius_api_key=None,  # public devnet RPC
        config_path=config_path,
        confirm_timeout_s=confirm_timeout_s,
        confirm_interval_s=confirm_interval_s,
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_charge_success_devnet(wallet_config_path: Path) -> None:
    client = _make_client(config_path=wallet_config_path)
    intent = _intent("0.10")

    transfer_url = f"{FRAMES_BASE}/wallets/{USERNAME}/actions/transfer-solana"

    async with respx.mock(assert_all_called=True) as router:
        transfer_route = router.post(transfer_url).mock(
            return_value=httpx.Response(
                200,
                json={
                    "actionId": "act_1",
                    "status": "submitted",
                    "txHash": FAKE_SIG,
                    "explorer": f"https://explorer.solana.com/tx/{FAKE_SIG}?cluster=devnet",
                },
            )
        )
        rpc_route = router.post(DEVNET_RPC).mock(
            return_value=httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {
                        "context": {"slot": 1},
                        "value": [
                            {
                                "slot": 1,
                                "confirmations": 10,
                                "err": None,
                                "confirmationStatus": "confirmed",
                            }
                        ],
                    },
                },
            )
        )

        result = await client.charge(intent)

    assert result.status == "success"
    assert result.tx_signature == FAKE_SIG
    assert not result.tx_signature.startswith("stub_")
    assert result.error is None
    assert result.intent_id == intent.intent_id

    # Frames was called exactly once with USDC, devnet, 100000 smallest units (= $0.10).
    assert transfer_route.call_count == 1
    sent = json.loads(transfer_route.calls[0].request.content)
    assert sent["asset"] == "usdc"
    assert sent["network"] == "devnet"
    assert sent["amount"] == "100000"
    assert sent["to"] == TREASURY
    assert sent["walletAddress"] == WALLET_ADDR
    assert rpc_route.called


# ---------------------------------------------------------------------------
# Pre-flight failures (no network)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_wallet_config(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist.json"
    client = _make_client(config_path=missing)
    with pytest.raises(WalletNotConfiguredError, match=r"frames\.ag credentials not found"):
        await client.charge(_intent())


@pytest.mark.asyncio
async def test_missing_treasury(wallet_config_path: Path) -> None:
    client = _make_client(config_path=wallet_config_path, treasury=None)
    with pytest.raises(TreasuryNotConfiguredError, match="GECKO_WALLET_ADDRESS"):
        await client.charge(_intent())


# ---------------------------------------------------------------------------
# frames.ag error mapping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apitoken_expired_maps_to_auth_error(wallet_config_path: Path) -> None:
    client = _make_client(config_path=wallet_config_path)
    transfer_url = f"{FRAMES_BASE}/wallets/{USERNAME}/actions/transfer-solana"

    async with respx.mock(assert_all_called=True) as router:
        router.post(transfer_url).mock(
            return_value=httpx.Response(
                401,
                json={"error": "UNAUTHORIZED", "message": "apiToken expired"},
            )
        )
        with pytest.raises(FramesAuthError, match="apiToken"):
            await client.charge(_intent())


@pytest.mark.asyncio
async def test_insufficient_funds(wallet_config_path: Path) -> None:
    client = _make_client(config_path=wallet_config_path)
    transfer_url = f"{FRAMES_BASE}/wallets/{USERNAME}/actions/transfer-solana"

    async with respx.mock(assert_all_called=True) as router:
        router.post(transfer_url).mock(
            return_value=httpx.Response(
                402,
                json={"code": "INSUFFICIENT_FUNDS", "message": "balance 0.00 USDC"},
            )
        )
        with pytest.raises(InsufficientBalanceError, match="insufficient USDC balance"):
            await client.charge(_intent())


@pytest.mark.asyncio
async def test_network_mismatch(wallet_config_path: Path) -> None:
    client = _make_client(config_path=wallet_config_path)
    transfer_url = f"{FRAMES_BASE}/wallets/{USERNAME}/actions/transfer-solana"

    async with respx.mock(assert_all_called=True) as router:
        router.post(transfer_url).mock(
            return_value=httpx.Response(
                400,
                json={"code": "NETWORK_MISMATCH", "message": "wallet on mainnet, requested devnet"},
            )
        )
        with pytest.raises(NetworkMismatchError, match="network mismatch"):
            await client.charge(_intent())


@pytest.mark.asyncio
async def test_frames_5xx_unavailable(wallet_config_path: Path) -> None:
    client = _make_client(config_path=wallet_config_path)
    transfer_url = f"{FRAMES_BASE}/wallets/{USERNAME}/actions/transfer-solana"

    async with respx.mock(assert_all_called=True) as router:
        router.post(transfer_url).mock(
            return_value=httpx.Response(503, json={"error": "upstream", "message": "bad gateway"})
        )
        with pytest.raises(FramesUnavailableError, match="503"):
            await client.charge(_intent())


# ---------------------------------------------------------------------------
# Solana RPC failure paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confirmation_timeout(wallet_config_path: Path) -> None:
    """Submission succeeds but RPC never reports the signature → timeout."""
    client = _make_client(
        config_path=wallet_config_path,
        confirm_timeout_s=0.05,  # tight — one or two polls max
        confirm_interval_s=0.02,
    )
    transfer_url = f"{FRAMES_BASE}/wallets/{USERNAME}/actions/transfer-solana"

    async with respx.mock(assert_all_called=False) as router:
        router.post(transfer_url).mock(
            return_value=httpx.Response(
                200, json={"actionId": "a", "status": "submitted", "txHash": FAKE_SIG}
            )
        )
        # RPC always reports "not seen yet" (value=[None])
        router.post(DEVNET_RPC).mock(
            return_value=httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {"context": {"slot": 1}, "value": [None]},
                },
            )
        )

        with pytest.raises(ConfirmationTimeoutError, match="not confirmed within"):
            await client.charge(_intent())


@pytest.mark.asyncio
async def test_rpc_retries_once_then_fails(wallet_config_path: Path) -> None:
    """Two consecutive RPC failures → ConfirmationTimeoutError."""
    client = _make_client(
        config_path=wallet_config_path,
        confirm_timeout_s=2.0,
        confirm_interval_s=0.01,
    )
    transfer_url = f"{FRAMES_BASE}/wallets/{USERNAME}/actions/transfer-solana"

    async with respx.mock(assert_all_called=False) as router:
        router.post(transfer_url).mock(
            return_value=httpx.Response(
                200, json={"actionId": "a", "status": "submitted", "txHash": FAKE_SIG}
            )
        )
        router.post(DEVNET_RPC).mock(return_value=httpx.Response(500, text="rpc broken"))

        with pytest.raises(ConfirmationTimeoutError, match="failed twice"):
            await client.charge(_intent())


# ---------------------------------------------------------------------------
# Default config_path is the canonical agentwallet path (not test-overridden).
# We don't read this file in the test — just assert the constant.
# ---------------------------------------------------------------------------


def test_default_config_path_is_agentwallet() -> None:
    assert AGENT_WALLET_CONFIG.name == "config.json"
    assert AGENT_WALLET_CONFIG.parent.name == ".agentwallet"
