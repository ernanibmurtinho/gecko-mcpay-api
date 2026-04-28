"""Typed settings for gecko-api.

Reads x402 + wallet configuration from the environment. Defaults to
``X402_MODE=stub`` so local dev and CI never accidentally talk to a real
facilitator. Live mode (`mode != "stub"`) requires the facilitator URL and
the receiving wallet address — we fail fast at startup rather than at the
first 402.

Network selection (S2-03): ``X402_NETWORK`` is resolved through
``gecko_core.payments.networks`` so devnet ↔ mainnet is a single SSM-flip
plus ``aws ecs update-service --force-new-deployment``. The resolver also
selects the facilitator URL — mainnet defaults to CDP at
``https://api.cdp.coinbase.com/platform/v2/x402`` unless the operator
explicitly overrides ``X402_FACILITATOR_URL``.
"""

from __future__ import annotations

import os
from typing import Literal

from gecko_core.payments.cdp import is_unconfigured
from gecko_core.payments.networks import NetworkConfig, resolve_network
from pydantic import BaseModel

X402Mode = Literal["stub", "live", "frames"]


class Settings(BaseModel):
    """Runtime settings derived from the process environment."""

    x402_mode: X402Mode = "stub"
    x402_facilitator_url: str | None = None
    gecko_wallet_address: str | None = None
    # Friendly network name; the CAIP-2 chain id is on `network_config`.
    x402_network: str = "solana-devnet"
    # Resolved network config (chain_id, facilitator URL default, cluster).
    # Optional only because BaseModel default-construction can happen in
    # tests; `from_env()` always sets it.
    network_config: NetworkConfig | None = None

    # CDP credentials — required when x402_network == "solana-mainnet".
    # Stored as plain strings inside Settings (which is `frozen=True`) and
    # never logged. SSM sentinels (`__unset__`, `__dev_change_me__`) are
    # detected by `gecko_core.payments.cdp.is_unconfigured` so they don't
    # silently flow into a live request.
    cdp_api_key_id: str | None = None
    cdp_api_key_secret: str | None = None

    # Pricing — basic and pro tiers exposed as separate routes.
    research_basic_price: str = "$20.00"
    research_pro_price: str = "$0.75"

    # Per-instance secret used to HMAC-sign session-scoped events tokens for
    # the SSE endpoint. Defaults to a derived value at process start so devs
    # don't need to set anything; production deploys MUST set EVENTS_SECRET so
    # tokens survive restarts and load-balanced replicas. 32+ bytes recommended.
    events_secret: str = "dev-events-secret-not-for-production"

    model_config = {"frozen": True, "arbitrary_types_allowed": True}

    @classmethod
    def from_env(cls) -> Settings:
        mode = os.environ.get("X402_MODE", "stub")
        if mode not in ("stub", "live", "frames"):
            raise ValueError(f"unknown X402_MODE: {mode!r}")

        # Resolve network FIRST — `resolve_network` raises on unknown values
        # so a typo in SSM crashes startup instead of running on the wrong
        # cluster. It also accepts the legacy CAIP-2 form for one-deploy
        # backwards compat.
        network_value = os.environ.get("X402_NETWORK")
        net = resolve_network(network_value)

        # Operator can override the per-network default facilitator URL —
        # useful for staging or when CDP is behind a private gateway. When
        # unset, fall back to whatever the network registry advertises.
        facilitator_url = os.environ.get("X402_FACILITATOR_URL") or net.facilitator_url
        wallet = os.environ.get("GECKO_WALLET_ADDRESS")

        # CDP creds: read raw, defer the sentinel check until we know we're
        # actually on mainnet. Pulling the env values regardless lets us
        # surface them in /healthz when present without exposing secrets.
        cdp_key_id = os.environ.get("CDP_API_KEY_ID")
        cdp_key_secret = os.environ.get("CDP_API_KEY_SECRET")

        if mode != "stub":
            missing: list[str] = []
            if not facilitator_url:
                missing.append("X402_FACILITATOR_URL")
            if not wallet:
                missing.append("GECKO_WALLET_ADDRESS")
            if missing:
                raise RuntimeError(f"X402_MODE={mode!r} requires env vars: {', '.join(missing)}")

            # Mainnet path: CDP creds must be present AND non-sentinel.
            # Refuse to boot otherwise — a sentinel reaching the JWT signer
            # produces a confusing 401 on every paid request instead of a
            # clean startup error.
            if net.name == "solana-mainnet":
                cdp_missing: list[str] = []
                if is_unconfigured(cdp_key_id):
                    cdp_missing.append("CDP_API_KEY_ID")
                if is_unconfigured(cdp_key_secret):
                    cdp_missing.append("CDP_API_KEY_SECRET")
                if cdp_missing:
                    raise RuntimeError(
                        "X402_NETWORK=solana-mainnet requires "
                        f"{' and '.join(cdp_missing)} to be set "
                        "(SSM sentinel or empty value detected)."
                    )

        # Pricing overrides — useful on devnet where we want cheap demo runs.
        # Format: "$N.NN" (the x402 SDK parses the leading $).
        basic_price = os.environ.get("RESEARCH_BASIC_PRICE", "$20.00")
        pro_price = os.environ.get("RESEARCH_PRO_PRICE", "$0.75")
        events_secret = os.environ.get("EVENTS_SECRET", "dev-events-secret-not-for-production")

        # In stub mode we still need a non-empty pay_to for the route catalog.
        # Use a clearly-fake placeholder so it's obvious in /.well-known/x402.
        return cls(
            x402_mode=mode,  # type: ignore[arg-type]
            x402_facilitator_url=facilitator_url,
            gecko_wallet_address=wallet or "STUB_WALLET_ADDRESS_NOT_FOR_LIVE",
            x402_network=net.name,
            network_config=net,
            cdp_api_key_id=cdp_key_id,
            cdp_api_key_secret=cdp_key_secret,
            research_basic_price=basic_price,
            research_pro_price=pro_price,
            events_secret=events_secret,
        )


__all__ = ["Settings", "X402Mode"]
