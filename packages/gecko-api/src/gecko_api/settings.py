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
from gecko_core.wallets.privy import is_privy_configured as _privy_configured
from pydantic import BaseModel, SecretStr

_TWITSH_SENTINELS = ("", "__unset__", "__dev_change_me__")

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

    # Privy v2 server-side wallet credentials (S2-05). Both required for
    # per-project wallet provisioning; absent values just skip provisioning
    # silently (lazy-create on first paid call still works once configured).
    privy_app_id: str | None = None
    privy_app_secret: str | None = None

    # twit.sh — X/Twitter data via x402 micropayments on Base mainnet (S2X-09).
    # Server-managed Gecko-owned EVM wallet, funded with USDC on Base. Disabled
    # by default — flip TWITSH_ENABLED=true after the wallet is funded AND the
    # S2X-08 client lands. Sentinel values keep ECS booting cleanly before
    # onboarding completes; `is_twitsh_configured()` gates real network calls.
    twitsh_enabled: bool = False
    twitsh_wallet_private_key: SecretStr | None = None
    twitsh_wallet_address: str | None = None
    twitsh_base_url: str = "https://x402.twit.sh"

    # Pricing — basic and pro tiers exposed as separate routes.
    research_basic_price: str = "$20.00"
    research_pro_price: str = "$0.75"
    # S4-ROUTE-02: flat per-call charge for /route. The OpenRouter `usage.cost`
    # truth is only known post-call; x402's payment model is pre-flight, so we
    # charge a fixed amount that covers the expected average + margin instead
    # of doing exact markup math. Tradeoff: heavy callers subsidize light ones,
    # but the implementation stays simple and predictable.
    route_call_price: str = "$0.02"
    # S5-API-01: Advisor Panel call price. Mirrors `route_call_price` —
    # flat per-call charge advertised on `POST /plan`. Default $0.25 lands
    # below the $0.75 Pro debate price; the panel is cheaper because it
    # reuses the existing session's transcript instead of running a fresh
    # 5-agent debate.
    plan_call_price: str = "$0.25"
    # S7-DOGFOOD-02: sprint review meta-tool. Cheap (one LLM call vs. 5 for
    # /plan), so charge accordingly. Free in stub mode (route is registered
    # in live mode only — see _build_routes).
    review_call_price: str = "$0.10"
    # S8-API-01: HTTP surfaces for /scaffold and /pulse, mirroring the MCP
    # tools. /scaffold matches the MCP tool's $0.05 charge; /pulse is free
    # in stub mode (deferred pricing — operator can flip via PULSE_CALL_PRICE).
    scaffold_call_price: str = "$0.05"
    pulse_call_price: str = "$0.00"

    # S5-API-03: tiered /route pricing. Sprint 4 shipped a single flat
    # $0.02 charge; Sprint 5 splits it into three paths so heavy callers
    # subsidize light ones less. The middleware can't refund post-call,
    # so we register one route per tier and the client picks the path
    # matching its `task_hint` + `prefer_premium`.
    route_price_default: str = "$0.01"
    route_price_premium: str = "$0.05"
    route_price_upgrade: str = "$0.20"

    # Per-instance secret used to HMAC-sign session-scoped events tokens for
    # the SSE endpoint. Defaults to a derived value at process start so devs
    # don't need to set anything; production deploys MUST set EVENTS_SECRET so
    # tokens survive restarts and load-balanced replicas. 32+ bytes recommended.
    events_secret: str = "dev-events-secret-not-for-production"

    model_config = {"frozen": True, "arbitrary_types_allowed": True}

    def is_privy_configured(self) -> bool:
        """True iff PRIVY_APP_ID + PRIVY_APP_SECRET are set + non-sentinel.

        Delegates to `gecko_core.wallets.privy.is_privy_configured` so the
        sentinel list stays in one place.
        """
        return _privy_configured(
            app_id=self.privy_app_id,
            app_secret=self.privy_app_secret,
        )

    def is_twitsh_configured(self) -> bool:
        """True iff twit.sh is enabled AND the wallet is real (non-sentinel).

        The kill-switch (`TWITSH_ENABLED=false`) wins regardless of wallet
        state — flipping enabled to true with sentinel creds still returns
        False so misconfigured deploys can't burn calls. Both the private key
        and the address must be non-empty and not match a known sentinel.
        """
        if not self.twitsh_enabled:
            return False
        pk = (
            self.twitsh_wallet_private_key.get_secret_value()
            if self.twitsh_wallet_private_key is not None
            else ""
        )
        if pk in _TWITSH_SENTINELS:
            return False
        addr = self.twitsh_wallet_address or ""
        return addr not in _TWITSH_SENTINELS

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

        # Privy creds — pulled regardless of network. When sentinel-detected,
        # is_privy_configured() returns False and the api lazy-skips wallet
        # provisioning. We do NOT hard-fail at boot; devnet without a Privy
        # account still serves users without project_id.
        privy_app_id = os.environ.get("PRIVY_APP_ID")
        privy_app_secret = os.environ.get("PRIVY_APP_SECRET")

        # twit.sh wallet config — read raw, no boot-time hard fail. The
        # `is_twitsh_configured()` helper gates real usage downstream.
        twitsh_enabled_raw = os.environ.get("TWITSH_ENABLED", "false").strip().lower()
        twitsh_enabled = twitsh_enabled_raw in ("1", "true", "yes", "on")
        twitsh_pk_raw = os.environ.get("TWITSH_WALLET_PRIVATE_KEY")
        twitsh_pk = SecretStr(twitsh_pk_raw) if twitsh_pk_raw else None
        twitsh_addr = os.environ.get("TWITSH_WALLET_ADDRESS")
        twitsh_base_url = os.environ.get("TWITSH_BASE_URL", "https://x402.twit.sh")

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
        route_price = os.environ.get("ROUTE_CALL_PRICE", "$0.02")
        # S5-API-01: $0.25 per advisor panel run. Mirrors ROUTE_CALL_PRICE env shape.
        plan_price = os.environ.get("PLAN_CALL_PRICE", "$0.25")
        review_price = os.environ.get("REVIEW_CALL_PRICE", "$0.10")
        scaffold_price = os.environ.get("SCAFFOLD_CALL_PRICE", "$0.05")
        pulse_price = os.environ.get("PULSE_CALL_PRICE", "$0.00")
        # S5-API-03: tiered /route pricing. ROUTE_CALL_PRICE stays as the
        # legacy single-tier knob (back-compat); the three new vars below
        # let operators tune each tier independently.
        route_price_default = os.environ.get("ROUTE_PRICE_TIER_DEFAULT", "$0.01")
        route_price_premium = os.environ.get("ROUTE_PRICE_TIER_PREMIUM", "$0.05")
        route_price_upgrade = os.environ.get("ROUTE_PRICE_TIER_UPGRADE", "$0.20")
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
            privy_app_id=privy_app_id,
            privy_app_secret=privy_app_secret,
            twitsh_enabled=twitsh_enabled,
            twitsh_wallet_private_key=twitsh_pk,
            twitsh_wallet_address=twitsh_addr,
            twitsh_base_url=twitsh_base_url,
            research_basic_price=basic_price,
            research_pro_price=pro_price,
            route_call_price=route_price,
            plan_call_price=plan_price,
            review_call_price=review_price,
            scaffold_call_price=scaffold_price,
            pulse_call_price=pulse_price,
            route_price_default=route_price_default,
            route_price_premium=route_price_premium,
            route_price_upgrade=route_price_upgrade,
            events_secret=events_secret,
        )


__all__ = ["Settings", "X402Mode"]
