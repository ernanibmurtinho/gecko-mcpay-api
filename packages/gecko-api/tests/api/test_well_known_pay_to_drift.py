"""S12.5-TEST-03 — schema-drift guard for /.well-known/x402 payTo addresses.

For each route advertised on /.well-known/x402, the `payTo` address MUST be
well-formed for the route's `network`:

  * `eip155:*` (Base mainnet/Sepolia)  →  0x-prefixed 40-hex EVM address
  * `solana*`                          →  base58, no 0x prefix

Sprint 12 cae61ed shipped a bug where _build_routes blindly used the Solana
GECKO_WALLET_ADDRESS for ALL routes, including eip155:8453. CDP attempted
the on-chain USDC transferWithAuthorization, parsed the Solana base58
string as raw bytes against the EVM ABI, and failed with a generic
"transfer amount exceeds balance" — masking the true "invalid destination"
error.

This test catches that class of bug at unit-test time. Run twice — once
under each network — so both branches of the per-network selection in
_build_routes get exercised.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

# Address format regexes — kept loose enough to accept stub-mode addresses
# (any non-empty placeholder) while still rejecting cross-network use.
_EVM_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
# Solana base58: valid alphabet excludes 0/O/I/l. Length range 32–44 covers
# all real pubkeys plus the gecko-wallet stub. Excludes anything starting
# with 0x.
_SOLANA_ADDRESS_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


@pytest.fixture
def evm_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """Build the API with X402_NETWORK=base-mainnet so /.well-known/x402
    advertises eip155:* routes. payTo MUST be a 0x address."""
    monkeypatch.setenv("X402_MODE", "stub")
    monkeypatch.setenv("X402_NETWORK", "base-mainnet")
    monkeypatch.setenv(
        "GECKO_WALLET_ADDRESS_BASE",
        "0x1234567890abcdef1234567890abcdef12345678",
    )
    # Solana wallet still set — we want to verify _build_routes prefers the
    # base address on eip155:* routes EVEN WHEN the Solana one is non-empty.
    # This is exactly the cae61ed regression: pre-fix code used the Solana
    # one anyway. Use a clearly-Solana base58 string to make the assertion
    # crisp.
    monkeypatch.setenv(
        "GECKO_WALLET_ADDRESS",
        "GeckoSoanaWaetAddressBase58Sampe1234567xyz9",
    )
    for mod in [m for m in sys.modules if m.startswith("gecko_api")]:
        sys.modules.pop(mod, None)
    from gecko_api.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def solana_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """Solana-network app under stub mode. payTo MUST be a base58 (Solana)
    address."""
    monkeypatch.setenv("X402_MODE", "stub")
    monkeypatch.setenv("X402_NETWORK", "solana-devnet")
    monkeypatch.setenv(
        "GECKO_WALLET_ADDRESS",
        "GeckoSoanaWaetAddressBase58Sampe1234567xyz9",
    )
    monkeypatch.delenv("GECKO_WALLET_ADDRESS_BASE", raising=False)
    for mod in [m for m in sys.modules if m.startswith("gecko_api")]:
        sys.modules.pop(mod, None)
    from gecko_api.main import app

    with TestClient(app) as c:
        yield c


def _assert_address_matches_network(network: str, pay_to: str) -> None:
    """Loose-but-meaningful well-formedness check.

    eip155:* must look like an EVM address (0x + 40 hex). Solana must NOT
    have an 0x prefix and must be in the base58 alphabet. Anything else
    (the special STUB_WALLET_ADDRESS_NOT_FOR_LIVE sentinel) is allowed only
    if the address actually equals that sentinel — sentinel use is intended
    for cases where neither wallet is configured.
    """
    if pay_to == "STUB_WALLET_ADDRESS_NOT_FOR_LIVE":
        return
    if network.startswith("eip155:"):
        assert _EVM_ADDRESS_RE.match(pay_to), (
            f"network={network} but payTo={pay_to!r} is not an EVM address. "
            "Did _build_routes use GECKO_WALLET_ADDRESS (Solana) on a "
            "Base/EVM route? See cae61ed."
        )
    elif network.startswith("solana") or network == "solana-mainnet":
        assert not pay_to.startswith("0x"), (
            f"network={network} but payTo={pay_to!r} looks like an EVM "
            "address. Solana routes need a base58 address."
        )
        assert _SOLANA_ADDRESS_RE.match(pay_to), (
            f"network={network} but payTo={pay_to!r} is not base58."
        )
    else:
        pytest.fail(f"unknown network family: {network!r}")


def test_well_known_pay_to_well_formed_for_evm_routes(evm_client: TestClient) -> None:
    """Every eip155:* route on /.well-known/x402 must advertise an EVM payTo.

    THIS would have caught cae61ed before the live smoke."""
    response = evm_client.get("/.well-known/x402")
    assert response.status_code == 200
    catalog = response.json()
    assert catalog["routes"], "no routes registered — env priming broken?"
    saw_eip155 = False
    for route in catalog["routes"]:
        for opt in route["accepts"]:
            network = opt["network"]
            pay_to = opt["payTo"]
            if network.startswith("eip155:"):
                saw_eip155 = True
            _assert_address_matches_network(network, pay_to)
    assert saw_eip155, "no eip155:* routes — base-mainnet env did not take effect"


def test_well_known_pay_to_well_formed_for_solana_routes(solana_client: TestClient) -> None:
    """Every solana* route on /.well-known/x402 must advertise a base58
    payTo (no 0x prefix)."""
    response = solana_client.get("/.well-known/x402")
    assert response.status_code == 200
    catalog = response.json()
    assert catalog["routes"]
    saw_solana = False
    for route in catalog["routes"]:
        for opt in route["accepts"]:
            network = opt["network"]
            pay_to = opt["payTo"]
            if network.startswith("solana"):
                saw_solana = True
            _assert_address_matches_network(network, pay_to)
    assert saw_solana, "no solana* routes registered"


def test_evm_route_uses_base_wallet_not_solana_when_both_set(
    evm_client: TestClient,
) -> None:
    """Direct regression for cae61ed: when both wallets are present, the
    eip155:* route MUST advertise the BASE wallet, never the Solana one.

    Pre-fix code blindly used `gecko_wallet_address` for every network,
    which on Base routes meant CDP got a Solana base58 string interpreted
    as raw EVM-ABI bytes — generic "transfer amount exceeds balance" error
    masking the real cause."""
    response = evm_client.get("/.well-known/x402")
    assert response.status_code == 200
    expected_base = os.environ["GECKO_WALLET_ADDRESS_BASE"]
    saw_eip155 = False
    for route in response.json()["routes"]:
        for opt in route["accepts"]:
            if opt["network"].startswith("eip155:"):
                saw_eip155 = True
                assert opt["payTo"] == expected_base, (
                    f"eip155 route advertised payTo={opt['payTo']!r}, "
                    f"expected the GECKO_WALLET_ADDRESS_BASE value "
                    f"{expected_base!r}. cae61ed regression: code is using "
                    "the Solana wallet on a Base/EVM route."
                )
    assert saw_eip155, "no eip155:* routes — env priming broken"
