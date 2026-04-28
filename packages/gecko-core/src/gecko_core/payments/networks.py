"""x402 network registry — devnet ↔ mainnet selection.

Single source of truth for the (CAIP-2 chain id, Solana cluster, facilitator
URL) tuple keyed by a friendly network name. The deploy switches networks by
flipping `X402_NETWORK` in SSM and forcing a new ECS deployment — no code
changes between devnet and mainnet operation.

Friendly names ("solana-devnet", "solana-mainnet") are what we expose in env,
SSM, the x402 RouteConfig, and 402 challenges. The CAIP-2 form is derived,
not stored independently — that prevents drift between "the chain id we
advertise" and "the cluster the facilitator settles on".

Legacy compat: earlier deploys set `X402_NETWORK` to a raw CAIP-2 string
(e.g. `solana:EtW...`). `resolve_network()` accepts that form and translates
back to the friendly key with a deprecation warning.
"""

from __future__ import annotations

import logging
import warnings
from dataclasses import dataclass
from typing import Final, Literal

logger = logging.getLogger(__name__)

NetworkName = Literal["solana-devnet", "solana-mainnet"]


@dataclass(frozen=True)
class NetworkConfig:
    """Everything boot code needs to know about a Solana network."""

    name: NetworkName
    chain_id: str  # CAIP-2 form, e.g. "solana:EtW..."
    facilitator_url: str
    cluster: str  # solana CLI cluster name: "devnet" | "mainnet-beta"


# Genesis-hash-based CAIP-2 chain ids for the two Solana clusters we run on.
# These are the canonical strings the x402 spec expects and what the
# facilitator validates against — DO NOT shorten or rewrite them.
_DEVNET_CHAIN_ID: Final = "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1"
_MAINNET_CHAIN_ID: Final = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"

# Devnet uses the public Coinbase-operated x402 facilitator (free, no auth).
# Mainnet uses CDP — Coinbase Developer Platform, behind API key + JWT.
_DEVNET_FACILITATOR_URL: Final = "https://www.x402.org/facilitator"
_MAINNET_FACILITATOR_URL: Final = "https://api.cdp.coinbase.com/platform/v2/x402"


NETWORKS: Final[dict[NetworkName, NetworkConfig]] = {
    "solana-devnet": NetworkConfig(
        name="solana-devnet",
        chain_id=_DEVNET_CHAIN_ID,
        facilitator_url=_DEVNET_FACILITATOR_URL,
        cluster="devnet",
    ),
    "solana-mainnet": NetworkConfig(
        name="solana-mainnet",
        chain_id=_MAINNET_CHAIN_ID,
        facilitator_url=_MAINNET_FACILITATOR_URL,
        cluster="mainnet-beta",
    ),
}

# Legacy CAIP-2 → friendly-name map. Old SSM values used the chain id as the
# X402_NETWORK value directly. We accept that form, translate, and warn.
_LEGACY_CAIP2_TO_NAME: Final[dict[str, NetworkName]] = {
    _DEVNET_CHAIN_ID: "solana-devnet",
    _MAINNET_CHAIN_ID: "solana-mainnet",
}


def resolve_network(value: str | None) -> NetworkConfig:
    """Translate an `X402_NETWORK` env value into a `NetworkConfig`.

    Accepted forms:
      * Friendly name: "solana-devnet", "solana-mainnet" — preferred.
      * Legacy CAIP-2: "solana:EtW...", "solana:5eyk..." — deprecated; logs +
        warns. Kept so existing SSM params keep working through one deploy.

    Anything else raises ValueError at startup. We refuse to silently default
    to devnet when an unknown value is set — better to crash on boot than to
    settle real money on the wrong cluster.
    """
    if value is None or value == "":
        # Empty / unset → devnet, the safe default. Matches existing
        # Settings.from_env() behaviour.
        return NETWORKS["solana-devnet"]

    if value in NETWORKS:
        return NETWORKS[value]

    legacy = _LEGACY_CAIP2_TO_NAME.get(value)
    if legacy is not None:
        msg = (
            f"X402_NETWORK={value!r} uses the legacy CAIP-2 form; "
            f"set it to {legacy!r} on the next deploy."
        )
        logger.warning(msg)
        warnings.warn(msg, DeprecationWarning, stacklevel=2)
        return NETWORKS[legacy]

    valid = ", ".join(sorted(NETWORKS.keys()))
    raise ValueError(
        f"unknown X402_NETWORK={value!r}; expected one of: {valid} (or a legacy CAIP-2 chain id)"
    )


__all__ = [
    "NETWORKS",
    "NetworkConfig",
    "NetworkName",
    "resolve_network",
]
