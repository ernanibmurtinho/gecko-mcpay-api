"""S2-02 / S2-03 — X402_NETWORK registry + CDP facilitator wiring.

Covers the post-grant Sprint 2 acceptance criteria:

* `solana-devnet` resolves to the existing devnet config (no behavioural
  change — pinned by the explicit chain-id assert).
* `solana-mainnet` resolves to mainnet + CDP facilitator URL.
* Legacy CAIP-2 form (`solana:EtW...`) still works with a deprecation
  warning. Lets one-deploy SSM values cross the upgrade.
* Unknown network → ValueError at startup with a clear message.
* Mainnet + sentinel CDP creds → RuntimeError naming both env vars.
* `_build_facilitator(devnet)` returns the existing devnet HTTP client.
* `_build_facilitator(mainnet)` returns a CDP-authenticated HTTP client.

The CDP signing path is unit-tested via `_sign_jwt` round-trip (decode +
inspect claims). We do NOT make live HTTP calls — until real CDP creds and
mainnet onboarding land, byte-for-byte server compat is a separate runbook
step.
"""

from __future__ import annotations

import os
import sys
import warnings

import pytest

# Always exercise the registry from clean state.
from gecko_core.payments.cdp import (
    CDPAuthProvider,
    CDPCredentials,
    _sign_jwt,
    is_unconfigured,
)
from gecko_core.payments.networks import NETWORKS, resolve_network

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# Devnet/mainnet CAIP-2 chain ids; copied verbatim so a typo in the registry
# breaks this test instead of silently shipping.
_DEVNET_CHAIN_ID = "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1"
_MAINNET_CHAIN_ID = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"
_DEVNET_FACILITATOR = "https://www.x402.org/facilitator"
_MAINNET_FACILITATOR = "https://api.cdp.coinbase.com/platform/v2/x402"

# Ed25519 PEM keypair generated at test time. We never commit a real CDP key.
# Generated once and cached at module level so the JWT-roundtrip tests are
# deterministic across runs. Cryptography lib does the heavy lifting.


