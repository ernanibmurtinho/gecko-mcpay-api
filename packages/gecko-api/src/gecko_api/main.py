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
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any
from uuid import UUID

import gecko_core
import uvicorn
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from gecko_core.models import AskResult, SourceInfo, Tier
from gecko_core.payments.cdp import CDPCredentials, build_cdp_facilitator_client
from gecko_core.sessions.store import SessionStore
from pydantic import BaseModel, Field
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.responses import JSONResponse, StreamingResponse
from x402 import x402ResourceServer
from x402.http.facilitator_client import HTTPFacilitatorClient
from x402.http.facilitator_client_base import FacilitatorClient, FacilitatorConfig
from x402.http.middleware.fastapi import PaymentMiddlewareASGI
from x402.http.types import PaymentOption, RouteConfig
from x402.mechanisms.svm.exact import ExactSvmServerScheme

from gecko_api.auth import verify_frames_token
from gecko_api.bazaar import (
    BAZAAR_EXTENSIONS,
    extension_as_dict,
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

    Stub returns a fake settle. Live and frames talk to a real facilitator
    over HTTP. On `solana-mainnet` we route to CDP (Coinbase Developer
    Platform) and sign every verify/settle with a fresh JWT bearer token.
    Same protocol surface in all three cases — the rest of the stack
    doesn't care which client it's holding.
    """
    if settings.x402_mode == "stub":
        # Stub must advertise the same network identifier the routes register
        # under. The x402 lib reconciles route.network against
        # facilitator.get_supported().kinds[*].network at init time; a mismatch
        # raises RouteConfigurationError. Routes use CAIP-2 (chain id), so the
        # stub does too.
        stub_network = (
            settings.network_config.chain_id if settings.network_config else settings.x402_network
        )
        return StubFacilitatorClient(network=stub_network)

    # Live + frames talk to a real facilitator. Mainnet → CDP with JWT auth;
    # devnet → public x402.org (or whatever X402_FACILITATOR_URL overrides).
    assert settings.x402_facilitator_url is not None  # checked in Settings.from_env
    if settings.x402_network == "solana-mainnet":
        # `from_env` already verified non-sentinel CDP creds for mainnet.
        assert settings.cdp_api_key_id is not None
        assert settings.cdp_api_key_secret is not None
        creds = CDPCredentials.from_env_values(
            settings.cdp_api_key_id,
            settings.cdp_api_key_secret,
        )
        return build_cdp_facilitator_client(
            creds,
            base_url=settings.x402_facilitator_url,
        )
    return HTTPFacilitatorClient(FacilitatorConfig(url=settings.x402_facilitator_url))


def _build_routes(settings: Settings) -> dict[str, RouteConfig]:
    pay_to = settings.gecko_wallet_address or "STUB_WALLET_ADDRESS_NOT_FOR_LIVE"
    # The x402 lib's RouteConfig.PaymentOption.network expects the CAIP-2
    # chain id (e.g. "solana:EtW..."), NOT the friendly name. The lib runs a
    # facilitator-support check at registration time; passing "solana-devnet"
    # raises RouteConfigurationError("Facilitator doesn't support \"exact\"
    # on \"solana-devnet\"") on the first request. The friendly name lives
    # only in our env / settings / runbook for human ergonomics.
    chain_id = (
        settings.network_config.chain_id if settings.network_config else settings.x402_network
    )
    routes: dict[str, RouteConfig] = {
        "POST /research": RouteConfig(
            accepts=[
                PaymentOption(
                    scheme="exact",
                    pay_to=pay_to,
                    price=settings.research_basic_price,
                    network=chain_id,
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
                ),
            ],
            description="Generate the 3-file scaffold bundle for a Pro session",
        )
        # /pulse is opt-in: register on x402 only when the operator sets a
        # non-zero PULSE_CALL_PRICE. Pricing for pulse is still TBD; we keep
        # the wire shape stable so the V2 web app can call it for free while
        # we decide. "$0", "$0.00", or empty string all parse as free.
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
                    ),
                ],
                description="Re-run the Advisor Panel and surface deltas vs prior pulse",
            )
    return routes


def _build_resource_server(facilitator: FacilitatorClient) -> x402ResourceServer:
    server = x402ResourceServer(facilitator)
    # Wildcard registration covers both "solana-devnet" / "solana-mainnet"
    # (V1 names) and the CAIP-2 form returned post-normalization.
    scheme: Any = ExactSvmServerScheme()  # type: ignore[no-untyped-call]
    server.register("solana:*", scheme)
    server.register("solana-devnet", scheme)
    server.register("solana-mainnet", scheme)
    return server


# Module-level so the lifespan + the /.well-known endpoint share one config.
_settings = Settings.from_env()
_facilitator = _build_facilitator(_settings)
_routes_config = _build_routes(_settings)
_resource_server = _build_resource_server(_facilitator)

# Strong refs to background tasks so they aren't GC'd mid-flight. Tasks
# remove themselves from the set in their done callback.
_BACKGROUND_TASKS: set[asyncio.Task[None]] = set()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    load_dotenv()
    # S8-LOG-01 — install the redaction filter at app startup so httpcore /
    # hpack DEBUG dumps in CloudWatch don't leak Supabase JWT, Bearer
    # tokens, or apikeys.
    from gecko_core._logging import install as install_redaction

    level_name = (os.environ.get("LOG_LEVEL") or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    install_redaction(level=level)
    logger.info("gecko-api starting (X402_MODE=%s)", _settings.x402_mode)
    if not _settings.is_privy_configured():
        logger.warning(
            "Privy unconfigured — per-project wallet provisioning disabled. "
            "Set PRIVY_APP_ID and PRIVY_APP_SECRET to enable."
        )
    else:
        logger.info("Privy configured: per-project wallet provisioning ON")
    yield


# Rate limiter: prefer Authorization header as the bucket key (so a single
# user can't share rate by spoofing IPs); fall back to remote address for
# unauthenticated paths. The limiter is exposed via app.state for slowapi's
# decorator + handler.
def _rate_limit_key(request: Request) -> str:
    auth = request.headers.get("authorization")
    if auth:
        return auth
    return get_remote_address(request)


limiter = Limiter(key_func=_rate_limit_key)


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
            entries = _unpaid_rate_state.get(client_ip, [])
            entries = [t for t in entries if t >= cutoff]
            if len(entries) >= self.rate:
                _unpaid_rate_state[client_ip] = entries
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
            _unpaid_rate_state[client_ip] = entries

        await self.app(scope, receive, send)


app.add_middleware(
    _UnauthenticatedRateLimitMiddleware,
    rate_per_minute=_UNPAID_RATE_LIMIT_PER_MINUTE,
    paths=_RATE_LIMITED_PATHS,
)


# Internal ops routes (S2X-09: /internal/twitsh/balance for EventBridge).
# Mounted after middleware so x402 doesn't gate them; they're free and
# read-only. Discovery: not advertised in /.well-known/x402.
app.include_router(internal_router)


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class ResearchRequest(BaseModel):
    idea: str = Field(..., min_length=10, max_length=500)
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


class AskRequest(BaseModel):
    question: str = Field(..., min_length=3, max_length=500)


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
    """S8-API-01 — HTTP shape for POST /pulse.

    Either ``session_id`` or ``project_id`` is required; project_id wins
    when both are given. Matches the MCP gecko_pulse tool.
    """

    session_id: str | None = None
    project_id: str | None = None
    tier_preset: str = Field(default="balanced", pattern="^(quality|balanced|budget|free)$")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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
    task.add_done_callback(_BACKGROUND_TASKS.discard)

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
    task.add_done_callback(_BACKGROUND_TASKS.discard)

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
    )


async def _run_pro_debate(
    *,
    session_id: UUID,
    idea: str,
    urls: list[str] | None = None,
    project_id: str | None = None,
    tier_preset: str = "balanced",
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
    task.add_done_callback(_BACKGROUND_TASKS.discard)

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
    """Free follow-up. Once a session is paid for, queries are unlimited."""
    try:
        return await gecko_core.ask(session_id=session_id, question=req.question)
    except NotImplementedError as e:
        raise HTTPException(status_code=501, detail=str(e)) from e


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

    return panel.model_dump(mode="json")


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
    """S8-API-01 — re-run the Advisor Panel and surface deltas.

    Free by default in all modes (stub + live) until the team commits to a
    pulse pricing model. When PULSE_CALL_PRICE is set to a non-zero value,
    the route auto-registers on the x402 middleware (see ``_build_routes``)
    and the same handler runs after settle.
    """
    from uuid import UUID as _UUID

    from gecko_core.orchestration.advisor import (
        AdvisorSessionNotFoundError,
        run_pulse,
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

    try:
        result = await run_pulse(
            session_id=sid,
            project_id=pid,
            tier_preset=tier,
        )
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


@app.get("/sessions/{session_id}/result")
async def session_result(session_id: str) -> dict[str, Any]:
    """Poll for the async ResearchResult.

    Status semantics:
        - 200 + ResearchResult JSON: workflow complete
        - 425 Too Early: still processing — poll again in a few seconds
        - 500 + {error}: workflow failed (see the `error` field)
        - 404: session not found
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


def run() -> None:
    """Entry point for `gecko-api` command."""
    uvicorn.run(
        "gecko_api.main:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
        reload=os.environ.get("RELOAD") == "1",
    )
