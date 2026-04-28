"""Tests for gecko_core.wallets.privy (S2-05).

No live network calls. We mock Privy's REST surface with respx so the
tests assert request shape (method/path/headers/body) rather than relying
on the real API.
"""

from __future__ import annotations

import base64
from datetime import datetime

import httpx
import pytest
import respx
from gecko_core.wallets.privy import (
    PrivyClient,
    PrivyClientError,
    PrivyNotConfiguredError,
    PrivyWallet,
    is_privy_configured,
)

PRIVY_BASE = "https://api.privy.io"


# ---------------------------------------------------------------------------
# Sentinel detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "app_id, app_secret, expected",
    [
        ("app123", "secret123", True),
        ("", "secret123", False),
        ("app123", "", False),
        ("__unset__", "secret123", False),
        ("app123", "__unset__", False),
        ("__dev_change_me__", "secret", False),
        ("  app123  ", "  secret  ", True),  # stripped before check
    ],
)
def test_is_privy_configured_sentinels(
    app_id: str | None,
    app_secret: str | None,
    expected: bool,
) -> None:
    assert is_privy_configured(app_id=app_id, app_secret=app_secret) is expected


def test_is_privy_configured_falls_back_to_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Args=None falls back to the process environment; sentinels still detected."""
    monkeypatch.delenv("PRIVY_APP_ID", raising=False)
    monkeypatch.delenv("PRIVY_APP_SECRET", raising=False)
    assert is_privy_configured(app_id=None, app_secret=None) is False
    monkeypatch.setenv("PRIVY_APP_ID", "real-id")
    monkeypatch.setenv("PRIVY_APP_SECRET", "real-secret")
    assert is_privy_configured(app_id=None, app_secret=None) is True
    monkeypatch.setenv("PRIVY_APP_SECRET", "__unset__")
    assert is_privy_configured(app_id=None, app_secret=None) is False


def test_constructor_refuses_sentinel() -> None:
    with pytest.raises(PrivyNotConfiguredError):
        PrivyClient(app_id="__unset__", app_secret="real-secret")
    with pytest.raises(PrivyNotConfiguredError):
        PrivyClient(app_id="real-app", app_secret="")


# ---------------------------------------------------------------------------
# Auth header construction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_headers_basic_plus_privy_app_id() -> None:
    client = PrivyClient(app_id="cmapp123", app_secret="topsecret")
    try:
        headers = client._auth_headers()
    finally:
        await client.aclose()

    expected = base64.b64encode(b"cmapp123:topsecret").decode()
    assert headers["Authorization"] == f"Basic {expected}"
    assert headers["privy-app-id"] == "cmapp123"
    assert headers["Content-Type"] == "application/json"


# ---------------------------------------------------------------------------
# create_solana_wallet — request shape + response parsing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_solana_wallet_sends_external_id_no_owner() -> None:
    async with respx.mock(base_url=PRIVY_BASE, assert_all_called=True) as router:
        route = router.post("/v1/wallets").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "wallet-abc",
                    "address": "SoLaNa1111111111111111111111111111111111111",
                    "chain_type": "solana",
                    "created_at": "2026-04-28T12:00:00Z",
                },
            )
        )
        async with PrivyClient(app_id="cmapp", app_secret="sec") as client:
            wallet = await client.create_solana_wallet(owner_label="proj-uuid-123")

    assert isinstance(wallet, PrivyWallet)
    assert wallet.wallet_id == "wallet-abc"
    assert wallet.address.startswith("SoLaNa")
    assert wallet.chain_type == "solana"
    assert isinstance(wallet.created_at, datetime)

    # Inspect the request shape: app-owned wallet, no `owner` field.
    call = route.calls.last
    body = call.request.read()
    import json as _json

    parsed = _json.loads(body)
    assert parsed == {"chain_type": "solana", "external_id": "proj-uuid-123"}
    assert "owner" not in parsed
    # Auth headers present.
    assert call.request.headers["privy-app-id"] == "cmapp"
    assert call.request.headers["Authorization"].startswith("Basic ")


@pytest.mark.asyncio
async def test_create_solana_wallet_propagates_4xx_verbatim() -> None:
    async with respx.mock(base_url=PRIVY_BASE, assert_all_called=True) as router:
        router.post("/v1/wallets").mock(
            return_value=httpx.Response(
                409,
                json={"error": "external_id already exists"},
            )
        )
        async with PrivyClient(app_id="cmapp", app_secret="sec") as client:
            with pytest.raises(PrivyClientError) as excinfo:
                await client.create_solana_wallet(owner_label="dup")

    # Body preserved verbatim — callers above key off this message.
    assert "409" in str(excinfo.value)
    assert "external_id" in str(excinfo.value)


@pytest.mark.asyncio
async def test_get_wallet_returns_parsed_model() -> None:
    async with respx.mock(base_url=PRIVY_BASE, assert_all_called=True) as router:
        router.get("/v1/wallets/wallet-abc").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "wallet-abc",
                    "address": "Sol123",
                    "chain_type": "solana",
                },
            )
        )
        async with PrivyClient(app_id="cmapp", app_secret="sec") as client:
            wallet = await client.get_wallet("wallet-abc")

    assert wallet.wallet_id == "wallet-abc"
    assert wallet.address == "Sol123"


@pytest.mark.asyncio
async def test_create_rejects_non_solana_chain_response() -> None:
    """Defensive: if Privy somehow returns chain_type != solana, fail loud."""
    async with respx.mock(base_url=PRIVY_BASE, assert_all_called=True) as router:
        router.post("/v1/wallets").mock(
            return_value=httpx.Response(
                200,
                json={"id": "w1", "address": "0xabc", "chain_type": "ethereum"},
            )
        )
        async with PrivyClient(app_id="cmapp", app_secret="sec") as client:
            with pytest.raises(PrivyClientError, match="chain_type"):
                await client.create_solana_wallet(owner_label="x")


@pytest.mark.asyncio
async def test_get_wallet_balance_not_implemented() -> None:
    """Balance reads must go via Solana RPC, not Privy — until that wires up."""
    async with PrivyClient(app_id="cmapp", app_secret="sec") as client:
        with pytest.raises(NotImplementedError):
            await client.get_wallet_balance("wallet-abc")