def _gen_ed25519_pem() -> tuple[str, str]:
    """Mint a fresh Ed25519 PEM keypair for JWT round-trip tests."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    priv = Ed25519PrivateKey.generate()
    priv_pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    pub_pem = (
        priv.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return priv_pem, pub_pem


_ED25519_PRIV_PEM, _ED25519_PUB_PEM = _gen_ed25519_pem()


def _purge_settings_cache() -> None:
    """Drop the cached gecko_api modules so `Settings.from_env()` re-reads."""
    for mod in [m for m in sys.modules if m.startswith("gecko_api")]:
        sys.modules.pop(mod, None)


# ---------------------------------------------------------------------------
# Network registry
# ---------------------------------------------------------------------------


def test_devnet_resolves_to_existing_chain_id_and_facilitator() -> None:
    cfg = resolve_network("solana-devnet")
    assert cfg.name == "solana-devnet"
    assert cfg.chain_id == _DEVNET_CHAIN_ID
    assert cfg.facilitator_url == _DEVNET_FACILITATOR
    assert cfg.cluster == "devnet"


def test_mainnet_resolves_to_cdp_facilitator() -> None:
    cfg = resolve_network("solana-mainnet")
    assert cfg.name == "solana-mainnet"
    assert cfg.chain_id == _MAINNET_CHAIN_ID
    assert cfg.facilitator_url == _MAINNET_FACILITATOR
    assert cfg.cluster == "mainnet-beta"


def test_legacy_caip2_devnet_translates_with_deprecation_warning() -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        cfg = resolve_network(_DEVNET_CHAIN_ID)
    assert cfg.name == "solana-devnet"
    assert any(issubclass(w.category, DeprecationWarning) for w in caught), (
        "expected DeprecationWarning for legacy CAIP-2 X402_NETWORK value"
    )


def test_legacy_caip2_mainnet_translates() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        cfg = resolve_network(_MAINNET_CHAIN_ID)
    assert cfg.name == "solana-mainnet"
    assert cfg.facilitator_url == _MAINNET_FACILITATOR


def test_unknown_network_raises_with_valid_options_in_message() -> None:
    with pytest.raises(ValueError) as exc:
        resolve_network("solana-testnet")
    msg = str(exc.value)
    assert "solana-testnet" in msg
    assert "solana-devnet" in msg and "solana-mainnet" in msg


def test_empty_network_value_defaults_to_devnet() -> None:
    """`X402_NETWORK` unset is the dev/CI default — must NOT crash."""
    assert resolve_network("").name == "solana-devnet"
    assert resolve_network(None).name == "solana-devnet"


def test_registry_keys_match_friendly_names() -> None:
    """Catch refactors that drop a network without updating the alias."""
    assert set(NETWORKS.keys()) == {"solana-devnet", "solana-mainnet"}


# ---------------------------------------------------------------------------
# CDP credential / sentinel handling
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    ["", "__unset__", "__dev_change_me__", None],
)
def test_is_unconfigured_detects_sentinels(value: str | None) -> None:
    assert is_unconfigured(value) is True


def test_is_unconfigured_accepts_real_value() -> None:
    assert is_unconfigured("organizations/abc/apiKeys/xyz") is False


def test_cdp_credentials_missing_raises_with_both_var_names() -> None:
    with pytest.raises(ValueError) as exc:
        CDPCredentials.from_env_values("__unset__", "__unset__")
    msg = str(exc.value)
    assert "CDP_API_KEY_ID" in msg
    assert "CDP_API_KEY_SECRET" in msg
    assert "solana-mainnet" in msg


def test_cdp_credentials_partial_missing_lists_only_missing() -> None:
    with pytest.raises(ValueError) as exc:
        CDPCredentials.from_env_values("real-key-id", "__unset__")
    msg = str(exc.value)
    assert "CDP_API_KEY_SECRET" in msg
    # Don't claim the present one is missing — operator-fix-list discipline.
    assert "CDP_API_KEY_ID" not in msg


# ---------------------------------------------------------------------------
# CDP JWT signing — round-trip
# ---------------------------------------------------------------------------


def test_sign_jwt_produces_decodable_token_with_expected_claims() -> None:
    import jwt as pyjwt

    creds = CDPCredentials(key_id="my-key-id", key_secret=_ED25519_PRIV_PEM)
    token = _sign_jwt(
        creds=creds,
        method="POST",
        url="https://api.cdp.coinbase.com/platform/v2/x402/verify",
        now=1_700_000_000.0,
    )

    decoded = pyjwt.decode(
        token,
        _ED25519_PUB_PEM,
        algorithms=["EdDSA"],
        options={"verify_exp": False, "verify_nbf": False},
    )
    assert decoded["sub"] == "my-key-id"
    assert decoded["iss"] == "cdp"
    assert decoded["nbf"] == 1_700_000_000
    assert decoded["exp"] == 1_700_000_000 + 120
    # Canonical URI: "POST host/path", no query, no scheme.
    assert decoded["uri"] == "POST api.cdp.coinbase.com/platform/v2/x402/verify"

    headers = pyjwt.get_unverified_header(token)
    assert headers["kid"] == "my-key-id"
    assert "nonce" in headers and len(headers["nonce"]) >= 16


def test_cdp_auth_provider_returns_distinct_headers_per_endpoint() -> None:
    creds = CDPCredentials(key_id="kid-1", key_secret=_ED25519_PRIV_PEM)
    provider = CDPAuthProvider(
        creds,
        base_url="https://api.cdp.coinbase.com/platform/v2/x402",
        clock=lambda: 1_700_000_000.0,
    )
    headers = provider.get_auth_headers()
    # Each endpoint gets its own JWT (different `uri` claim → different sig).
    assert headers.verify["Authorization"].startswith("Bearer ")
    assert headers.settle["Authorization"].startswith("Bearer ")
    assert headers.supported["Authorization"].startswith("Bearer ")
    assert headers.verify["Authorization"] != headers.settle["Authorization"]
    assert headers.verify["Authorization"] != headers.supported["Authorization"]


# ---------------------------------------------------------------------------
# gecko-api Settings + facilitator wiring
# ---------------------------------------------------------------------------


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear all x402-related env vars so each test starts from a known base."""
    for k in (
        "X402_MODE",
        "X402_NETWORK",
        "X402_FACILITATOR_URL",
        "GECKO_WALLET_ADDRESS",
        "CDP_API_KEY_ID",
        "CDP_API_KEY_SECRET",
        "RESEARCH_BASIC_PRICE",
        "RESEARCH_PRO_PRICE",
        "EVENTS_SECRET",
    ):
        monkeypatch.delenv(k, raising=False)


def test_settings_devnet_default_unchanged(clean_env: None) -> None:
    """Acceptance: devnet path has zero behavioural change from pre-S2-03."""
    _purge_settings_cache()
    os.environ["X402_MODE"] = "stub"
    from gecko_api.settings import Settings

    s = Settings.from_env()
    assert s.x402_network == "solana-devnet"
    assert s.network_config is not None
    assert s.network_config.chain_id == _DEVNET_CHAIN_ID
    assert s.x402_facilitator_url == _DEVNET_FACILITATOR


