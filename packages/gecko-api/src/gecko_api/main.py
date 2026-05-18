"""FastAPI service wrapping gecko-core for the V2 web app at app.geckovision.tech.

Routes (V2 — agent-native):
    POST /research              — gated by x402, $20 (basic tier)
    POST /research/pro          — gated by x402, $75 (pro tier; 501 NotImplemented)
    POST /sessions/{id}/ask     — FREE follow-up question
    GET  /sessions/{id}/sources — FREE list indexed sources
    GET  /healthz               — FREE liveness + payment mode
    GET  /.well-known/x402      — FREE route catalog per x402 convention

Middleware order matters: CORS first, then x402. Browser callers can't read
402 response headers if x402 wraps CORS.

This is a thin transport layer — all business logic lives in `gecko_core`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

import gecko_core
import uvicorn
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from gecko_core.models import AskResult, ResearchResult, SourceInfo, Tier
from gecko_core.payments.constants import STUB_WALLET_ADDRESS_NOT_FOR_LIVE
from gecko_core.payments.factory import resolve_facilitator_client
from gecko_core.sessions.store import SessionStore

# S22-MCP-HOST-03 — FastMCP instance owned by gecko-mcp. We mount its
# Streamable HTTP ASGI app at /mcp below and run its session manager
# inside the existing lifespan. Same x402 PaymentMiddlewareASGI wraps it
# automatically (see app.add_middleware(PaymentMiddlewareASGI, ...)).
from gecko_mcp.server import mcp as _gecko_mcp
from pydantic import BaseModel, Field, field_validator
from slowapi.errors import RateLimitExceeded
from slowapi.util import (
    get_remote_address,  # noqa: F401  (legacy import retained for downstream imports)
)
from starlette.responses import JSONResponse, Response, StreamingResponse
from x402 import x402ResourceServer
from x402.http.facilitator_client_base import FacilitatorClient
from x402.http.middleware.fastapi import PaymentMiddlewareASGI
from x402.http.types import PaymentOption, RouteConfig
from x402.mechanisms.evm.exact.server import ExactEvmScheme
from x402.mechanisms.svm.exact import ExactSvmServerScheme

from gecko_api.auth import verify_frames_token
from gecko_api.bazaar import (
    BAZAAR_EXTENSIONS,
    extension_as_dict,
)
from gecko_api.circuit_breaker import (
    RETRY_AFTER_SECONDS as _CIRCUIT_RETRY_AFTER_SECONDS,
)
from gecko_api.circuit_breaker import (
    get_breaker as _get_cost_breaker,
)
from gecko_api.events_token import (
    EVENTS_PREFIX,
    RETRY_PREFIX,
    EventsTokenError,
    issue_retry_token,
    issue_token,
    verify_token,
)
from gecko_api.internal_routes import router as internal_router
from gecko_api.settings import Settings
from gecko_api.wallets import (
    check_project_budget,
    ensure_project_wallet,
    increment_project_spent_safe,
)
from gecko_api.x402_stub import StubFacilitatorClient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# x402 wiring — built once at import time so the same RouteConfig dict is
# shared with /.well-known/x402.
# ---------------------------------------------------------------------------


def _build_facilitator(settings: Settings) -> FacilitatorClient:
    """Pick the right facilitator client for the configured mode + network.

    Sprint 14 S14-PAY-MIGRATE-01: dispatch is delegated to
    :func:`gecko_core.payments.factory.resolve_facilitator_client` so a
    new facilitator (Cloudflare in S15) is one entry in core, not a
    duplicated branch here. Stub stays in gecko-api because
    ``StubFacilitatorClient`` is transport-shaped (no on-chain settle,
    just the in-process middleware contract). Everything else — Solana
    mainnet (CDP), Base mainnet (CDP), devnet/public — flows through
    the seam.

    Routes use CAIP-2 chain ids; the stub facilitator must advertise the
    same id the routes register under or the x402 lib raises
    ``RouteConfigurationError`` on first request.
    """
    if settings.x402_mode == "stub":
        stub_network = (
            settings.network_config.chain_id if settings.network_config else settings.x402_network
        )
        return StubFacilitatorClient(network=stub_network)

    # Live / frames / cdp all go through the core factory — same dispatch
    # table for the X402Client and FacilitatorClient surfaces, no inline
    # ``if x402_network == ...`` left in the transport layer.
    assert settings.x402_facilitator_url is not None  # checked in Settings.from_env
    return resolve_facilitator_client(  # type: ignore[return-value]
        mode=settings.x402_mode,
        network_id=settings.x402_network,
        facilitator_url=settings.x402_facilitator_url,
        cdp_api_key_id=settings.cdp_api_key_id,
        cdp_api_key_secret=settings.cdp_api_key_secret,
    )


# S14-CDP-HARDEN-02 — payTo address-format check, fired at app startup.
#
# Sprint 12 lesson #4: a Solana base58 address was advertised as `payTo`
# on Base/EVM routes (cae61ed). CDP parsed it as raw bytes against the
# EVM ABI and failed `/settle` with a misleading "transfer amount
# exceeds balance". The reactive fix lived for one sprint as a
# unit-test guard (S12.5-TEST-03); promote it to a startup-time
# assertion so a misconfigured deploy refuses to boot rather than
# silently advertising a broken catalog.

_EVM_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_SOLANA_ADDRESS_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
_PAYTO_STUB_SENTINEL = STUB_WALLET_ADDRESS_NOT_FOR_LIVE


class ConfigurationError(RuntimeError):
    """Raised at startup when a route's payTo doesn't match its network family.

    The misconfigured route would either reject every settle (CDP path)
    or, worse, advertise an address that looks valid to a relaxed buyer
    and lands funds in the wrong wallet. Fail at boot, not on the first
    paid request.
    """


def _assert_payto_format(network: str, pay_to: str, *, route: str) -> None:
    """Assert ``pay_to`` is well-formed for ``network``.

    Stub-mode sentinel (``STUB_WALLET_ADDRESS_NOT_FOR_LIVE``) is allowed
    so dev / CI stays green when neither wallet is configured. In any
    live mode the surrounding settings layer rejects the sentinel up
    front; this layer is purely about *cross-network* mismatches.
    """
    if pay_to == _PAYTO_STUB_SENTINEL:
        return
    if network.startswith("eip155:"):
        if not _EVM_ADDRESS_RE.match(pay_to):
            raise ConfigurationError(
                f"route {route!r} advertises network={network!r} but payTo={pay_to!r} "
                "is not a valid EIP-55 EVM address (0x + 40 hex). "
                "This is the cae61ed regression class — Solana base58 on a "
                "Base/EVM route. Set GECKO_WALLET_ADDRESS_BASE."
            )
    elif network.startswith("solana"):
        if pay_to.startswith("0x"):
            raise ConfigurationError(
                f"route {route!r} advertises network={network!r} but payTo={pay_to!r} "
                "looks like an EVM address. Solana routes need a base58 address."
            )
        if not _SOLANA_ADDRESS_RE.match(pay_to):
            raise ConfigurationError(
                f"route {route!r} advertises network={network!r} but payTo={pay_to!r} "
                "is not a valid base58 (32-44 chars, no 0/O/I/l)."
            )
    else:
        # Unknown network family — refuse to advertise rather than hope.
        raise ConfigurationError(
            f"route {route!r} advertises unknown network family {network!r}; "
            "only eip155:* and solana* are supported by the address-format check."
        )


def _assert_routes_payto_well_formed(routes: dict[str, RouteConfig]) -> None:
    """Walk every advertised payment option; raise ``ConfigurationError`` on
    the first mismatch. Called once at module import after ``_build_routes``.

    Closes Sprint 12 lesson #4 by promoting the unit-test-only check to
    a startup-time fail-fast.
    """
    for pattern, route in routes.items():
        accepts = route.accepts if isinstance(route.accepts, list) else [route.accepts]
        for opt in accepts:
            # ``PaymentOption.pay_to`` is typed ``str | Callable`` in the
            # x402 lib (the dynamic-resolver shape). We never use the
            # callable form — every ``_build_routes`` site assigns a plain
            # string — so narrow defensively. A callable here would be a
            # forward incompatibility worth surfacing with a clear error.
            pay_to = opt.pay_to
            if not isinstance(pay_to, str):
                raise ConfigurationError(
                    f"route {pattern!r} payTo is a callable; the "
                    "address-format check requires a static string."
                )
            _assert_payto_format(opt.network, pay_to, route=pattern)
            # SVM routes MUST advertise feePayer in extra — ExactSvmScheme
            # raises client-side without it. EVM routes MUST NOT — leaks the
            # SVM-only field into eip155:* options. Locks the contract so a
            # future refactor that drops svm_extra fails fast at startup.
            network = opt.network
            extra = opt.extra or {}
            if network and network.startswith("eip155:"):
                if "feePayer" in extra:
                    raise ConfigurationError(
                        f"route {pattern!r} eip155 option carries extra.feePayer "
                        f"({extra['feePayer']!r}); feePayer is SVM-only."
                    )
            elif network and network.startswith("solana"):
                fee_payer = extra.get("feePayer")
                if not fee_payer:
                    raise ConfigurationError(
                        f"route {pattern!r} solana option missing extra.feePayer; "
                        "ExactSvmScheme requires it client-side."
                    )
                if fee_payer != pay_to:
                    raise ConfigurationError(
                        f"route {pattern!r} feePayer={fee_payer!r} != payTo={pay_to!r}; "
                        "operator wallet must serve as both for the stub flow."
                    )


def _build_routes(settings: Settings) -> dict[str, RouteConfig]:
    # The x402 lib's RouteConfig.PaymentOption.network expects the CAIP-2
    # chain id (e.g. "solana:EtW..."), NOT the friendly name. The lib runs a
    # facilitator-support check at registration time; passing "solana-devnet"
    # raises RouteConfigurationError("Facilitator doesn't support \"exact\"
    # on \"solana-devnet\"") on the first request. The friendly name lives
    # only in our env / settings / runbook for human ergonomics.
    chain_id = (
        settings.network_config.chain_id if settings.network_config else settings.x402_network
    )
    # Sprint 12 Track A: Base/EVM routes need a 0x payTo, not the Solana
    # gecko_wallet_address. Pick per network. Without this, eip155:* routes
    # advertise a Solana base58 address as `payTo` and CDP's USDC settle
    # call fails with a cryptic "transfer amount exceeds balance" once the
    # address gets parsed as raw bytes against the EVM ABI.
    if chain_id and chain_id.startswith("eip155:"):
        pay_to = (
            settings.gecko_wallet_address_base
            or settings.gecko_wallet_address
            or STUB_WALLET_ADDRESS_NOT_FOR_LIVE
        )
        # S14-CDP-HARDEN-03 — EIP-55 checksum-encode the eip155:* payTo
        # before it appears in any PaymentRequirements / /.well-known
        # listing. Mixed-case Solana base58 untouched (gated on chain_id).
        # The output is byte-stable across deploys regardless of how the
        # operator wrote the env value.
        from gecko_core.payments.cdp_x402_client import to_evm_checksum_address

        pay_to = to_evm_checksum_address(pay_to)
    else:
        pay_to = settings.gecko_wallet_address or STUB_WALLET_ADDRESS_NOT_FOR_LIVE
    # SVM (Solana) routes require feePayer in extra — the facilitator's Solana
    # address that sponsors transaction fees. For Gecko, the same wallet
    # receives USDC and covers fees. EVM routes don't use this field.
    svm_extra: dict[str, str] | None = (
        {"feePayer": pay_to} if not (chain_id and chain_id.startswith("eip155:")) else None
    )
    routes: dict[str, RouteConfig] = {
        "POST /research": RouteConfig(
            accepts=[
                PaymentOption(
                    scheme="exact",
                    pay_to=pay_to,
                    price=settings.research_basic_price,
                    network=chain_id,
                    extra=svm_extra,
                ),
            ],
            description="Run a Builder Bootstrap research session (basic tier)",
        ),
        "POST /research/pro": RouteConfig(
            accepts=[
                PaymentOption(
                    scheme="exact",
                    pay_to=pay_to,
                    price=settings.research_pro_price,
                    network=chain_id,
                    extra=svm_extra,
                ),
            ],
            description="Run a Builder Bootstrap research session (pro tier)",
        ),
        # S4-ROUTE-02 / S5-API-03: cost-aware LLM routing surface.
        # Sprint 4 shipped a single flat $0.02 charge; Sprint 5 splits it
        # into three paths so the price scales loosely with the routed
        # model's expected cost. x402 charges pre-flight (it can't read
        # `task_hint` from the body), so each tier gets its own URL and
        # the client picks the path that matches its hint.
        "POST /route": RouteConfig(
            accepts=[
                PaymentOption(
                    scheme="exact",
                    pay_to=pay_to,
                    price=settings.route_price_default,
                    network=chain_id,
                    extra=svm_extra,
                ),
            ],
            description=(
                "Route an LLM call through Gecko's cost-aware router "
                "(default tier — extraction/summary/default task_hint)"
            ),
        ),
        "POST /route/premium": RouteConfig(
            accepts=[
                PaymentOption(
                    scheme="exact",
                    pay_to=pay_to,
                    price=settings.route_price_premium,
                    network=chain_id,
                    extra=svm_extra,
                ),
            ],
            description=(
                "Route an LLM call through Gecko's cost-aware router "
                "(premium tier — reasoning/code task_hint)"
            ),
        ),
        "POST /route/upgrade": RouteConfig(
            accepts=[
                PaymentOption(
                    scheme="exact",
                    pay_to=pay_to,
                    price=settings.route_price_upgrade,
                    network=chain_id,
                    extra=svm_extra,
                ),
            ],
            description=(
                "Route an LLM call through Gecko's cost-aware router "
                "(upgrade tier — prefer_premium=True)"
            ),
        ),
        # S5-API-01: Advisor Panel paid surface ($0.25 flat). Mirrors the
        # /research and /route patterns — body carries session_id +
        # tier_preset; the response is the AdvisorPanel JSON.
        "POST /plan": RouteConfig(
            accepts=[
                PaymentOption(
                    scheme="exact",
                    pay_to=pay_to,
                    price=settings.plan_call_price,
                    network=chain_id,
                    extra=svm_extra,
                ),
            ],
            description="Run the 5-voice Advisor Panel against an existing session",
        ),
    }
    # S7-DOGFOOD-02: /review is FREE in stub mode and $0.10 in live. We
    # only register the x402 RouteConfig outside stub so the middleware
    # doesn't 402 a stub-mode dogfood loop.
    if settings.x402_mode != "stub":
        routes["POST /review"] = RouteConfig(
            accepts=[
                PaymentOption(
                    scheme="exact",
                    pay_to=pay_to,
                    price=settings.review_call_price,
                    network=chain_id,
                    extra=svm_extra,
                ),
            ],
            description="Synthesize a SprintReview from git log + memory + sprint docs",
        )
        # S8-API-01: /scaffold matches the MCP gecko_scaffold tool ($0.05).
        # Stub mode skips registration so the dogfood loop and CI smoke
        # don't hit a 402 wall.
        routes["POST /scaffold"] = RouteConfig(
            accepts=[
                PaymentOption(
                    scheme="exact",
                    pay_to=pay_to,
                    price=settings.scaffold_call_price,
                    network=chain_id,
                    extra=svm_extra,
                ),
            ],
            description="Generate the 3-file scaffold bundle for a Pro session",
        )
    # S13-COMMO-01..03 — Track E commoditization wedges. Registered in BOTH
    # stub and live modes so Bazaar discovery + the bazaar-extensions tests
    # see them under either deploy. In stub mode the middleware still issues
    # 402 challenges; only the facilitator settle is short-circuited.
    routes["POST /advise"] = RouteConfig(
        accepts=[
            PaymentOption(
                scheme="exact",
                pay_to=pay_to,
                price=settings.advisor_voice_price,
                network=chain_id,
                extra=svm_extra,
            ),
        ],
        description="Invoke a single advisor voice (CEO/CTO/BM/PM/SM) on an existing session",
    )
    # S13-COMMO-02 — paid follow-up against a session's KB (POST /ask, $0.01).
    # The free /sessions/{id}/ask path stays available; this route is what
    # callers hit AFTER the per-session free quota is exhausted.
    routes["POST /ask"] = RouteConfig(
        accepts=[
            PaymentOption(
                scheme="exact",
                pay_to=pay_to,
                price=settings.ask_call_price,
                network=chain_id,
                extra=svm_extra,
            ),
        ],
        description="Ask a grounded follow-up against an existing session's knowledge base",
    )
    # S13-COMMO-03 — classify-as-a-service (POST /classify, $0.10).
    # Returns categories + suggested sources + priority weights for an idea
    # without running the full research pipeline.
    routes["POST /classify"] = RouteConfig(
        accepts=[
            PaymentOption(
                scheme="exact",
                pay_to=pay_to,
                price=settings.classify_call_price,
                network=chain_id,
                extra=svm_extra,
            ),
        ],
        description="Classify an idea into categories with suggested sources and priority weights",
    )
    # Phase 10A — POST /trade_research x402 gate. Was free in Phase 8b
    # (DoS surface: client loop hits Mongo + AG2 7-agent debate per call).
    # Registered in both stub and live so Bazaar discovery sees it.
    routes["POST /trade_research"] = RouteConfig(
        accepts=[
            PaymentOption(
                scheme="exact",
                pay_to=pay_to,
                price=settings.trade_research_basic_price,
                network=chain_id,
                extra=svm_extra,
            ),
        ],
        description="Run the 7-agent trade research panel (basic tier)",
    )
    routes["POST /trade_research/pro"] = RouteConfig(
        accepts=[
            PaymentOption(
                scheme="exact",
                pay_to=pay_to,
                price=settings.trade_research_pro_price,
                network=chain_id,
                extra=svm_extra,
            ),
        ],
        description="Run the 7-agent trade research panel (pro tier)",
    )
    # S23-REPORT-01 — gecko_report HTTP surface. Registered in both stub and
    # live so Bazaar discovery sees it under either deploy. $0.05 flat.
    routes["POST /report/:session_id"] = RouteConfig(
        accepts=[
            PaymentOption(
                scheme="exact",
                pay_to=pay_to,
                price=settings.report_call_price,
                network=chain_id,
                extra=svm_extra,
            ),
        ],
        description=(
            "Generate a formatted report (HTML or markdown) for a completed "
            "research session. Renders verdict, sources, validation, business "
            "plan, PRD, and 5-voice panel into a single shareable document."
        ),
    )
    # S14-PULSE-01 — gecko_pulse v1 SKU at $0.50/call. Registered in BOTH
    # stub and live modes so Bazaar discovery sees it under either deploy.
    # In stub mode the middleware still issues 402 challenges; only the
    # facilitator settle is short-circuited.
    pulse_raw = (settings.pulse_call_price or "").lstrip("$").strip()
    try:
        pulse_amt = float(pulse_raw) if pulse_raw else 0.0
    except ValueError:
        pulse_amt = 0.0
    if pulse_amt > 0:
        routes["POST /pulse"] = RouteConfig(
            accepts=[
                PaymentOption(
                    scheme="exact",
                    pay_to=pay_to,
                    price=settings.pulse_call_price,
                    network=chain_id,
                    extra=svm_extra,
                ),
            ],
            description=(
                "Recurring product validation: re-runs the advisor panel "
                "against evolving evidence to surface verdict shifts as "
                "build progresses"
            ),
        )
    return routes


def _build_resource_server(facilitator: FacilitatorClient) -> x402ResourceServer:
    server = x402ResourceServer(facilitator)
    # Wildcard registration covers both "solana-devnet" / "solana-mainnet"
    # (V1 names) and the CAIP-2 form returned post-normalization.
    svm_scheme: Any = ExactSvmServerScheme()  # type: ignore[no-untyped-call]
    server.register("solana:*", svm_scheme)
    server.register("solana-devnet", svm_scheme)
    server.register("solana-mainnet", svm_scheme)
    # EVM networks (Base mainnet / Base Sepolia) use CAIP-2 eip155 chain IDs.
    evm_scheme: Any = ExactEvmScheme()  # type: ignore[no-untyped-call]
    server.register("eip155:*", evm_scheme)
    server.register("base-mainnet", evm_scheme)
    server.register("base-sepolia", evm_scheme)
    return server


# Load .env before Settings.from_env() so module-level initialization picks
# up local dotenv overrides (lifespan's load_dotenv would be too late).
# override is gated on GECKO_LOCAL_DEV=1 — production sets env from SSM and
# must never let a baked-in .env stomp on those values. Dev exports the flag
# explicitly when they want the local .env to win over stale shell vars
# (e.g. X402_NETWORK=base-mainnet exported before devnet was configured).
load_dotenv(override=os.environ.get("GECKO_LOCAL_DEV") == "1")

# Module-level so the lifespan + the /.well-known endpoint share one config.
_settings = Settings.from_env()
_facilitator = _build_facilitator(_settings)
_routes_config = _build_routes(_settings)
# S14-CDP-HARDEN-02 — startup-time advertisement contract.
# Fail fast if any route's payTo doesn't match its network family.
_assert_routes_payto_well_formed(_routes_config)
_resource_server = _build_resource_server(_facilitator)

# Strong refs to background tasks so they aren't GC'd mid-flight. Tasks
# remove themselves from the set in their done callback.
_BACKGROUND_TASKS: set[asyncio.Task[None]] = set()

# Parallel mapping task -> session_id for in-flight research workflows.
# Used by the lifespan shutdown handler to stamp orphaned sessions as
# `interrupted` when the container is drained mid-flight (ECS rolling
# deploy, autoscale). Mutated under the same add/discard pattern as
# _BACKGROUND_TASKS so the two sets stay consistent — an entry only
# exists here while the task is still in _BACKGROUND_TASKS.
_INFLIGHT_SESSIONS: dict[asyncio.Task[None], UUID] = {}


def _track_inflight(task: asyncio.Task[None], session_id: UUID) -> None:
    """Register a background research task with its session_id.

    Wires up the done-callback that removes the entry. Callers should
    use this helper instead of mutating _INFLIGHT_SESSIONS directly so
    the cleanup contract stays in one place.
    """
    _INFLIGHT_SESSIONS[task] = session_id

    def _drop(t: asyncio.Task[None]) -> None:
        _INFLIGHT_SESSIONS.pop(t, None)
        _BACKGROUND_TASKS.discard(t)

    task.add_done_callback(_drop)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # S8-LOG-01 — install the redaction filter at app startup so httpcore /
    # hpack DEBUG dumps in CloudWatch don't leak Supabase JWT, Bearer
    # tokens, or apikeys.
    from gecko_core._logging import install as install_redaction

    level_name = (os.environ.get("LOG_LEVEL") or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    install_redaction(level=level)

    # 2026-05-06 — downgrade `/sessions/{id}/result` 425 polls from INFO
    # to DEBUG so a single 60s research session doesn't flood the access
    # log with ~20 "still working" lines. The 425 IS the documented
    # contract; this is log hygiene only. See gecko_api.access_log_filter.
    from gecko_api.access_log_filter import install as install_access_filter

    install_access_filter()

    logger.info("gecko-api starting (X402_MODE=%s)", _settings.x402_mode)
    # S23-FIX2 — TAVILY_API_KEY is optional in gecko-mcp but required here.
    # Without it the first research call burns session + embedding work then
    # crashes at the Tavily discovery step. Fail fast at startup.
    if not os.environ.get("TAVILY_API_KEY"):
        raise RuntimeError("TAVILY_API_KEY is required for gecko-api startup")
    if not _settings.is_privy_configured():
        logger.warning(
            "Privy unconfigured — per-project wallet provisioning disabled. "
            "Set PRIVY_APP_ID and PRIVY_APP_SECRET to enable."
        )
    else:
        logger.info("Privy configured: per-project wallet provisioning ON")
    # S22-MCP-HOST-03 — drive the FastMCP Streamable HTTP session manager
    # for the lifetime of the FastAPI app. The session manager handles
    # SSE keepalives + stateless POST routing for the /mcp mount.
    #
    # Caveat: `StreamableHTTPSessionManager.run()` is one-shot per instance
    # (the SDK guards `_has_started`). Production hits lifespan exactly
    # once at boot, so this is fine. Test harnesses (TestClient) spin up
    # multiple lifespans against the module-level FastMCP singleton; we
    # detect the "already started" path and treat it as a no-op so the
    # existing gecko-api test suite keeps green without rebuilding the
    # whole FastMCP instance per test.
    mcp_cm: contextlib.AbstractAsyncContextManager[Any] | None = None
    if not getattr(_gecko_mcp.session_manager, "_has_started", False):
        mcp_cm = _gecko_mcp.session_manager.run()
        await mcp_cm.__aenter__()
    try:
        try:
            yield
        finally:
            # 2026-05-06 — ECS rolling-deploy / autoscale shutdown handler.
            # Production logs showed two `Shutting down` events mid-session:
            # the container's research workflow gets SIGTERMed, the session
            # row is left at status='generating', and clients poll 425
            # forever. Stamp every still-running session this worker is
            # holding as 'interrupted' so the result route can answer
            # 410 Gone instead. We do NOT wait for tasks to complete — they
            # already lost their event loop; this is bookkeeping for the DB
            # row, not a graceful drain. Full session-resume is a separate
            # ticket; here we just stop the zombie state.
            await _drain_inflight_sessions()
    finally:
        if mcp_cm is not None:
            # Suppress shutdown errors — the test path may close the MCP
            # session manager out of order; production only reaches this
            # branch on container SIGTERM where best-effort is sufficient.
            with contextlib.suppress(Exception):
                await mcp_cm.__aexit__(None, None, None)


async def _drain_inflight_sessions() -> None:
    """Mark every in-flight session this worker holds as interrupted.

    Called from the lifespan shutdown branch. Idempotent — `mark_interrupted`
    only flips rows whose status is non-terminal, so a session that
    raced to `complete`/`failed` between shutdown signal and this UPDATE
    landing keeps its real terminal state.
    """
    if not _INFLIGHT_SESSIONS:
        return
    snapshot = list(_INFLIGHT_SESSIONS.values())
    logger.warning(
        "lifespan.shutdown.interrupting count=%d session_ids=%s",
        len(snapshot),
        [str(s) for s in snapshot],
    )
    try:
        store = SessionStore.from_env()
    except Exception as exc:  # pragma: no cover — store-init failure is rare
        logger.error("lifespan.shutdown.store_init_failed err=%s", exc)
        return
    for sid in snapshot:
        try:
            await store.mark_interrupted(sid, reason="container_shutdown")
        except Exception as exc:
            # Best-effort: log and move on. Losing one stamp is better
            # than hanging the whole shutdown handler on one bad row.
            logger.error(
                "lifespan.shutdown.mark_interrupted_failed sid=%s err=%s",
                sid,
                exc,
            )


# Rate limiter: prefer Authorization header as the bucket key (so a single
# user can't share rate by spoofing IPs); fall back to remote address for
# unauthenticated paths. The limiter is exposed via app.state for slowapi's
# decorator + handler. Lives in ``gecko_api.rate_limit`` so route packages
# (e.g. ``gecko_api.routes.verdict``) can decorate handlers without pulling
# this module into a circular import.
from gecko_api.rate_limit import limiter  # noqa: E402

app = FastAPI(
    title="Gecko API",
    description="Builder Bootstrap Platform — backend for app.geckovision.tech.",
    version="0.2.0",
    lifespan=lifespan,
)
app.state.limiter = limiter


async def _rate_limit_handler(request: Request, exc: Exception) -> JSONResponse:
    # slowapi raises RateLimitExceeded; the handler signature must accept
    # the base Exception type for FastAPI's exception_handlers registry.
    detail = str(exc) if isinstance(exc, RateLimitExceeded) else "rate limited"
    return JSONResponse(status_code=429, content={"detail": detail})


app.add_exception_handler(RateLimitExceeded, _rate_limit_handler)


# ---------------------------------------------------------------------------
# S14-HARDEN-02 — Supabase / postgrest 5xx → structured BACKEND_STORE_UNAVAILABLE.
#
# Dogfood findings F-S14-1 + F-S14-2 surfaced repeated Supabase outages where
# a Cloudflare 522 / 525 / 526 (or a postgrest 502/503) bubbled up to the
# client as a generic 500. The frames.ag bridge maps 500s into a generic
# UPSTREAM_ERROR — we want session-store outages to be distinguishable so
# the bridge can apply the right retry SLO (60s) instead of the
# upstream-failure default.
#
# Mapping policy:
#   * code in {502, 503, 522, 525, 526}  → 503 with retry_after=60.
#   * Anything else falls through to FastAPI's default 500 path so we
#     don't accidentally mask a SQL-shape bug as a transient outage.
# ---------------------------------------------------------------------------


_BACKEND_STORE_5XX_CODES: frozenset[str] = frozenset({"502", "503", "522", "525", "526"})

_BACKEND_STORE_RETRY_AFTER_SECONDS = 60


def _is_backend_store_5xx(code: object) -> bool:
    """Match the API-error code against the configured 5xx allowlist.

    The postgrest ``APIError.code`` field can be a str (HTTP-shaped) or
    occasionally a numeric str-coercible — coerce defensively before
    comparing.
    """
    if code is None:
        return False
    return str(code).strip() in _BACKEND_STORE_5XX_CODES


async def _supabase_5xx_handler(request: Request, exc: Exception) -> JSONResponse:
    """Map Supabase / postgrest backend-down errors to a structured 503.

    The frames.ag bridge keys off the ``error`` code field —
    ``BACKEND_STORE_UNAVAILABLE`` is distinct from the existing
    ``UPSTREAM_ERROR`` so the bridge can apply a session-store-specific
    retry policy (different SLOs).
    """
    code = getattr(exc, "code", None)
    message = getattr(exc, "message", None) or "session store temporarily unavailable"
    if not _is_backend_store_5xx(code):
        # Re-raise so FastAPI's default 500 handler renders this; we do
        # NOT want to mask SQL-shape bugs (e.g. PGRST106) as transient.
        raise exc
    logger.warning(
        "supabase 5xx mapped to BACKEND_STORE_UNAVAILABLE: code=%s message=%s path=%s",
        code,
        message,
        request.url.path,
    )
    return JSONResponse(
        status_code=503,
        content={
            "error": "BACKEND_STORE_UNAVAILABLE",
            "code": str(code) if code is not None else "unknown",
            "message": "session store temporarily unavailable",
            "retry_after": _BACKEND_STORE_RETRY_AFTER_SECONDS,
            "detail": message,
        },
        headers={"Retry-After": str(_BACKEND_STORE_RETRY_AFTER_SECONDS)},
    )


# Register lazily — postgrest may not be importable in degraded test
# environments. Failing-fast on import here would break the unrelated
# stub-mode test surface.
try:
    from postgrest.exceptions import APIError as _PostgrestAPIError

    app.add_exception_handler(_PostgrestAPIError, _supabase_5xx_handler)
except ImportError:  # pragma: no cover — postgrest is a hard dep in production
    logger.warning("postgrest not importable — Supabase 5xx mapping disabled")

# CORS FIRST, then x402. Browsers need to be able to read the 402 headers.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://app.geckovision.tech",
        "https://geckovision.tech",
        "http://localhost:3000",
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
    # Expose the 402-related headers so browser-side clients can react.
    expose_headers=["X-PAYMENT-RESPONSE", "X-PAYMENT", "WWW-Authenticate"],
)

app.add_middleware(
    PaymentMiddlewareASGI,
    routes=_routes_config,
    server=_resource_server,
)


# ---------------------------------------------------------------------------
# S12-HARDEN-01 — unauthenticated rate limit on the public 402 surface.
#
# Listing in CDP Bazaar discovery exposes /research and /plan to anonymous
# probes. slowapi handles authenticated /projects routes; this middleware
# adds a separate IP-bucket limit ahead of x402 so abuse traffic gets
# rejected before we burn a verify call. Authenticated paid traffic
# bypasses entirely — payment is itself the rate gate.
#
# Added AFTER PaymentMiddlewareASGI so it wraps it (Starlette's
# add_middleware adds new layers as the OUTER wrapper).
# ---------------------------------------------------------------------------


# 30 requests/minute/IP — generous for legitimate browsing (Bazaar
# discovery probes, manual exploration), tight enough that scraping or
# DDoS becomes uneconomical.
_UNPAID_RATE_LIMIT_PER_MINUTE = 30
_RATE_LIMITED_PATHS = frozenset({"/research", "/research/pro", "/plan"})

# Phase 10A — /trade_research is heavier than /research (AG2 7-agent
# debate + Mongo retrieval per call), so it gets a stricter unpaid cap.
_TRADE_RESEARCH_RATE_LIMIT_PER_MINUTE = 10
_TRADE_RESEARCH_RATE_LIMITED_PATHS = frozenset({"/trade_research", "/trade_research/pro"})


# Per-IP token bucket state. Single-process only; Sprint 13+ should
# migrate to Redis for multi-replica deploys.
_unpaid_rate_state: dict[str, list[float]] = {}
_unpaid_rate_lock = asyncio.Lock()


def _has_payment_header(headers: dict[bytes, bytes]) -> bool:
    """True if the request looks like an authenticated paid call."""
    for name in (b"x-payment", b"payment-signature", b"x-payment-signature"):
        if headers.get(name):
            return True
    return False


async def _send_json_error(
    send: Callable[[dict[str, Any]], Awaitable[None]],
    *,
    status: int,
    detail: str,
    extra_headers: list[tuple[bytes, bytes]] | None = None,
) -> None:
    """Shared ASGI-level JSON error response used by the harden middlewares."""
    body = json.dumps({"detail": detail}).encode()
    headers: list[tuple[bytes, bytes]] = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
    ]
    if extra_headers:
        headers.extend(extra_headers)
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": headers,
        }
    )
    await send({"type": "http.response.body", "body": body, "more_body": False})


class _UnauthenticatedRateLimitMiddleware:
    """30/min/IP rate limit on the public 402-path routes."""

    def __init__(
        self,
        app: Any,
        *,
        rate_per_minute: int,
        paths: frozenset[str],
    ) -> None:
        self.app = app
        self.rate = int(rate_per_minute)
        self.paths = paths
        self.window = 60.0
        # Per-instance bucket so two stacked middlewares (e.g. /research at
        # 30/min and /trade_research at 10/min) don't share counters.
        self._state: dict[str, list[float]] = {}

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        method = scope.get("method", "").upper()
        if method != "POST" or path not in self.paths:
            await self.app(scope, receive, send)
            return

        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        if _has_payment_header(headers):
            await self.app(scope, receive, send)
            return

        client = scope.get("client") or ("unknown", 0)
        client_ip = client[0] if isinstance(client, tuple | list) else "unknown"
        xff = headers.get(b"x-forwarded-for")
        if xff:
            with contextlib.suppress(UnicodeDecodeError, AttributeError):
                client_ip = xff.decode("latin-1").split(",", 1)[0].strip()[:64]

        now = time.monotonic()
        cutoff = now - self.window
        async with _unpaid_rate_lock:
            entries = self._state.get(client_ip, [])
            entries = [t for t in entries if t >= cutoff]
            if len(entries) >= self.rate:
                self._state[client_ip] = entries
                await _send_json_error(
                    send,
                    status=429,
                    detail=(
                        f"rate limit exceeded: {self.rate} requests/minute for "
                        "unauthenticated traffic. Authenticated x402 calls bypass."
                    ),
                    extra_headers=[(b"retry-after", b"60")],
                )
                return
            entries.append(now)
            self._state[client_ip] = entries

        await self.app(scope, receive, send)


# ---------------------------------------------------------------------------
# S12-HARDEN-02 — body-size cap (10KB) at the ASGI layer.
#
# The Pydantic field constraint on `idea` (max_length=2000) is a logical
# cap; this is the byte-level cap that prevents an attacker from sending
# a 100MB payload and forcing the server to allocate / parse it before
# handler validation runs. Reject at the streaming layer with a clean
# 400; downstream handlers never see the oversized payload.
# ---------------------------------------------------------------------------


# 10KB body cap — generous for the longest legitimate payload (idea up to
# 2000 chars + JSON envelope), tight enough that 100KB abuse attempts are
# rejected before any handler runs.
_MAX_REQUEST_BODY_BYTES = 10 * 1024


class _BodySizeLimitMiddleware:
    """Reject POST/PUT/PATCH bodies larger than ``max_bytes`` at the ASGI layer.

    Two checks: content-length preflight (cheapest), then a streaming byte
    count for chunked / unknown-length requests. On overflow we 400 and
    stop draining; on success we replay the buffered bytes as a single
    chunk to the downstream app.
    """

    def __init__(self, app: Any, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = int(max_bytes)

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        method = scope.get("method", "").upper()
        if method not in {"POST", "PUT", "PATCH"}:
            await self.app(scope, receive, send)
            return

        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        cl_raw = headers.get(b"content-length")
        if cl_raw is not None:
            try:
                cl = int(cl_raw.decode("latin-1"))
            except (ValueError, UnicodeDecodeError):
                cl = -1
            if cl > self.max_bytes:
                await _send_json_error(
                    send,
                    status=400,
                    detail=(f"request body exceeds {self.max_bytes} byte cap ({cl} bytes)"),
                )
                return

        max_bytes = self.max_bytes
        buffered: list[bytes] = []
        total = 0
        more_body = True
        while more_body:
            message = await receive()
            if message["type"] != "http.request":
                buffered.append(b"")
                break
            chunk = message.get("body", b"") or b""
            total += len(chunk)
            if total > max_bytes:
                await _send_json_error(
                    send,
                    status=400,
                    detail=(f"request body exceeds {max_bytes} byte cap ({total}+ bytes)"),
                )
                return
            buffered.append(chunk)
            more_body = message.get("more_body", False)

        full_body = b"".join(buffered)
        delivered = False

        async def _flat_receive() -> dict[str, Any]:
            nonlocal delivered
            if not delivered:
                delivered = True
                return {
                    "type": "http.request",
                    "body": full_body,
                    "more_body": False,
                }
            return {"type": "http.disconnect"}

        await self.app(scope, _flat_receive, send)


# ---------------------------------------------------------------------------
# S12-HARDEN-04 — cost circuit breaker on /research and /plan.
#
# When the rolling-window LLM-spend tracker (gecko_api.circuit_breaker)
# exceeds the per-minute budget, NEW POSTs are rejected with 503 +
# Retry-After: 30. Existing in-flight handlers complete normally — the
# breaker only short-circuits new arrivals. Sits ahead of x402 so we
# don't burn a verify call on a request we'll refuse anyway.
# ---------------------------------------------------------------------------


class _CostCircuitBreakerMiddleware:
    """Short-circuit /research and /plan when the cost breaker is open."""

    def __init__(self, app: Any, *, paths: frozenset[str]) -> None:
        self.app = app
        self.paths = paths

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        method = scope.get("method", "").upper()
        if method != "POST" or path not in self.paths:
            await self.app(scope, receive, send)
            return
        if _get_cost_breaker().is_open():
            await _send_json_error(
                send,
                status=503,
                detail=(
                    "service temporarily over capacity (cost circuit breaker "
                    f"open); retry after {_CIRCUIT_RETRY_AFTER_SECONDS}s"
                ),
                extra_headers=[(b"retry-after", str(_CIRCUIT_RETRY_AFTER_SECONDS).encode())],
            )
            return
        await self.app(scope, receive, send)


# Order outer→inner: body-size → rate-limit → cost-breaker → x402.
# Body-size is OUTERMOST so a 100KB abuse payload always returns 400.
# Rate-limit runs second so unpaid spam is rejected with 429 before the
# breaker has a chance to mask it as 503. Breaker sits just above x402
# so authenticated paid traffic is the only kind that ever sees it open.
# Each `add_middleware` wraps the previous, so add in REVERSE order:
# breaker first (innermost of the three), then rate-limit, then body-size.
app.add_middleware(_CostCircuitBreakerMiddleware, paths=_RATE_LIMITED_PATHS)
app.add_middleware(
    _UnauthenticatedRateLimitMiddleware,
    rate_per_minute=_UNPAID_RATE_LIMIT_PER_MINUTE,
    paths=_RATE_LIMITED_PATHS,
)
# Phase 10A — stricter 10/min cap on /trade_research endpoints. Stacked
# alongside the 30/min cap above; each instance shares the same per-IP
# bucket store but tracks its own paths frozenset, so a /research caller
# does not consume budget against /trade_research and vice versa.
app.add_middleware(
    _UnauthenticatedRateLimitMiddleware,
    rate_per_minute=_TRADE_RESEARCH_RATE_LIMIT_PER_MINUTE,
    paths=_TRADE_RESEARCH_RATE_LIMITED_PATHS,
)
app.add_middleware(_BodySizeLimitMiddleware, max_bytes=_MAX_REQUEST_BODY_BYTES)


# Bot-probe block. Known vuln-scanner paths — WordPress, .git, .env,
# phpMyAdmin, PHP/CGI extensions — get an instant 404 before routing.
# Deliberately a plain 404, NOT a 402: a payment challenge on a junk path
# tells a scanner the host is "interesting" and worth revisiting, whereas a
# 404 is a boring dead end. No tarpit — holding the connection open would
# be a self-DoS lever for an attacker who controls request volume.
_BOT_PROBE_EXTENSIONS: tuple[str, ...] = (
    ".php",
    ".asp",
    ".aspx",
    ".jsp",
    ".cgi",
    ".env",
    ".sql",
    ".bak",
)
_BOT_PROBE_SUBSTRINGS: tuple[str, ...] = (
    "/wp-admin",
    "/wp-includes",
    "/wp-login",
    "/wordpress",
    "/cgi-bin",
    "/.git",
    "/.env",
    "/phpmyadmin",
    "/xmlrpc",
    "/vendor/",
    "/.aws",
)


@app.middleware("http")
async def _bot_probe_block(request: Request, call_next: Any) -> Response:
    path = request.url.path.lower()
    if path.endswith(_BOT_PROBE_EXTENSIONS) or any(s in path for s in _BOT_PROBE_SUBSTRINGS):
        return Response(status_code=404)
    response: Response = await call_next(request)
    return response


# Internal ops routes (S2X-09: /internal/twitsh/balance for EventBridge).
# Mounted after middleware so x402 doesn't gate them; they're free and
# read-only. Discovery: not advertised in /.well-known/x402.
app.include_router(internal_router)


# S20-VERDICT-URL-IMPL-01 — public verdict-URL surface. Imported lazily
# (post-app-construction) so the route module can decorate handlers with
# the shared limiter. CORS for this route is wide open (handled inside
# the route via response headers — see routes/verdict.py module docstring)
# because the public app-wide CORSMiddleware is restricted to the
# geckovision.tech origins.
from gecko_api.routes.verdict import router as _verdict_router  # noqa: E402

app.include_router(_verdict_router)


# S20-B3 — single x402-gated dispatcher for the 12-skill manifest. Mounted
# AFTER middleware so the route's own dispatcher (X402Dispatcher) is the
# 402 gate, not the x402 PaymentMiddlewareASGI — the manifest skills are
# discovered via /.well-known/agent-skills/index.json and follow the new
# pay.sh-style accepts shape rather than the legacy /research route table.
from gecko_api.skills_router import router as _skills_router  # noqa: E402

app.include_router(_skills_router)


# ---------------------------------------------------------------------------
# S22-MCP-HOST-03 — mount FastMCP Streamable HTTP at /mcp
#
# The mount has to happen at module import time (after `app` is built and
# after middleware is registered) so the FastMCP session manager exists
# before `lifespan` runs. The session manager is created lazily on the
# first call to `streamable_http_app()`.
#
# The existing `PaymentMiddlewareASGI` (added at app.add_middleware above)
# wraps the entire ASGI app, so /mcp inherits the same x402 paywall
# treatment. Per the S22 deploy plan section 3, the **tool-level** paywall
# is what we want — the inner HTTP calls each MCP tool makes (via
# `GeckoAPIClient`) hit the per-route x402 gates (/research, /plan, ...)
# and those 402 challenges bubble up through the MCP envelope. Routes
# without a RouteConfig (i.e. /mcp itself) pass through unpaywalled, which
# is the intended behaviour — the MCP transport itself stays free; paid
# tools settle on the inner HTTP call.
#
# The web3-engineer audit (S22-MCP-HOST-06) verifies the WWW-Authenticate
# propagation end-to-end; this commit just wires the mount.
# ---------------------------------------------------------------------------
app.mount("/mcp", _gecko_mcp.streamable_http_app())


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class ResearchRequest(BaseModel):
    # S12-HARDEN-02 — input size cap on the idea field. 2000 chars is the
    # public contract; combined with the 10KB body-size middleware below
    # this caps OOM/abuse vectors before any handler runs.
    idea: str = Field(..., min_length=10, max_length=2000)
    tier: Tier = "basic"
    urls: list[str] | None = None
    auto_approve: bool = True
    # Phase B5 v1 — optional project envelope. v1 always pays from main wallet;
    # `paid_from_wallet_address` is recorded as "<frames_username>:main" for
    # audit. v2 will replace this with the per-project Privy wallet address.
    project_id: str | None = None
    frames_username: str | None = None
    # S6-TIER-02 — user-facing cost/quality preset (validated against the
    # Tier enum at request time). Default 'balanced'. Plumbed through
    # gecko_core.research → pro debate via the existing tier_preset arg.
    tier_preset: str = Field(default="balanced", pattern="^(quality|balanced|budget|free)$")
    # S23-FIX-12 (T1b) — optional vertical hint. Plumbed through to
    # ``gecko_core.research`` → ``rag_query`` so the marketplace corpus
    # read fires when ``GECKO_MARKETPLACE_ROUTING_ENABLED`` is on.
    # Unvalidated at the wire boundary; the RAG/taxonomy layer is the
    # source of truth for known verticals and unknown values degrade
    # gracefully (corpus read returns empty, session-scoped read still works).
    vertical: str | None = None


class AskRequest(BaseModel):
    question: str = Field(..., min_length=3, max_length=500)


class TelemetryEventRequest(BaseModel):
    """S33-#73 — body shape for POST /events (top-of-funnel telemetry).

    Unauthenticated by design: it must fire before a wallet exists. All
    string fields are length-capped at the wire boundary so an abusive
    client cannot bloat the row; `metadata` is capped by a field validator.
    `event_type` is free-text — the known values live in
    `gecko_core.telemetry.store.KNOWN_TELEMETRY_EVENTS`, not a Literal.
    """

    event_type: str = Field(..., min_length=1, max_length=64)
    wallet_address: str | None = Field(default=None, max_length=128)
    email: str | None = Field(default=None, max_length=320)
    installer_tag: str | None = Field(default=None, max_length=128)
    metadata: dict[str, Any] | None = None

    @field_validator("metadata")
    @classmethod
    def _cap_metadata(cls, v: dict[str, Any] | None) -> dict[str, Any] | None:
        # Defensive size cap — telemetry metadata is small key/value context
        # (OS, installer version, error string). Reject anything large.
        if v is None:
            return None
        if len(v) > 25:
            raise ValueError("metadata has too many keys (max 25)")
        if len(json.dumps(v, default=str)) > 4096:
            raise ValueError("metadata too large (max 4KB serialized)")
        return v


class RouteRequest(BaseModel):
    """S4-ROUTE-02 — request shape for POST /route."""

    prompt: str = Field(..., min_length=1, max_length=20_000)
    task_hint: str = "default"
    max_cost_usd: float = Field(default=0.05, ge=0.0)
    prefer_premium: bool = False
    # S6-TIER-02 — user-facing cost/quality preset, validated against the
    # Tier enum (quality|balanced|budget|free). Default 'balanced'. v1
    # ignores the value at the routing layer and falls through to the
    # legacy matrix; the validation here keeps the wire shape stable so
    # the MCP/CLI/web clients converge on a single vocabulary.
    tier_preset: str = Field(default="balanced", pattern="^(quality|balanced|budget|free)$")


class ReviewRequest(BaseModel):
    """S7-DOGFOOD-02 — request shape for POST /review (sprint review meta)."""

    project_id: str | None = None
    since_days: int = Field(default=14, ge=1, le=365)
    tier_preset: str = Field(default="balanced", pattern="^(quality|balanced|budget|free)$")


class PlanRequest(BaseModel):
    """S5-API-01 — request shape for POST /plan (paid Advisor Panel)."""

    session_id: str = Field(..., min_length=1)
    # S6-TIER-02 — validated tier_preset (was free-form string).
    tier_preset: str = Field(default="balanced", pattern="^(quality|balanced|budget|free)$")
    project_id: str | None = None
    # Optional attribution for paid_from audit, mirrors /research's pattern.
    frames_username: str | None = None


class ScaffoldRequest(BaseModel):
    """S8-API-01 — HTTP shape for POST /scaffold.

    Mirrors the MCP ``gecko_scaffold`` tool's input schema. ``output_dir``
    is server-side and intentionally NOT exposed over HTTP — clients can't
    write to the API host's filesystem; the response surfaces the bundle
    contents (paths under the server's working dir) for the caller to fetch
    or render. Future work: stream the bundle bytes back in the response.
    """

    session_id: str = Field(..., min_length=1)


class PulseRequest(BaseModel):
    """S8-API-01 / S14-PULSE-01 — HTTP shape for POST /pulse.

    Either ``session_id`` or ``project_id`` is required.

    When ``session_id`` is provided, runs the v14 pulse engine: creates
    a new ``phase="during_build"`` session row, re-runs the advisor
    panel, and returns a PulseResult (verdict + gap_classification +
    closing lines + windowed citations).

    Legacy S5-API-02 surface (project_id only) still runs the
    closing-line delta detection. session_id wins when both are given.

    ``user_wallet`` is optional. When set, the route checks 12-pack
    credits BEFORE the per-call x402 settle (S14-PULSE-02).
    """

    session_id: str | None = None
    project_id: str | None = None
    tier_preset: str = Field(default="balanced", pattern="^(quality|balanced|budget|free)$")
    user_wallet: str | None = None


# ---------------------------------------------------------------------------
# S13-COMMO-01..03 — Track E commoditization request shapes.
# ---------------------------------------------------------------------------


_VOICE_CHOICES_LITERAL = (
    "ceo",
    "cto",
    "business_manager",
    "product_manager",
    "staff_manager",
    "bm",
    "pm",
    "sm",
)


class AdviseRequest(BaseModel):
    """S13-COMMO-01 — request shape for paid POST /advise.

    Mirrors the MCP `gecko_advise` tool. `voice` aliases (bm/pm/sm) resolve
    inside `gecko_core.orchestration.advisor.generate_voice`.
    """

    session_id: str = Field(..., min_length=1)
    voice: str = Field(
        ...,
        pattern="^(ceo|cto|business_manager|product_manager|staff_manager|bm|pm|sm)$",
    )
    tier_preset: str = Field(default="balanced", pattern="^(quality|balanced|budget|free)$")


class PaidAskRequest(BaseModel):
    """S13-COMMO-02 — request shape for paid POST /ask.

    Distinct from `AskRequest` (used by the free `/sessions/{id}/ask`) because
    the paid surface carries `session_id` in the body — Bazaar collapses
    bare-UUID path segments, so a paid `/sessions/{id}/ask` would lose its
    listing. Keeping the body-keyed `/ask` keeps the catalog entry distinct.
    """

    session_id: str = Field(..., min_length=1)
    question: str = Field(..., min_length=3, max_length=500)


class TradeResearchRequest(BaseModel):
    """Phase 8b — request shape for POST /trade_research.

    Free in v1 (no x402 gate). The 7-agent panel reads the trading-oracle
    corpus filtered by ``(vertical, protocol)`` and returns the verdict
    shape. ``protocol`` is required and lower-cased downstream.
    """

    idea: str = Field(..., min_length=3, max_length=2000)
    protocol: str = Field(..., min_length=1, max_length=64)
    vertical: str = Field(default="dex", min_length=1, max_length=64)
    tier: Tier = "basic"


def _extract_settle_receipt(request: Request) -> tuple[str | None, str]:
    """S24 WS-E — pull (tx_signature, x402_mode) off the request.

    The Coinbase x402 middleware attaches ``payment_payload`` to
    ``request.state`` on a settled call. When the payload includes a
    ``tx_signature`` key (live settle), we surface it; otherwise we treat
    the call as stub. ``X402_MODE`` is read from the resolved settings so
    a stub-mode handler never advertises a live receipt even if the
    middleware leaks one.
    """
    import os

    payload = getattr(request.state, "payment_payload", None)
    tx_sig: str | None = None
    if isinstance(payload, dict):
        raw = payload.get("tx_signature") or payload.get("tx_hash")
        if isinstance(raw, str) and raw:
            tx_sig = raw
    mode = os.environ.get("X402_MODE", "stub")
    return tx_sig, mode


class TradeResearchResponse(BaseModel):
    """Wire response for POST /trade_research.

    Mirrors :class:`gecko_core.orchestration.trade_panel.TradePanelVerdict`
    field-for-field. The ``turns`` list carries plain dicts so we don't
    couple this surface to the core's pydantic model when consumers
    deserialize off the wire.

    Issue #14: ``backtest`` is the pro-tier-only field that justifies the
    3x price delta over basic. Always ``None`` on the basic envelope (the
    handler does not enable the backtest path); always a dict on pro
    (either real PnL stats, or ``unbacktestable=True`` + ``reason`` when
    the price source has no history). Surfacing the unbacktestable shape
    is intentional — the buyer paid for evidence either way.
    """

    verdict: Literal["act", "pass", "defer"]
    confidence: float = Field(..., ge=0.0, le=1.0)
    key_drivers: list[str] = Field(default_factory=list)
    dissent_count: int = Field(default=0, ge=0)
    blocker_questions: list[str] = Field(default_factory=list)
    turns: list[dict[str, Any]] = Field(default_factory=list)
    evidence_citations: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "S35-#99 — 'the data'. Protocol/market-data chunks a panel turn "
            "referenced via its inline [N] marker (provider_kind in "
            "protocol_native / market_data / paysh_live / bazaar_live). Each "
            "entry: {id, source, url, chunk_id, provider_kind, freshness_tier, "
            "snippet}. Breaking change: replaces the old single citations[]."
        ),
    )
    framework_context: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "S35-#99 — 'the lens'. Investor-canon chunks (provider_kind "
            "canon_*) the panel reasoned over. Same item shape as "
            "evidence_citations. Not relevance-trimmed: canon is cross-cutting."
        ),
    )
    backtest: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Pro-tier only. Realized-history replay of the Strategist intent "
            "(pnl_pct, drawdown_pct, hit_rate, source) or {'unbacktestable': "
            "True, 'reason': '...'} when the price corpus can't satisfy. "
            "Always None on the basic envelope."
        ),
    )
    # S24 WS-E — settlement receipt (mirrors TradePanelVerdict).
    tx_signature: str | None = Field(
        default=None,
        description="Solana on-chain x402 settlement signature; null in stub mode.",
    )
    solscan_url: str | None = Field(
        default=None,
        description="Solscan deep link for tx_signature; null in stub mode.",
    )
    settlement_mode: Literal["stub", "live"] = Field(
        default="stub",
        description=(
            "Whether real money moved on chain. Canonical literal lives in "
            "gecko_core.types.SettlementMode (Pattern A)."
        ),
    )


class ClassifyRequest(BaseModel):
    """S13-COMMO-03 — request shape for paid POST /classify.

    Returns categories + suggested sources + priority weights without
    running the full research pipeline. Useful for agents that want to
    pre-flight Gecko's source selection before paying for /research.
    """

    idea: str = Field(..., min_length=10, max_length=500)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# S12-HARDEN-04 — coarse LLM-spend estimate fed to the cost circuit
# breaker at the top of each handler. Conservative; the breaker only
# needs a window signal, not exact accounting. Values tuned so a burst
# of ~50 concurrent /research calls trips the $5/min cap.
_ROUTE_SPEND_ESTIMATE_USD: dict[str, float] = {
    "POST /research": 0.10,
    "POST /research/pro": 1.50,
    "POST /plan": 0.10,
}


def _route_price_usd(route: str) -> float:
    """Pull the advertised USD price for a route from the x402 config.

    Strips the `$` prefix and parses to float. Used by handlers to record
    price_usd on the session so margin can be computed.
    """
    cfg = _routes_config.get(route)
    if cfg is None:
        return 0.0
    accepts = cfg.accepts if isinstance(cfg.accepts, list) else [cfg.accepts]
    if not accepts:
        return 0.0
    raw = str(accepts[0].price).lstrip("$")
    try:
        return float(raw)
    except ValueError:
        return 0.0


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.post("/research", status_code=202)
async def research(req: ResearchRequest, request: Request) -> Any:
    """Kick off a research session. Returns 202 + session_id immediately.

    The full pipeline runs as a background task — frames.ag's /x402/fetch
    upstream timeout is ~30s, but the workflow takes 60-90s. By returning
    fast we let payment settle synchronously while research keeps running;
    the client polls GET /sessions/{id}/result for completion.
    """
    # S12-HARDEN-04 — record an estimated LLM spend so the rolling-window
    # breaker has signal even when the background workflow hasn't reported
    # actual cost yet. Conservative; refined post-completion via SessionStore.
    _get_cost_breaker().record_spend(_ROUTE_SPEND_ESTIMATE_USD["POST /research"])

    store = SessionStore.from_env()

    # S2-06: per-project budget cap pre-flight. Reject at-or-over-cap projects
    # with a 402 carrying a `project_budget_exceeded` error before we burn any
    # work. The wallet's own balance is the cryptographic ceiling at mainnet
    # time; this column-driven gate is the v1 server-side enforcement.
    if req.project_id:
        try:
            project_uuid = UUID(req.project_id)
        except ValueError:
            project_uuid = None
        if project_uuid is not None:
            blocked = await check_project_budget(store=store, project_id=project_uuid)
            if blocked is not None:
                return blocked
            # S2-05: lazy wallet provisioning on first paid call when a
            # project pre-exists without one. Soft-skips when Privy isn't
            # configured; never blocks the call.
            try:
                await ensure_project_wallet(
                    settings=_settings, store=store, project_id=project_uuid
                )
            except Exception:  # pragma: no cover — best-effort
                logger.exception("research: ensure_project_wallet failed")

    # Create the session row up front so the client has a handle to poll.
    session_id: UUID = await store.create(idea=req.idea, tier="basic")

    # Persist the price the user just paid so the economics view is accurate
    # even before the background task finishes.
    payload = getattr(request.state, "payment_payload", None)
    if payload is not None:
        try:
            await store.set_tx_signature(session_id, "pending-settle")
            await store.set_price(session_id, _route_price_usd("POST /research"))
        except Exception as exc:  # pragma: no cover — best-effort
            logger.warning("failed to persist payment marker: %s", exc)

    # Phase B5 v1 — attach project + record paying wallet for audit.
    if req.project_id:
        try:
            project_uuid = UUID(req.project_id)
            paid_from = f"{req.frames_username}:main" if req.frames_username else None
            await store.set_session_project(
                session_id,
                project_uuid,
                paid_from_wallet_address=paid_from,
            )
        except (ValueError, Exception) as exc:  # pragma: no cover — best-effort
            logger.warning("failed to attach project_id %s: %s", req.project_id, exc)

    task = asyncio.create_task(_run_research_background(session_id, req))
    _BACKGROUND_TASKS.add(task)
    _track_inflight(task, session_id)

    return {
        "session_id": str(session_id),
        "status": "processing",
        "poll_url": f"/sessions/{session_id}/result",
    }


async def _run_research_background(session_id: UUID, req: ResearchRequest) -> None:
    """Run the gecko_core workflow under an existing session_id.

    Persists the result to sessions.result_json on success or sessions.
    error_message on failure. Errors here never crash the request — the
    client polls /sessions/{id}/result and sees a 500-with-detail or
    a 425 still-processing.
    """
    store = SessionStore.from_env()
    try:
        result = await gecko_core.research(
            idea=req.idea,
            tier="basic",
            urls=req.urls,
            auto_approve=True,
            skip_payment_gate=True,
            session_id=session_id,
            tier_preset=req.tier_preset,
            vertical=req.vertical,
        )
        await store.set_result(session_id, result.model_dump(mode="json"))
        await store.update_status(session_id, "complete")
        # S2-06: roll the session price into projects.spent_usd. The atomic
        # RPC handles concurrent completes for the same project. Failure is
        # swallowed inside the helper — spend tracking is best-effort.
        if req.project_id:
            try:
                pid = UUID(req.project_id)
            except ValueError:
                pid = None
            if pid is not None:
                price = Decimal(str(_route_price_usd("POST /research")))
                await increment_project_spent_safe(store=store, project_id=pid, delta_usd=price)
    except NotImplementedError as exc:
        logger.warning("research session %s not implemented: %s", session_id, exc)
        await store.set_error(session_id, f"NotImplemented: {exc}")
    except Exception as exc:
        logger.exception("research session %s failed", session_id)
        await store.set_error(session_id, f"{type(exc).__name__}: {exc}")


@app.post("/research/pro", status_code=202)
async def research_pro(req: ResearchRequest, request: Request) -> Any:
    """Kick off a pro-tier research session (5-agent debate via AG2).

    Async contract identical to /research:
        202 → {session_id, status, poll_url, events_url, events_token}
    The events_token is HMAC-signed, scoped to this session, and expires in
    10 minutes. Clients pass it as `?token=...` (or Authorization: Bearer ...)
    on GET /research/pro/{session_id}/events to subscribe to the SSE stream.
    """
    _get_cost_breaker().record_spend(_ROUTE_SPEND_ESTIMATE_USD["POST /research/pro"])

    store = SessionStore.from_env()

    # S2-06: same pre-flight gate as /research — block the paid call when
    # the project has hit its cap. S2-05: lazy-provision the project's
    # Privy wallet if missing.
    if req.project_id:
        try:
            project_uuid = UUID(req.project_id)
        except ValueError:
            project_uuid = None
        if project_uuid is not None:
            blocked = await check_project_budget(store=store, project_id=project_uuid)
            if blocked is not None:
                return blocked
            try:
                await ensure_project_wallet(
                    settings=_settings, store=store, project_id=project_uuid
                )
            except Exception:  # pragma: no cover — best-effort
                logger.exception("research_pro: ensure_project_wallet failed")

    session_id: UUID = await store.create(idea=req.idea, tier="pro")

    payload = getattr(request.state, "payment_payload", None)
    if payload is not None:
        try:
            await store.set_tx_signature(session_id, "pending-settle")
            await store.set_price(session_id, _route_price_usd("POST /research/pro"))
        except Exception as exc:  # pragma: no cover — best-effort
            logger.warning("failed to persist pro payment marker: %s", exc)

    if req.project_id:
        try:
            project_uuid = UUID(req.project_id)
            paid_from = f"{req.frames_username}:main" if req.frames_username else None
            await store.set_session_project(
                session_id,
                project_uuid,
                paid_from_wallet_address=paid_from,
            )
        except (ValueError, Exception) as exc:  # pragma: no cover — best-effort
            logger.warning("failed to attach project_id %s: %s", req.project_id, exc)

    task = asyncio.create_task(_run_pro_background(session_id, req))
    _BACKGROUND_TASKS.add(task)
    _track_inflight(task, session_id)

    events_token = issue_token(session_id, _settings.events_secret)
    return {
        "session_id": str(session_id),
        "status": "processing",
        "poll_url": f"/sessions/{session_id}/result",
        "events_url": f"/research/pro/{session_id}/events",
        "events_token": events_token,
    }


async def _run_pro_background(session_id: UUID, req: ResearchRequest) -> None:
    """Run the gecko_core workflow under tier='pro' for an existing session.

    Thin wrapper kept for backwards compatibility with tests that patch
    `_run_pro_background` directly. New paths should call `_run_pro_debate`.
    """
    await _run_pro_debate(
        session_id=session_id,
        idea=req.idea,
        urls=req.urls,
        project_id=req.project_id,
        tier_preset=req.tier_preset,
        vertical=req.vertical,
    )


async def _run_pro_debate(
    *,
    session_id: UUID,
    idea: str,
    urls: list[str] | None = None,
    project_id: str | None = None,
    tier_preset: str = "balanced",
    vertical: str | None = None,
) -> None:
    """Run a Pro debate against an existing session_id.

    On failure: persist the error AND emit a synthetic `final` SSE event
    carrying a fresh `retry_token` (24h TTL, retry-prefixed). The MCP client
    extracts the token from the SSE final payload and surfaces a "retry
    without recharge" prompt to the user. Single-use is enforced by the
    status guard on the retry endpoint (failed → running burns the token).
    """
    store = SessionStore.from_env()
    try:
        result = await gecko_core.research(
            idea=idea,
            tier="pro",
            urls=urls,
            auto_approve=True,
            skip_payment_gate=True,
            session_id=session_id,
            tier_preset=tier_preset,
            vertical=vertical,
        )
        await store.set_result(session_id, result.model_dump(mode="json"))
        await store.update_status(session_id, "complete")
        if project_id:
            try:
                pid = UUID(project_id)
            except ValueError:
                pid = None
            if pid is not None:
                price = Decimal(str(_route_price_usd("POST /research/pro")))
                await increment_project_spent_safe(store=store, project_id=pid, delta_usd=price)
    except Exception as exc:
        logger.exception("pro research session %s failed", session_id)
        msg = f"{type(exc).__name__}: {exc}"
        await store.set_error(session_id, msg)
        # Emit a synthetic final event so any subscribed SSE client sees the
        # failure (and retry_token) without waiting on the 300s timeout. The
        # token is also embedded in the row content as JSON; the SSE handler
        # surfaces it as the `retry_token` field on the outgoing payload.
        try:
            retry_token = issue_retry_token(session_id, _settings.events_secret)
            failure_payload = json.dumps({"error": msg, "retry_token": retry_token})
            # `seq` must be monotonically increasing; we don't know the last
            # seq the workflow used, so pick a value high enough to clear it.
            # The pro debate emits ~10-20 events; 1_000_000 is a safe ceiling.
            await store.append_pro_event(
                session_id=session_id,
                seq=1_000_000,
                event_type="final",
                agent=None,
                content=failure_payload,
                tokens_in=0,
                tokens_out=0,
                ts=asyncio.get_event_loop().time(),
            )
        except Exception as inner:  # pragma: no cover — best-effort
            logger.warning("could not emit failure-final SSE event: %s", inner)


# ---------------------------------------------------------------------------
# /research/pro/{session_id}/events — SSE stream of AgentEvents.
#
# Two-tier auth: the events_token is the session-scoped credential; the
# parent /research/pro POST already settled payment via x402. This endpoint
# is read-after-payment, so we don't gate it again.
#
# Loop: poll `pro_events` every 250ms with `id > last_id`. Yield each row
# as `event: turn / data: <json>`. Heartbeat every 15s. Closes when an
# event_type='final' row arrives or after a 300s hard timeout.
# ---------------------------------------------------------------------------


_SSE_POLL_INTERVAL_S = 0.25
_SSE_HEARTBEAT_INTERVAL_S = 15.0
_SSE_HARD_TIMEOUT_S = 300.0


def _resolve_events_token(request: Request) -> str:
    token = request.query_params.get("token")
    if token:
        return token
    auth = request.headers.get("authorization") or request.headers.get("Authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth.split(" ", 1)[1].strip()
    return ""


@app.get("/research/pro/{session_id}/events")
async def research_pro_events(session_id: str, request: Request) -> StreamingResponse:
    """SSE stream of pro-tier debate events for a session.

    Auth: events_token from `?token=` or `Authorization: Bearer <events_token>`.
    Closes on `event_type=final`, on client disconnect, or after a 300s hard
    timeout. Yields heartbeat comments every 15s so proxies keep the
    connection alive.
    """
    try:
        sid = UUID(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid session_id") from exc

    token = _resolve_events_token(request)
    try:
        verify_token(
            token,
            _settings.events_secret,
            sid,
            expected_prefix=EVENTS_PREFIX,
        )
    except EventsTokenError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    store = SessionStore.from_env()

    async def _generator() -> AsyncIterator[bytes]:
        last_id = 0
        last_heartbeat = asyncio.get_event_loop().time()
        deadline = asyncio.get_event_loop().time() + _SSE_HARD_TIMEOUT_S
        try:
            while True:
                if await request.is_disconnected():
                    return
                if asyncio.get_event_loop().time() >= deadline:
                    yield b'event: error\ndata: {"reason":"timeout"}\n\n'
                    return

                rows = await store.tail_pro_events(sid, after_id=last_id, limit=50)
                for row in rows:
                    last_id = row.id
                    payload: dict[str, Any] = {
                        "seq": row.seq,
                        "type": row.event_type,
                        "agent": row.agent,
                        "content": row.content,
                        "tokens_in": row.tokens_in,
                        "tokens_out": row.tokens_out,
                        "ts": row.ts,
                    }
                    # If this is a final event whose content is a JSON envelope
                    # (failure path emits {"error": ..., "retry_token": ...}),
                    # surface those fields at the top of the payload so the
                    # MCP client can pick up `retry_token` without parsing
                    # `content`. Successful finals leave content as-is.
                    if row.event_type == "final" and isinstance(row.content, str):
                        try:
                            envelope = json.loads(row.content)
                        except (json.JSONDecodeError, ValueError):
                            envelope = None
                        if isinstance(envelope, dict):
                            tok = envelope.get("retry_token")
                            err = envelope.get("error")
                            if isinstance(tok, str) and tok:
                                payload["retry_token"] = tok
                            if isinstance(err, str) and err:
                                payload["error"] = err
                    body = (
                        f"event: turn\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                    ).encode()
                    yield body
                    if row.event_type == "final":
                        return

                now = asyncio.get_event_loop().time()
                if now - last_heartbeat >= _SSE_HEARTBEAT_INTERVAL_S:
                    yield b": ping\n\n"
                    last_heartbeat = now
                await asyncio.sleep(_SSE_POLL_INTERVAL_S)
        except asyncio.CancelledError:
            # Client hung up mid-stream — clean shutdown, no error to log.
            return

    return StreamingResponse(
        _generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # disable nginx buffering for SSE
        },
    )


# ---------------------------------------------------------------------------
# /research/pro/{session_id}/retry — redeem a retry_token issued at the moment
# a Pro session failed. Re-runs the same debate (same idea + project_id) under
# the same session_id, honoring the original x402 payment. Single-use is
# enforced by the status guard: only sessions in `failed` are retryable, and
# the very first action of the retry handler is to flip status to `running` —
# so the second redemption attempt sees a non-failed status and is rejected.
# ---------------------------------------------------------------------------


@app.post("/research/pro/{session_id}/retry", status_code=202)
async def research_pro_retry(
    session_id: str,
    request: Request,
) -> dict[str, Any]:
    """Retry a failed Pro session without re-charging x402.

    Auth: the `retry_token` IS the credential. It was issued (24h TTL) at
    the moment of failure and is bound to this session. No bearer + no x402
    here — possessing the token proves the user paid for the original run.

    Status guard:
        - `failed`     → accept, transition to `running`, kick off a new
                         background debate.
        - `running`    → 409 (a retry is already in flight).
        - `complete`   → 409 (nothing to retry).
        - any other    → 409 (defensive default).
    """
    try:
        sid = UUID(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid session_id") from exc

    token = request.query_params.get("token") or ""
    try:
        verify_token(
            token,
            _settings.events_secret,
            sid,
            expected_prefix=RETRY_PREFIX,
        )
    except EventsTokenError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    store = SessionStore.from_env()
    record = await store.get(sid)
    if record is None:
        raise HTTPException(status_code=404, detail="session not found")

    # Status must be `failed`. Anything else burns the retry attempt.
    if record.status != "failed":
        raise HTTPException(
            status_code=409,
            detail=f"session is not retryable (status={record.status!r})",
        )
    if record.tier != "pro":
        # Sprint 1 scope: retry is Pro-only. Basic retries pay again.
        raise HTTPException(status_code=409, detail="retry is only supported on pro tier")

    # Burn the token: flip status off `failed` BEFORE kicking off the task.
    # A concurrent second-redemption attempt will see a non-failed status
    # and be rejected. We use `pending` (the same status used for a freshly
    # created session) so the existing /result polling treats this as in-
    # flight; the DB CHECK constraint only allows the canonical SessionStatus
    # set, so we cannot introduce a `running` value without a migration.
    await store.update_status(sid, "pending")

    task = asyncio.create_task(
        _run_pro_debate(
            session_id=sid,
            idea=record.idea,
            urls=None,
            project_id=str(record.project_id) if record.project_id else None,
        )
    )
    _BACKGROUND_TASKS.add(task)
    _track_inflight(task, sid)

    # Issue a fresh events_token for the renewed SSE subscription.
    events_token = issue_token(sid, _settings.events_secret)
    return {
        "session_id": str(sid),
        "status": "processing",
        "poll_url": f"/sessions/{sid}/result",
        "events_url": f"/research/pro/{sid}/events",
        "events_token": events_token,
    }


@app.post("/sessions/{session_id}/ask", response_model=AskResult)
async def ask(session_id: str, req: AskRequest) -> AskResult:
    """Free follow-up — first N queries per session, then paid via POST /ask.

    S13-COMMO-02: track per-session call count; once it exceeds
    `ASK_FREE_QUOTA_PER_SESSION` (default 100), return 402 pointing at the
    paid `POST /ask` route. Existing callers under the quota see no behavior
    change. Counter is incremented on every successful free call so the
    paid surface picks up exactly when the quota expires.
    """
    quota = _settings.ask_free_quota_per_session
    store = SessionStore.from_env()
    try:
        sid = UUID(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid session_id") from exc

    # Read current count BEFORE the call so we can refuse over-quota requests
    # without burning an LLM. Best-effort: if economics lookup fails (e.g.
    # session not yet persisted in test mode), fall back to allowing the call.
    try:
        econ = await store.get_economics(sid)
    except Exception:  # pragma: no cover — best-effort
        econ = None
    used = econ.ask_calls_count if econ is not None else 0
    if used >= quota:
        raise HTTPException(
            status_code=402,
            detail={
                "error": "ask_quota_exceeded",
                "message": (
                    f"Free quota of {quota} ask calls exhausted for this session. "
                    "Use POST /ask (paid, $0.01/call) to continue."
                ),
                "paid_route": "POST /ask",
                "ask_calls_count": used,
                "quota": quota,
            },
        )

    try:
        result = await gecko_core.ask(session_id=session_id, question=req.question)
    except NotImplementedError as e:
        raise HTTPException(status_code=501, detail=str(e)) from e

    # Increment the free-quota counter. Best-effort — never fail the user
    # response on bookkeeping.
    try:
        await _bump_ask_count(store, sid)
    except Exception:  # pragma: no cover — best-effort
        logger.warning("ask: could not bump ask_calls_count for %s", sid)

    return result


async def _bump_ask_count(store: SessionStore, session_id: UUID) -> None:
    """Increment ask_calls_count without touching cost columns.

    `gecko_add_session_cost` short-circuits when the amount is 0, so we pass
    a vanishingly small value (rounded to 0 in NUMERIC(12,6)) to bump the
    counter without dirtying the cost line. Service-role only.
    """

    def _update() -> None:
        client = store._client  # internal access — same package boundary.
        client.rpc(
            "gecko_add_session_cost",
            {
                "p_session_id": str(session_id),
                "p_kind": "ask",
                "p_amount_usd": 0.0000001,
            },
        ).execute()

    with contextlib.suppress(Exception):
        await asyncio.to_thread(_update)


# ---------------------------------------------------------------------------
# S13-COMMO-01 — Track E paid advisor voice.
# ---------------------------------------------------------------------------


@app.post("/advise")
async def advise_call(req: AdviseRequest, request: Request) -> dict[str, Any]:
    """S13-COMMO-01 — paid single advisor voice ($0.05).

    Same pipeline as the MCP `gecko_advise` tool / `bb advise` CLI. Paid in
    live mode via x402; in stub mode the route is unregistered and the
    handler is reachable directly without a 402 challenge.
    """
    from uuid import UUID as _UUID

    from gecko_core.orchestration.advisor import (
        AdvisorSessionNotFoundError,
        generate_voice,
    )
    from gecko_core.routing.catalog import Tier as _Tier

    try:
        sid = _UUID(req.session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid session_id") from exc

    try:
        tier = _Tier(req.tier_preset)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"invalid tier_preset: {exc}") from exc

    # Touch the payment payload to record we ran post-settle (live mode).
    _ = getattr(request.state, "payment_payload", None)

    try:
        voice_result = await generate_voice(sid, req.voice, tier_preset=tier)
    except AdvisorSessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("advise: voice generation failed for %s", sid)
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc

    # Bookkeeping: record the per-voice charge so `bb economics` shows it
    # alongside research costs. Best-effort.
    store = SessionStore.from_env()
    try:
        await store.add_cost(sid, "advisor", _route_price_usd("POST /advise"))
    except Exception:  # pragma: no cover — best-effort
        logger.warning("advise: could not record advisor charge for %s", sid)

    payload = voice_result.model_dump(mode="json")
    payload["prepay_usd"] = _route_price_usd("POST /advise")
    return payload


# ---------------------------------------------------------------------------
# S13-COMMO-02 — Track E paid follow-up (post-quota).
# ---------------------------------------------------------------------------


@app.post("/ask")
async def paid_ask_call(req: PaidAskRequest, request: Request) -> dict[str, Any]:
    """S13-COMMO-02 — paid follow-up ($0.01).

    Mirrors the free `POST /sessions/{id}/ask` but with `session_id` in the
    body so Bazaar's bare-UUID path collapse doesn't merge it with the free
    catalog entry. Use this AFTER the per-session free quota is exhausted.
    """
    from uuid import UUID as _UUID

    try:
        sid = _UUID(req.session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid session_id") from exc

    _ = getattr(request.state, "payment_payload", None)

    try:
        result = await gecko_core.ask(session_id=req.session_id, question=req.question)
    except NotImplementedError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("paid_ask: failed for %s", sid)
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc

    store = SessionStore.from_env()
    try:
        await store.add_cost(sid, "ask", _route_price_usd("POST /ask"))
    except Exception:  # pragma: no cover — best-effort
        logger.warning("paid_ask: could not record ask charge for %s", sid)

    return {
        **result.model_dump(mode="json"),
        "prepay_usd": _route_price_usd("POST /ask"),
    }


# ---------------------------------------------------------------------------
# S13-COMMO-03 — Track E paid classify-as-a-service.
# ---------------------------------------------------------------------------


@app.post("/classify")
async def classify_call(req: ClassifyRequest, request: Request) -> dict[str, Any]:
    """S13-COMMO-03 — classify-as-a-service ($0.10).

    Returns the classifier's category list + suggested-source list +
    per-source priority weights. Stays orthogonal to /research: this surface
    sells the *decision* about which sources to hit, not the running of
    those sources. Useful for agents that want to compose Gecko's
    classification with their own retrieval pipeline.
    """
    from gecko_core.classify import classify_idea_with_scores, suggest_sources

    _ = getattr(request.state, "payment_payload", None)

    try:
        categories, scores = await classify_idea_with_scores(req.idea)
    except Exception as exc:
        logger.exception("classify: failed for idea=%r", req.idea[:80])
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc

    suggested, weights = suggest_sources(scores)
    return {
        "categories": categories,
        "scores": {k: round(v, 3) for k, v in scores.items()},
        "suggested_sources": suggested,
        "priority_weights": weights,
        "prepay_usd": _route_price_usd("POST /classify"),
    }


# ---------------------------------------------------------------------------
# S22-MCP-03 — /precedents: flywheel precedent lookup for gecko-mcp clients.
# ---------------------------------------------------------------------------


@app.post("/precedents")
async def precedents_call(req: dict[str, Any], request: Request) -> list[dict[str, Any]]:
    """Return top-K Gecko flywheel precedents for an idea (embedding similarity).

    Free endpoint — no x402 gate. Used by gecko-mcp when GECKO_API_URL is remote
    so the embedding work stays server-side (S22-MCP-03).
    """
    from gecko_core.ingestion.embedder import embed
    from gecko_core.sessions.store import SessionStore

    idea = req.get("idea", "")
    top_k = int(req.get("top_k", 5))
    if not idea:
        raise HTTPException(status_code=400, detail="idea is required")

    try:
        vecs, _tokens = await embed([idea])
        if not vecs:
            return []
        store = SessionStore.from_env()
        rows = await store.retrieve_gecko_precedent(embedding=vecs[0], limit=top_k)
        return [r.model_dump(mode="json") for r in rows]
    except Exception as exc:
        logger.exception("precedents: failed for idea=%r", idea[:80])
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc


# ---------------------------------------------------------------------------
# Phase 8b — POST /trade_research: 7-agent trade research panel.
# Phase 10A — gated by x402 ($0.25 basic / $0.75 pro). Was free in 8b;
# DoS surface (Mongo retrieval + AG2 7-agent debate per call) closed by
# the gate plus a 10/min/IP unauthenticated rate limit.
#
# Companion to /research:
#   - /research          → founder-advisory voices for idea validation
#   - /trade_research    → 7 finance personas for trading decisions
# ---------------------------------------------------------------------------


@app.post(
    "/trade_research",
    response_model=TradeResearchResponse,
    response_model_exclude_none=True,
)
async def trade_research(req: TradeResearchRequest, request: Request) -> TradeResearchResponse:
    """Phase 8b/10A — 7-agent trade research panel ($0.25 basic via x402).

    See ``gecko_trade_research`` MCP tool for the description shipped to
    Claude Code skill manifests. This handler does the retrieval + panel
    invocation in one round trip; payload is JSON in / JSON out, no
    streaming in v1.
    """
    from uuid import uuid4

    from gecko_core.orchestration.settings import get_orchestration_settings
    from gecko_core.orchestration.trade_panel import run_trade_panel_with_retrieval

    _ = getattr(request.state, "payment_payload", None)

    # Issue #12 — diagnostic instrumentation. Per-request trace_id flows
    # into retrieval + panel logs so a single user-reported empty-citations
    # call can be reconstructed end-to-end from the access log.
    trace_id = uuid4().hex[:12]
    request.state.trade_research_trace_id = trace_id
    logger.info(
        "trade_research.handler_entry trace_id=%s tier=%s vertical=%s protocol=%s question_len=%d",
        trace_id,
        req.tier,
        req.vertical,
        req.protocol,
        len(req.idea),
    )

    orch = get_orchestration_settings()
    llm_config: dict[str, Any] = {
        "config_list": [
            {
                "model": "gpt-4o-mini",
                "api_key": orch.llm_api_key,
                "base_url": orch.llm_endpoint,
            }
        ],
        "temperature": 0.3,
    }

    import time as _time

    from gecko_core.observability import emit_event

    _t0 = _time.monotonic()
    try:
        verdict = await run_trade_panel_with_retrieval(
            idea=req.idea,
            protocol=req.protocol,
            vertical=req.vertical,
            tier=req.tier,
            llm_config=llm_config,
        )
    except ValueError:
        # Missing llm_config in production is the expected ValueError; surface
        # as 500 with a friendly one-liner. Detail is redacted — the
        # underlying ValueError can echo env keys in its message.
        logger.exception("trade_research: bad config for protocol=%r", req.protocol)
        raise HTTPException(
            status_code=500,
            detail=(
                "trade_research is misconfigured on the server; "
                "try again in a minute or contact support@geckovision.tech"
            ),
        ) from None
    except Exception as exc:
        # One-line user-facing message; class name only, never the message
        # (it can leak MONGODB_URI, API keys, internal paths).
        logger.exception("trade_research: failed for protocol=%r", req.protocol)
        raise HTTPException(
            status_code=500,
            detail=(
                f"trade_research failed ({type(exc).__name__}) — "
                "retry in a minute; if it persists, run `gecko-mcp doctor`"
            ),
        ) from None

    latency_ms = int((_time.monotonic() - _t0) * 1000)
    _payload = verdict.model_dump()
    # S24 WS-E — populate the settlement receipt from the x402 middleware.
    from gecko_core.payments.receipts import build_receipt

    tx_sig, x402_mode = _extract_settle_receipt(request)
    _payload.update(build_receipt(tx_signature=tx_sig, x402_mode=x402_mode))
    await emit_event(
        "verdict.served",
        {
            "tool": "gecko_trade_research",
            "tier": req.tier,
            "latency_ms": latency_ms,
            "citation_count": len(_payload.get("evidence_citations") or [])
            + len(_payload.get("framework_context") or []),
            "dissent_count": len(_payload.get("dissent") or []),
            "confidence": _payload.get("confidence"),
            "settlement_mode": _payload.get("settlement_mode"),
            "trace_id": trace_id,
        },
    )
    return TradeResearchResponse(**_payload)


@app.post("/trade_research/pro", response_model=TradeResearchResponse)
async def trade_research_pro(req: TradeResearchRequest, request: Request) -> TradeResearchResponse:
    """Phase 10A — pro-tier 7-agent trade research panel ($0.75 via x402).

    Identical handler logic to ``/trade_research``; the price difference
    reflects the higher cost ceiling for the pro debate path. The body's
    ``tier`` field is forced to ``"pro"`` here so callers can't pay the
    pro price for a basic run.
    """
    from uuid import uuid4

    from gecko_core.orchestration.settings import get_orchestration_settings
    from gecko_core.orchestration.trade_panel import run_trade_panel_with_retrieval

    _ = getattr(request.state, "payment_payload", None)

    # Issue #12 — see /trade_research handler above for rationale.
    trace_id = uuid4().hex[:12]
    request.state.trade_research_trace_id = trace_id
    logger.info(
        "trade_research_pro.handler_entry trace_id=%s tier=pro vertical=%s "
        "protocol=%s question_len=%d",
        trace_id,
        req.vertical,
        req.protocol,
        len(req.idea),
    )

    orch = get_orchestration_settings()
    llm_config: dict[str, Any] = {
        "config_list": [
            {
                "model": "gpt-4o-mini",
                "api_key": orch.llm_api_key,
                "base_url": orch.llm_endpoint,
            }
        ],
        "temperature": 0.3,
    }

    import time as _time

    from gecko_core.observability import emit_event

    _t0 = _time.monotonic()
    try:
        # Issue #14 — pro tier MUST add evidence the basic envelope doesn't
        # carry, otherwise buyers can't justify paying 3x. enable_backtest
        # turns on the Phase 9 CoinGecko OHLCV replay; the backtest payload
        # is then surfaced on the wire below. Backtest never raises (the
        # core wrapper catches and returns the verdict unchanged), so a
        # CoinGecko outage degrades gracefully to verdict-only.
        verdict = await run_trade_panel_with_retrieval(
            idea=req.idea,
            protocol=req.protocol,
            vertical=req.vertical,
            tier="pro",
            llm_config=llm_config,
            enable_backtest=True,
        )
    except ValueError:
        logger.exception("trade_research_pro: bad config for protocol=%r", req.protocol)
        raise HTTPException(
            status_code=500,
            detail=(
                "trade_research is misconfigured on the server; "
                "try again in a minute or contact support@geckovision.tech"
            ),
        ) from None
    except Exception as exc:
        logger.exception("trade_research_pro: failed for protocol=%r", req.protocol)
        raise HTTPException(
            status_code=500,
            detail=(
                f"trade_research failed ({type(exc).__name__}) — "
                "retry in a minute; if it persists, run `gecko-mcp doctor`"
            ),
        ) from None

    # Issue #14 — guarantee a non-null `backtest` on the pro wire envelope
    # even when the upstream wrapper swallowed an exception or the
    # strategist turn never produced a parseable intent. The
    # ``unbacktestable=True`` shape is the documented graceful-degradation
    # contract; surfacing it on the wire is what makes the pro tier
    # observably distinct from basic.
    payload = verdict.model_dump()
    if payload.get("backtest") is None:
        payload["backtest"] = {
            "pnl_pct": 0.0,
            "drawdown_pct": 0.0,
            "n_similar_setups": 0,
            "hit_rate": 0.0,
            "source": "coingecko",
            "unbacktestable": True,
            "reason": "no_strategist_intent",
        }
    latency_ms = int((_time.monotonic() - _t0) * 1000)
    # S24 WS-E — populate the settlement receipt from the x402 middleware.
    from gecko_core.payments.receipts import build_receipt

    tx_sig, x402_mode = _extract_settle_receipt(request)
    payload.update(build_receipt(tx_signature=tx_sig, x402_mode=x402_mode))
    await emit_event(
        "verdict.served",
        {
            "tool": "gecko_trade_research_pro",
            "tier": "pro",
            "latency_ms": latency_ms,
            "citation_count": len(payload.get("evidence_citations") or [])
            + len(payload.get("framework_context") or []),
            "dissent_count": len(payload.get("dissent") or []),
            "confidence": payload.get("confidence"),
            "settlement_mode": payload.get("settlement_mode"),
            "trace_id": trace_id,
        },
    )
    return TradeResearchResponse(**payload)


# S5-API-03: tiered /route pricing. Each path is gated by x402 at a
# different price; the handler logic is identical except for the
# `tier_charged` field surfaced on the response. Premium / upgrade tier
# enforcement is best-effort: we trust the client to have paid the right
# tier, since x402 already validated the payment matches the route.
_PLAN_TIER_PRESETS = ("quality", "balanced", "budget", "free")


_RouteHandler = Callable[[RouteRequest, Request], Awaitable[dict[str, Any]]]


def _route_handler_factory(tier: str, advertised_route: str) -> _RouteHandler:
    """Build the /route handler for a given tier.

    Each tier is a separate FastAPI endpoint so x402 can charge a distinct
    price per route. The handler logic is identical across tiers except
    for the `tier_charged` + `prepay_usd` fields on the result.
    """

    async def _handler(req: RouteRequest, request: Request) -> dict[str, Any]:
        from gecko_core.routing import route as core_route
        from gecko_core.routing.matrix import ROUTING_MATRIX

        if req.task_hint not in ROUTING_MATRIX:
            raise HTTPException(
                status_code=400,
                detail=f"task_hint must be one of {sorted(ROUTING_MATRIX)}",
            )

        # Touch the request so unused-arg lint is happy in any future refactor
        # that needs the payment payload (e.g. attaching tx_signature to logs).
        _ = getattr(request.state, "payment_payload", None)

        try:
            result = await core_route(
                req.prompt,
                task_hint=req.task_hint,
                max_cost_usd=req.max_cost_usd,
                prefer_premium=req.prefer_premium,
            )
        except Exception as exc:
            # Surface a clean 500; the x402 settle has already happened so we
            # don't try to refund — just log and tell the caller.
            logger.exception("route_call failed (tier=%s)", tier)
            raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc
        payload = result.model_dump(mode="json")
        # S5-API-03: surface which tier the caller actually paid so MCP /
        # CLI demos can render "you paid $0.05 (premium tier)" without
        # second-guessing the catalog.
        payload["tier_charged"] = tier
        payload["prepay_usd"] = _route_price_usd(advertised_route)
        return payload

    return _handler


@app.post("/route")
async def route_call(req: RouteRequest, request: Request) -> dict[str, Any]:
    """S4-ROUTE-02 / S5-API-03 — paid LLM router (default tier).

    Pricing tradeoff: x402 settles before we know the upstream-billed truth
    (`usage.cost`). A true post-call markup (`usage_cost_usd * 1.10`) would
    require x402 to support a settle-after-work refund hook; v2 doesn't.
    Sprint 5 ships tiered flat charges instead — three paths, three prices,
    client picks the path matching its task_hint:

        POST /route           → $0.01 (extraction / summary / default)
        POST /route/premium   → $0.05 (reasoning / code)
        POST /route/upgrade   → $0.20 (prefer_premium=True)

    Heavy callers subsidize light ones less than the Sprint 4 single-flat
    model, but the implementation stays predictable and auditable.

    Auth: payment is enforced by the x402 middleware; this handler runs only
    after settle. No per-user identity is required — `route` is session-less
    by design.
    """
    handler = _route_handler_factory("default", "POST /route")
    return await handler(req, request)


@app.post("/route/premium")
async def route_call_premium(req: RouteRequest, request: Request) -> dict[str, Any]:
    """S5-API-03 — premium-tier paid LLM router. $0.05 flat.

    Use this path for `task_hint=reasoning` or `task_hint=code`. The handler
    is identical to `POST /route`; the price difference reflects the higher
    expected upstream cost on these tasks.
    """
    handler = _route_handler_factory("premium", "POST /route/premium")
    return await handler(req, request)


@app.post("/route/upgrade")
async def route_call_upgrade(req: RouteRequest, request: Request) -> dict[str, Any]:
    """S5-API-03 — upgrade-tier paid LLM router. $0.20 flat.

    Use this path when `prefer_premium=True` to ensure x402 collects enough
    to cover routing through the premium catalog column.
    """
    handler = _route_handler_factory("upgrade", "POST /route/upgrade")
    return await handler(req, request)


@app.post("/plan")
async def plan_call(req: PlanRequest, request: Request) -> dict[str, Any]:
    """S5-API-01 — paid Advisor Panel surface ($0.25 flat).

    Mirrors the /research and /route patterns: x402-gated only, no
    redundant `verify_frames_token` (matches Sprint 4's deviation note).
    On 200 returns the AdvisorPanel JSON. The body's optional
    `frames_username` is recorded for audit; `project_id` lets the
    economics view roll the spend into the project budget.
    """
    from uuid import UUID as _UUID

    from gecko_core.orchestration.advisor import (
        AdvisorSessionNotFoundError,
        generate_panel,
    )
    from gecko_core.routing.catalog import Tier as _Tier

    _get_cost_breaker().record_spend(_ROUTE_SPEND_ESTIMATE_USD["POST /plan"])

    if req.tier_preset not in _PLAN_TIER_PRESETS:
        raise HTTPException(
            status_code=400,
            detail=f"tier_preset must be one of {list(_PLAN_TIER_PRESETS)}",
        )
    try:
        sid = _UUID(req.session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid session_id") from exc

    # Touch the payment payload to record we ran post-settle.
    _ = getattr(request.state, "payment_payload", None)

    # Persist the advertised price on the session so the economics view
    # picks up the panel charge alongside research costs. Best-effort —
    # never fail the user-visible request because of bookkeeping.
    store = SessionStore.from_env()
    try:
        await store.set_price(sid, _route_price_usd("POST /plan"))
    except Exception:  # pragma: no cover — best-effort bookkeeping
        logger.warning("plan: could not persist price marker for %s", sid)

    try:
        panel = await generate_panel(sid, tier_preset=_Tier(req.tier_preset))
    except AdvisorSessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        # x402 already settled — surface a clean 500 rather than retrying.
        logger.exception("plan: panel generation failed for %s", sid)
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc

    # Project rollup: add $0.25 to projects.spent_usd if the session is
    # bound to one. Same best-effort pattern as /research.
    if req.project_id:
        try:
            project_uuid = UUID(req.project_id)
        except ValueError:
            project_uuid = None
        if project_uuid is not None:
            try:
                await increment_project_spent_safe(
                    store=store,
                    project_id=project_uuid,
                    delta_usd=Decimal(str(_route_price_usd("POST /plan"))),
                )
            except Exception:  # pragma: no cover — best-effort
                logger.warning("plan: project spend increment failed")

    # S23-REPORT-01 — persist the panel into result_json so gecko_report
    # can reconstruct it without re-running generate_panel. Best-effort:
    # a persistence failure must never break the user-visible response.
    try:
        from gecko_core.persistence import update_result_payload

        existing = await store.get_result(sid)
        if existing is not None:
            existing["advisor_panel"] = panel.model_dump(mode="json")
            await update_result_payload(sid, existing, store=store)
    except Exception:  # pragma: no cover — best-effort
        logger.warning("plan: could not persist advisor_panel for %s", sid)

    return panel.model_dump(mode="json")


# S23-REPORT-01 — in-memory report cache keyed by verdict_hash. Idempotent
# re-renders of the same session are free. Simple dict is sufficient for V1
# (single process; bounded by the number of unique verdicts served).
_REPORT_CACHE: dict[str, str] = {}


@app.post("/report/{session_id}")
async def get_report(
    session_id: str,
    request: Request,
    format: str = "html",
) -> Any:
    """S23-REPORT-01 — generate a formatted report for a completed session.

    x402-gated at $0.05. Cached by verdict_hash so idempotent re-renders
    are free server-side. Returns text/html for format=html; JSON
    ``{"markdown": "..."}`` for format=markdown.
    """
    from uuid import UUID as _UUID

    from gecko_core.models import ResearchResult as _ResearchResult
    from gecko_core.orchestration.advisor.models import AdvisorPanel as _AdvisorPanel
    from gecko_core.reports.html import render_html_report, render_markdown_report
    from starlette.responses import Response

    try:
        sid = _UUID(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid session_id") from exc

    # Touch payment payload (set by middleware on settled requests).
    _ = getattr(request.state, "payment_payload", None)

    store = SessionStore.from_env()
    raw = await store.get_result(sid)
    if raw is None:
        raise HTTPException(status_code=404, detail="session not found or result not ready")

    try:
        result = _ResearchResult.model_validate(raw)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"result validation failed: {exc}") from exc

    # Cache key: verdict_hash when present, else session_id + format.
    cache_key = (result.verdict_hash or session_id) + f":{format}"
    if cache_key in _REPORT_CACHE:
        cached = _REPORT_CACHE[cache_key]
        if format == "markdown":
            return {"markdown": cached}
        return Response(content=cached, media_type="text/html")

    # Reconstruct the AdvisorPanel if one was persisted.
    panel: _AdvisorPanel | None = None
    if isinstance(raw.get("advisor_panel"), dict):
        try:
            panel = _AdvisorPanel.model_validate(raw["advisor_panel"])
        except Exception:
            logger.warning("report: advisor_panel validation failed for %s", session_id)

    if format == "markdown":
        md = render_markdown_report(result, plan=panel, asks=None)
        _REPORT_CACHE[cache_key] = md
        return {"markdown": md}

    html_content = render_html_report(result, plan=panel, asks=None)
    _REPORT_CACHE[cache_key] = html_content
    return Response(content=html_content, media_type="text/html")


@app.post("/scaffold")
async def scaffold_call(req: ScaffoldRequest, request: Request) -> dict[str, Any]:
    """S8-API-01 — paid scaffold surface, mirrors the MCP ``gecko_scaffold``.

    Charges $0.05 in live mode (matches the MCP pricing); free in stub.
    Errors collapse to clean HTTPExceptions instead of the MCP tool's
    ``{error, message}`` envelope so HTTP callers can branch on status code.
    """
    from pathlib import Path
    from uuid import UUID as _UUID

    from gecko_core.orchestration.scaffold import (
        KillVerdictError,
        ScaffoldError,
        SessionNotFoundError,
        SessionNotReadyError,
        generate_scaffold,
    )

    try:
        sid = _UUID(req.session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid session_id") from exc

    # Touch the payment payload to record we ran post-settle.
    _ = getattr(request.state, "payment_payload", None)

    try:
        result = await generate_scaffold(sid, Path.cwd())
    except KillVerdictError as exc:
        # The session's verdict was 'kill'; the MCP tool surfaces this as a
        # soft error. HTTP semantics: 409 Conflict — the resource is in a
        # state that forbids scaffolding.
        raise HTTPException(status_code=409, detail=f"kill_verdict: {exc}") from exc
    except SessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SessionNotReadyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ScaffoldError as exc:
        # x402 already settled — surface a clean 500 rather than retrying.
        logger.exception("scaffold: generation failed for %s", sid)
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc

    return {
        "session_id": str(result.session_id),
        "paths": [str(p) for p in result.paths],
        "tokens_used": result.tokens_used,
        "summary": result.summary,
    }


@app.post("/pulse")
async def pulse_call(req: PulseRequest, request: Request) -> dict[str, Any]:
    """S8-API-01 / S14-PULSE-01 — re-run the Advisor Panel and surface deltas.

    Free by default in all modes (stub + live) until the team commits to a
    pulse pricing model. When PULSE_CALL_PRICE is set to a non-zero value,
    the route auto-registers on the x402 middleware (see ``_build_routes``)
    and the same handler runs after settle.
    """
    from uuid import UUID as _UUID

    from gecko_core.orchestration.advisor import (
        AdvisorSessionNotFoundError,
        run_pulse,
        run_pulse_v14,
    )
    from gecko_core.payments.pulse_credits import (
        check_pulse_credits,
        decrement_pulse_credit,
    )
    from gecko_core.routing.catalog import Tier as _Tier

    if not req.session_id and not req.project_id:
        raise HTTPException(
            status_code=400,
            detail="either session_id or project_id is required",
        )

    sid: UUID | None = None
    pid: UUID | None = None
    if req.session_id:
        try:
            sid = _UUID(req.session_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid session_id") from exc
    if req.project_id:
        try:
            pid = _UUID(req.project_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid project_id") from exc

    # Touch the payment payload (set by middleware when /pulse is gated).
    _ = getattr(request.state, "payment_payload", None)

    try:
        tier = _Tier(req.tier_preset)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"invalid tier_preset: {exc}") from exc

    # S14-PULSE-02 — credits-first dispatch. When the wallet has prepaid
    # credits, decrement one and surface the new balance on the response.
    credits_after: int | None = None
    if req.user_wallet:
        try:
            current = await check_pulse_credits(user_wallet=req.user_wallet)
            if current > 0:
                credits_after = await decrement_pulse_credit(user_wallet=req.user_wallet)
        except Exception:
            logger.debug("pulse: credit check failed; falling through")

    try:
        if sid is not None:
            v14 = await run_pulse_v14(parent_session_id=sid, tier_preset=tier)
            if credits_after is not None:
                v14 = v14.model_copy(update={"credits_remaining_after": credits_after})
            return v14.model_dump(mode="json")
        # project_id-only legacy path.
        result = await run_pulse(session_id=None, project_id=pid, tier_preset=tier)
    except AdvisorSessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("pulse: run failed")
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc

    return result.model_dump(mode="json")


@app.post("/review")
async def review_call(req: ReviewRequest, request: Request) -> dict[str, Any]:
    """S7-DOGFOOD-02 — sprint review meta-tool.

    Free in stub mode (no LLM call). In live mode the route is registered
    on the x402 middleware at $0.10 and a single LLM call synthesizes the
    structured bullets. Auto-journals as `sprint_reviewed` (best-effort).
    """
    from uuid import UUID as _UUID

    from gecko_core.review import build_review

    if req.tier_preset not in _PLAN_TIER_PRESETS:
        raise HTTPException(
            status_code=400,
            detail=f"tier_preset must be one of {list(_PLAN_TIER_PRESETS)}",
        )

    project_uuid: UUID | None = None
    if req.project_id:
        try:
            project_uuid = _UUID(req.project_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid project_id") from exc

    # Touch the payment payload (set by middleware in live mode).
    _ = getattr(request.state, "payment_payload", None)

    # Live mode wires a one-shot LLM caller; stub mode uses the deterministic
    # path so dogfood loops in CI run free.
    llm_caller = None
    if _settings.x402_mode != "stub":
        llm_caller = _build_review_llm_caller(req.tier_preset)

    review = await build_review(
        project_id=str(project_uuid) if project_uuid else None,
        since_days=req.since_days,
        llm_caller=llm_caller,
        tier_preset=req.tier_preset,
    )

    # Auto-journal sprint_reviewed. Best-effort.
    if project_uuid is not None:
        try:
            from gecko_core.memory.auto_journal import journal_sprint_review

            await journal_sprint_review(
                project_id=project_uuid,
                since_days=review.since_days,
                shipped=review.shipped,
                weakest_link=review.weakest_link,
                proposed_next=review.proposed_next,
                mode=review.mode,
            )
        except Exception:  # pragma: no cover — best-effort
            logger.warning("review: sprint_reviewed journal failed", exc_info=True)

    return review.model_dump(mode="json")


def _build_review_llm_caller(tier_preset: str) -> Any:
    """Async LLM caller using the same router stack as gecko_advise.

    One chat completion per call. Returns the raw text content; the review
    builder parses JSON itself.
    """
    from gecko_core.orchestration.settings import get_orchestration_settings
    from gecko_core.routing.catalog import (
        AgentRole,
        lookup_model,
        task_for_role,
    )
    from gecko_core.routing.catalog import (
        Tier as _Tier,
    )
    from openai import AsyncOpenAI

    try:
        tier = _Tier(tier_preset)
    except ValueError:
        tier = _Tier.balanced
    task = task_for_role(AgentRole.staff_manager)
    model_entry = lookup_model(task, tier)
    orch = get_orchestration_settings()
    client = AsyncOpenAI(api_key=orch.llm_api_key, base_url=orch.llm_endpoint)

    async def _call(system_prompt: str, user_prompt: str) -> str:
        resp = await client.chat.completions.create(
            model=model_entry.id,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.3,
            response_format={"type": "json_object"},
        )
        return resp.choices[0].message.content or ""

    return _call


@app.get("/sessions/{session_id}/sources", response_model=list[SourceInfo])
async def sources(session_id: str) -> list[SourceInfo]:
    """Free — list all indexed sources for a session."""
    try:
        return await gecko_core.sources(session_id=session_id)
    except NotImplementedError as e:
        raise HTTPException(status_code=501, detail=str(e)) from e


@app.get("/sessions/{session_id}/result", response_model=ResearchResult)
async def session_result(session_id: str) -> Any:
    """Poll for the async ResearchResult.

    Polling contract (this is the public contract for the CLI, the web
    app, and any third-party consumer):

      * Clients poll this endpoint after `POST /research` returns
        `{"status": "processing", "poll_url": ...}`.
      * The recommended retry interval is **3 seconds**. The CLI uses
        3s; the body also surfaces `retry_after_seconds` as a hint when
        we've decided a longer back-off is appropriate.
      * The 425 response IS the documented "still working" signal — it
        is NOT an error. A typical 60-second basic-tier session will
        emit roughly 20 successive 425 responses before flipping to
        200. Operators: those 425s are downgraded to DEBUG in the
        access log by `gecko_api.access_log_filter`; if you're seeing
        them flood INFO logs, that filter isn't installed.
      * Stop polling on any of: 200, 4xx, 5xx, 410.

    Status code semantics:
        - 200 + ResearchResult JSON  : workflow complete (terminal)
        - 425 Too Early              : still processing — retry in
                                       ~`retry_after_seconds` seconds
        - 500 + {status, error}      : workflow raised — terminal
        - 410 Gone + {detail}        : session was killed by a
                                       container shutdown mid-flight
                                       (ECS rolling deploy / autoscale).
                                       Re-submit the original request
                                       to start a fresh session.
        - 404                        : session not found / soft-deleted
        - 400                        : session_id is not a UUID
    """
    try:
        sid = UUID(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid session_id") from exc

    store = SessionStore.from_env()
    record = await store.get(sid)
    if record is None:
        raise HTTPException(status_code=404, detail="session not found")

    if record.status == "failed":
        msg = await store.get_error(sid)
        raise HTTPException(
            status_code=500,
            detail={"status": "failed", "error": msg or "unknown failure"},
        )

    if record.status == "interrupted":
        # 2026-05-06 — the container that owned this session was drained
        # mid-flight. Don't keep the client polling indefinitely; signal
        # 410 Gone so they re-submit. Distinct from 500 (workflow bug)
        # so the bridge / CLI can attach the right user-facing copy.
        raise HTTPException(
            status_code=410,
            detail={
                "status": "interrupted",
                "detail": "session interrupted by container restart; please retry",
            },
        )

    result = await store.get_result(sid)
    if result is None:
        # Still processing — let the client retry. 425 "Too Early" is the
        # cleanest fit; some HTTP clients balk at non-standard codes, so
        # include a retry hint in the body too.
        raise HTTPException(
            status_code=425,
            detail={"status": record.status, "retry_after_seconds": 5},
        )

    return result


@app.get("/sessions/{session_id}/economics")
async def session_economics(session_id: str) -> dict[str, Any]:
    """Per-session unit economics: price charged vs real costs incurred.

    Free read — surfaces what's already on the `sessions` row (price_usd,
    cost_*_usd, generated cost_total_usd and margin_usd, plus the x402 tx
    signature). Useful for the demo dashboard and for pre-mainnet pricing
    decisions on devnet.
    """
    try:
        sid = UUID(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid session_id") from exc
    store = SessionStore.from_env()
    econ = await store.get_economics(sid)
    if econ is None:
        raise HTTPException(status_code=404, detail="session not found")
    payload = econ.model_dump(mode="json")
    return payload


@app.get("/sessions/spent-by-project/{project_id}")
async def sessions_spent_by_project(project_id: str) -> dict[str, Any]:
    """Free — total spend + session count for a project.

    Used by the gecko-mcp api_client as a pre-flight budget check before
    paying. Best-effort guarantee: this is *not* a hard ceiling in v1
    (frames.ag policy is per-wallet, not per-project). v2 replaces this
    with on-chain isolation via project-scoped Privy wallets.
    """
    try:
        pid = UUID(project_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid project_id") from exc
    store = SessionStore.from_env()
    spent, count = await store._project_spend(pid)
    return {
        "project_id": str(pid),
        "total_spent_usd": spent,
        "sessions_count": count,
    }


# ---------------------------------------------------------------------------
# /projects — bearer-authenticated CRUD for per-user project envelopes.
#
# All four endpoints require frames.ag bearer auth (verify_frames_token).
# Username is derived server-side from the verified token; the client never
# declares its own identity. Rate-limited at 60/min per Authorization header.
# ---------------------------------------------------------------------------


class ProjectCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    budget_usd: float | None = Field(default=None, ge=0)


class ProjectOut(BaseModel):
    project_id: str
    name: str
    budget_usd: float | None = None
    wallet_address: str | None = None
    wallet_provider: str | None = None
    created_at: str | None = None


def _project_row_to_out(row: dict[str, Any]) -> ProjectOut:
    return ProjectOut(
        project_id=str(row["id"]),
        name=str(row.get("name", "")),
        budget_usd=_float_or_none(row.get("budget_usd")),
        wallet_address=row.get("wallet_address"),
        wallet_provider=row.get("wallet_provider"),
        created_at=str(row["created_at"]) if row.get("created_at") is not None else None,
    )


def _float_or_none(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


@app.post("/projects", status_code=201)
@limiter.limit("60/minute")
async def create_project(
    request: Request,
    body: ProjectCreateRequest,
    username: str = Depends(verify_frames_token),
) -> dict[str, Any]:
    store = SessionStore.from_env()
    try:
        project_id = await store.create_project(
            username=username,
            name=body.name,
            budget_usd=body.budget_usd,
        )
    except Exception as exc:
        # Likely (frames_username, name) unique-constraint violation → 409.
        msg = str(exc).lower()
        if "duplicate" in msg or "unique" in msg or "23505" in msg:
            raise HTTPException(status_code=409, detail="project name already exists") from exc
        logger.exception("create_project failed for %s/%s", username, body.name)
        raise HTTPException(status_code=500, detail="could not create project") from exc

    record = await store.get_project(username=username, name=body.name)
    if record is None:
        # Should not happen — the insert just succeeded.
        return {
            "project_id": str(project_id),
            "name": body.name,
            "budget_usd": body.budget_usd,
            "wallet_address": None,
            "wallet_provider": "frames-policy",
            "created_at": None,
        }

    # S2-05: eagerly provision a Privy wallet at project creation time when
    # configured. ensure_project_wallet is idempotent and a soft-skip on
    # sentinel creds, so devnet without Privy stays a no-op here.
    try:
        wallet_snapshot = await ensure_project_wallet(
            settings=_settings,
            store=store,
            project_id=project_id,
        )
    except Exception:  # pragma: no cover — best-effort
        logger.exception("create_project: ensure_project_wallet failed")
        wallet_snapshot = None

    out = _project_row_to_out(record).model_dump()
    if wallet_snapshot and wallet_snapshot.privy_wallet_address:
        out["wallet_address"] = wallet_snapshot.privy_wallet_address
        out["wallet_provider"] = "privy-direct"
    return out


@app.get("/projects")
@limiter.limit("60/minute")
async def list_projects(
    request: Request,
    username: str = Depends(verify_frames_token),
) -> list[dict[str, Any]]:
    store = SessionStore.from_env()
    rows = await store.list_projects(username=username)
    out: list[dict[str, Any]] = []
    for row in rows:
        try:
            pid = UUID(str(row["id"]))
        except (KeyError, ValueError):
            continue
        spent, count = await store._project_spend(pid)
        item = _project_row_to_out(row).model_dump()
        item["total_spent_usd"] = spent
        item["sessions_count"] = count
        out.append(item)
    return out


@app.get("/projects/{name}")
@limiter.limit("60/minute")
async def get_project(
    name: str,
    request: Request,
    username: str = Depends(verify_frames_token),
) -> dict[str, Any]:
    store = SessionStore.from_env()
    record = await store.get_project(username=username, name=name)
    if record is None:
        raise HTTPException(status_code=404, detail="project not found")
    pid = UUID(str(record["id"]))
    spent = await store.project_total_spent(pid)
    remaining = await store.project_budget_remaining(pid)
    sessions = await store.list_project_sessions(pid, limit=5)
    payload = _project_row_to_out(record).model_dump()
    payload["total_spent_usd"] = spent
    payload["budget_remaining_usd"] = remaining
    payload["sessions"] = [
        {
            "id": str(s.get("id", "")),
            "idea": s.get("idea"),
            "status": s.get("status"),
            "cost_total_usd": _float_or_none(s.get("cost_total_usd")) or 0.0,
            "created_at": str(s["created_at"]) if s.get("created_at") is not None else None,
        }
        for s in sessions
    ]
    return payload


@app.get("/projects/{project_id}/economics")
@limiter.limit("60/minute")
async def get_project_economics(
    project_id: str,
    request: Request,
    username: str = Depends(verify_frames_token),
) -> dict[str, Any]:
    """Project-scoped economics view consumed by `gecko-mcp project <id>`
    and the V3 dashboard. Returns wallet info, budget rollup, and the 5
    most recent paid sessions. 404 on missing/wrong-owner project (no
    existence leak)."""
    try:
        pid = UUID(project_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="project not found") from None
    store = SessionStore.from_env()
    record = await store.get_project_by_id_for_user(username=username, project_id=pid)
    if record is None:
        raise HTTPException(status_code=404, detail="project not found")
    sessions = await store.list_project_paid_sessions(pid, limit=5)

    privy_wallet_id = record.get("privy_wallet_id")
    privy_wallet_address = record.get("privy_wallet_address")
    cap = float(record.get("budget_cap_usd") or 0.0)
    spent = float(record.get("spent_usd") or 0.0)
    remaining = max(0.0, cap - spent)

    return {
        "project_id": str(record["id"]),
        "name": record.get("name"),
        "wallet": {
            "privy_wallet_id": privy_wallet_id,
            "privy_wallet_address": privy_wallet_address,
            # On-chain USDC balance read deferred to a follow-up — Privy v2
            # doesn't surface SPL token balances cleanly and the Solana RPC
            # plumbing is shared with the x402 settlement path; wiring it up
            # without a cache risks hammering the RPC on dashboard polls.
            "balance_usdc": None,
        },
        "budget": {
            "cap_usd": f"{cap:.2f}",
            "spent_usd": f"{spent:.2f}",
            "remaining_usd": f"{remaining:.2f}",
        },
        "recent_sessions": [
            {
                "session_id": str(s.get("id", "")),
                "tier": s.get("tier"),
                "cost_usd": _float_or_none(s.get("cost_total_usd")) or 0.0,
                "paid_usd": _float_or_none(s.get("price_usd")) or 0.0,
                "x402_tx_signature": s.get("x402_tx_signature"),
                "network": s.get("network"),
                "created_at": str(s["created_at"]) if s.get("created_at") is not None else None,
            }
            for s in sessions
        ],
    }


@app.delete("/projects/{name}", status_code=204)
@limiter.limit("60/minute")
async def delete_project(
    name: str,
    request: Request,
    username: str = Depends(verify_frames_token),
) -> None:
    store = SessionStore.from_env()
    deleted = await store.delete_project(username=username, name=name)
    if not deleted:
        raise HTTPException(status_code=404, detail="project not found")
    return None


class MemoryQueryRequest(BaseModel):
    """S6-MINE-01 — structured filter over the memory layer."""

    scope_type: str = Field(..., pattern="^(project|session|user)$")
    scope_id: str = Field(..., min_length=1)
    entry_type: str | None = None
    since: str | None = None
    k: int = Field(default=20, ge=1, le=100)
    query: str | None = None


@app.post("/memory/query")
async def memory_query(req: MemoryQueryRequest) -> list[dict[str, Any]]:
    """Free — query memory by `(scope, entry_type, since, k)`.

    When `query` is supplied, returns top-k cosine matches; otherwise
    returns the chronological list (newest first). Mirrors the
    `gecko_memory_query` MCP tool — single source of truth lives in
    gecko_core.memory.
    """
    from datetime import datetime

    from gecko_core.memory import MemoryEntryType, MemoryScope, recall, search

    et: MemoryEntryType | None = None
    if req.entry_type:
        try:
            et = MemoryEntryType(req.entry_type)
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail=f"unknown entry_type {req.entry_type!r}"
            ) from exc
    since_dt: datetime | None = None
    if req.since:
        try:
            since_dt = datetime.fromisoformat(req.since.replace("Z", "+00:00"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="since must be ISO-8601") from exc
    scope = MemoryScope(type=req.scope_type, id=req.scope_id)  # type: ignore[arg-type]

    if req.query and req.query.strip():
        matches = await search(scope, req.query, top_k=req.k)
        out: list[dict[str, Any]] = []
        for entry, sim in matches:
            if et is not None and entry.entry_type != et:
                continue
            if since_dt is not None and entry.created_at < since_dt:
                continue
            out.append(
                {
                    "id": str(entry.id),
                    "entry_type": entry.entry_type.value,
                    "key": entry.key,
                    "value": entry.value,
                    "similarity": sim,
                    "created_at": entry.created_at.isoformat(),
                }
            )
        return out

    rows = await recall(scope, entry_type=et, limit=req.k, since=since_dt)
    return [
        {
            "id": str(r.id),
            "entry_type": r.entry_type.value,
            "key": r.key,
            "value": r.value,
            "tx_signature": r.tx_signature,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]


@app.get("/pricing")
async def pricing() -> dict[str, Any]:
    """S6-TIER-01 — informational ladder of `{tier_preset: TierQuote}` per
    endpoint. Driven by the curated catalog + the same x402 prices the
    middleware enforces. No payment, no auth — surface for `bb pricing`
    and the V2 web app.
    """
    from gecko_core.routing.pricing import build_pricing_table

    prices_by_endpoint = {
        "research": _route_price_usd("POST /research"),
        "plan": _route_price_usd("POST /plan"),
        "route": _route_price_usd("POST /route"),
    }
    return {
        "endpoints": build_pricing_table(prices_by_endpoint),
        "tiers": ["quality", "balanced", "budget", "free"],
    }


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "payments": _settings.x402_mode}


@app.post("/events", status_code=202)
@limiter.limit("30/minute")
async def record_telemetry_event(
    request: Request,
    body: TelemetryEventRequest,
) -> dict[str, bool]:
    """S33-#73 — record a top-of-funnel telemetry event.

    Unauthenticated and NOT x402-gated: it must fire before a wallet exists
    (install_started/install_error come from the skill installer itself).
    Thin transport — parse, call the gecko-core helper, return 202. Never
    echoes stored data back. Telemetry failures are swallowed in the store
    layer, so this always returns 202 once the body validates.
    """
    from gecko_core.telemetry import record_event

    await record_event(
        body.event_type,
        wallet_address=body.wallet_address,
        email=body.email,
        installer_tag=body.installer_tag,
        metadata=body.metadata,
    )
    return {"ok": True}


@app.get("/metrics/telemetry", include_in_schema=False)
@limiter.limit("10/minute")
async def telemetry_metrics(request: Request) -> dict[str, Any]:
    """Internal install-funnel rollup — counts only, never email values.

    Undocumented (`include_in_schema=False`) so it stays off the public
    OpenAPI contract. Exposes aggregate counts + an emails COUNT; no PII
    values ever leave this handler. If this needs a wider audience, gate it
    behind x402 or the frames token before documenting it.
    """
    from gecko_core.telemetry import telemetry_summary

    return await telemetry_summary()


@app.get("/robots.txt")
async def robots_txt() -> Response:
    """Disallow all crawlers. The API surface is for agents via x402, not
    indexers — this just quiets the scanner 404 noise in the access log."""
    return Response(content="User-agent: *\nDisallow: /\n", media_type="text/plain")


@app.get("/metrics")
async def metrics_endpoint() -> dict[str, Any]:
    """S24 WS-D — read-only aggregate of the global events ledger.

    No auth on purpose at v0.1 — we IP-allowlist in WS-F. Counts are
    served from the ``gecko_events.events`` collection; when Mongo is
    unset (local dev) the endpoint still returns a valid empty shell so
    callers can wire dashboards before infra lands.
    """
    from gecko_core.observability import aggregate_metrics

    return await aggregate_metrics()


@app.get("/.well-known/x402")
async def well_known_x402() -> dict[str, Any]:
    """x402 discovery endpoint.

    Returns the route catalog (price, network, payTo, scheme) so an agent
    can introspect what's payable here without first eating a 402.
    """
    catalog: list[dict[str, Any]] = []
    for pattern, route in _routes_config.items():
        accepts = route.accepts if isinstance(route.accepts, list) else [route.accepts]
        entry: dict[str, Any] = {
            "route": pattern,
            "description": route.description,
            "accepts": [
                {
                    "scheme": opt.scheme,
                    "network": opt.network,
                    "price": opt.price,
                    "payTo": opt.pay_to,
                    "maxTimeoutSeconds": opt.max_timeout_seconds,
                }
                for opt in accepts
            ],
        }
        # S12-BAZAAR-01: surface the CDP Bazaar discoveryExtension blob
        # so a payer-side client can fetch the input/output schema before
        # paying, and so the CDP facilitator (Track A, separate ticket)
        # can attach it to settle-time metadata.
        ext = BAZAAR_EXTENSIONS.get(pattern)
        if ext is not None:
            entry["bazaarExtension"] = extension_as_dict(ext)
        catalog.append(entry)
    return {
        "x402_version": 2,
        "mode": _settings.x402_mode,
        "routes": catalog,
    }


@app.get("/.well-known/agent-skills/index.json")
@app.head("/.well-known/agent-skills/index.json")
async def agent_skills_manifest(request: Request) -> Response:
    """S20-B2 — agent-skills manifest (pay.sh v1.0 compatible).

    Behind feature flag ``GECKO_MANIFEST_PUBLIC`` (default ``false``).
    When the flag is unset/false, returns 503 + ``X-Gecko-Manifest-Status: draft``
    so pay.sh's crawler can't pick up DRAFT copy. Flip the env to ``true``
    once business-manager + product-designer sign off on the 12 description
    strings (per memory ``project_output_layer_positioning``).
    """
    import hashlib

    from fastapi.responses import JSONResponse
    from gecko_core.skills.manifest import build_manifest

    flag = os.environ.get("GECKO_MANIFEST_PUBLIC", "false").strip().lower()
    if flag != "true":
        return JSONResponse(
            status_code=503,
            content={"detail": "manifest endpoint is in DRAFT mode awaiting copy review"},
            headers={"X-Gecko-Manifest-Status": "draft"},
        )

    body_dict = build_manifest()
    body_bytes = json.dumps(body_dict, separators=(",", ":"), sort_keys=True).encode("utf-8")
    if len(body_bytes) >= 64 * 1024:
        # Defensive cap. Today's manifest is ~6 KB; this should never fire.
        return JSONResponse(
            status_code=500,
            content={"detail": "manifest exceeds 64 KB cap"},
        )
    etag = f'"{hashlib.sha256(body_bytes).hexdigest()[:16]}"'

    if_none_match = request.headers.get("if-none-match")
    if if_none_match and if_none_match == etag:
        return Response(
            status_code=304,
            headers={"ETag": etag, "Cache-Control": "public, max-age=300"},
        )

    return Response(
        content=body_bytes,
        media_type="application/json",
        headers={
            "Cache-Control": "public, max-age=300",
            "ETag": etag,
        },
    )


def run() -> None:
    """Entry point for `gecko-api` command."""
    uvicorn.run(
        "gecko_api.main:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
        reload=os.environ.get("RELOAD") == "1",
    )