def test_settings_mainnet_with_real_creds_resolves(clean_env: None) -> None:
    _purge_settings_cache()
    os.environ.update(
        {
            "X402_MODE": "live",
            "X402_NETWORK": "solana-mainnet",
            "GECKO_WALLET_ADDRESS": "FakeMainnetWalletPubkey1111111111111111111",
            "CDP_API_KEY_ID": "organizations/abc/apiKeys/xyz",
            "CDP_API_KEY_SECRET": _ED25519_PRIV_PEM,
        }
    )
    from gecko_api.settings import Settings

    s = Settings.from_env()
    assert s.x402_network == "solana-mainnet"
    assert s.network_config is not None
    assert s.network_config.chain_id == _MAINNET_CHAIN_ID
    assert s.x402_facilitator_url == _MAINNET_FACILITATOR
    assert s.cdp_api_key_id == "organizations/abc/apiKeys/xyz"


def test_settings_mainnet_sentinel_creds_raises(clean_env: None) -> None:
    _purge_settings_cache()
    os.environ.update(
        {
            "X402_MODE": "live",
            "X402_NETWORK": "solana-mainnet",
            "GECKO_WALLET_ADDRESS": "FakeMainnetWalletPubkey1111111111111111111",
            "CDP_API_KEY_ID": "__unset__",
            "CDP_API_KEY_SECRET": "__unset__",
        }
    )
    from gecko_api.settings import Settings

    with pytest.raises(RuntimeError) as exc:
        Settings.from_env()
    msg = str(exc.value)
    assert "CDP_API_KEY_ID" in msg
    assert "CDP_API_KEY_SECRET" in msg
    assert "solana-mainnet" in msg


def test_settings_invalid_network_raises(clean_env: None) -> None:
    _purge_settings_cache()
    os.environ["X402_MODE"] = "stub"
    os.environ["X402_NETWORK"] = "ethereum-sepolia"
    from gecko_api.settings import Settings

    with pytest.raises(ValueError) as exc:
        Settings.from_env()
    assert "ethereum-sepolia" in str(exc.value)


def test_settings_legacy_caip2_still_works(clean_env: None) -> None:
    _purge_settings_cache()
    os.environ["X402_MODE"] = "stub"
    os.environ["X402_NETWORK"] = _DEVNET_CHAIN_ID
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        from gecko_api.settings import Settings

        s = Settings.from_env()
    # Resolved to friendly name regardless of input form.
    assert s.x402_network == "solana-devnet"


# ---------------------------------------------------------------------------
# _build_facilitator dispatch
# ---------------------------------------------------------------------------


def test_build_facilitator_devnet_uses_existing_http_client(clean_env: None) -> None:
    _purge_settings_cache()
    os.environ.update(
        {
            "X402_MODE": "live",
            "X402_NETWORK": "solana-devnet",
            "GECKO_WALLET_ADDRESS": "FakeDevnetWalletPubkey1111111111111111111",
        }
    )
    from gecko_api.main import _build_facilitator
    from gecko_api.settings import Settings
    from x402.http.facilitator_client import HTTPFacilitatorClient

    s = Settings.from_env()
    client = _build_facilitator(s)
    # Devnet path: vanilla HTTPFacilitatorClient (no auth provider).
    assert isinstance(client, HTTPFacilitatorClient)
    # Devnet facilitator should NOT carry a CDP-style auth provider.
    assert getattr(client, "_auth_provider", None) is None


def test_build_facilitator_mainnet_uses_cdp_auth(clean_env: None) -> None:
    _purge_settings_cache()
    os.environ.update(
        {
            "X402_MODE": "live",
            "X402_NETWORK": "solana-mainnet",
            "GECKO_WALLET_ADDRESS": "FakeMainnetWalletPubkey1111111111111111111",
            "CDP_API_KEY_ID": "organizations/abc/apiKeys/xyz",
            "CDP_API_KEY_SECRET": _ED25519_PRIV_PEM,
        }
    )
    from gecko_api.main import _build_facilitator
    from gecko_api.settings import Settings
    from x402.http.facilitator_client import HTTPFacilitatorClient

    s = Settings.from_env()
    client = _build_facilitator(s)
    # Mainnet path: HTTPFacilitatorClient wrapping CDP auth provider.
    assert isinstance(client, HTTPFacilitatorClient)
    auth = getattr(client, "_auth_provider", None)
    assert auth is not None, "mainnet facilitator must carry a CDP AuthProvider"
    assert isinstance(auth, CDPAuthProvider)
    # Sanity: the auth provider can mint headers without raising.
    headers = auth.get_auth_headers()
    assert headers.verify["Authorization"].startswith("Bearer ")


def test_build_facilitator_stub_unchanged(clean_env: None) -> None:
    """Acceptance: stub mode still returns the StubFacilitatorClient."""
    _purge_settings_cache()
    os.environ.update({"X402_MODE": "stub"})
    from gecko_api.main import _build_facilitator
    from gecko_api.settings import Settings
    from gecko_api.x402_stub import StubFacilitatorClient

    s = Settings.from_env()
    client = _build_facilitator(s)
    assert isinstance(client, StubFacilitatorClient)
