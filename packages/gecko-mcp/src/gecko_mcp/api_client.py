"""HTTP client for the Gecko API used by the MCP server.

The MCP server is just another x402 client. When Claude Code invokes a tool,
this client POSTs to `gecko-api`. Two modes (auto-detected):

1. **frames.ag mode (v3 default)** — if `~/.agentwallet/config.json` exists,
   we delegate every paid call to frames.ag's `/x402/fetch` proxy. Frames
   handles 402 detection, USDC signing on Solana, retries, and returns the
   final upstream response. We never see the apiToken or sign anything
   locally. Free endpoints (`/sessions/{id}/ask`, `/sessions/{id}/sources`)
   bypass frames and hit gecko-api directly over plain HTTPS.

2. **self-custody fallback (v2)** — if frames.ag isn't configured, fall
   back to the old `KeypairSigner` path with `~/.gecko/wallet.json`. Used
   for CI, demos that need a non-frames keypair, or environments without
   internet access to frames.ag.

Environment:
    GECKO_API_URL     — base URL for `gecko-api`. Production:
                        ``https://api.geckovision.tech``. Override for local
                        dev (`http://localhost:8000`) or staging. **In v3
                        frames.ag mode the URL must be HTTPS** — frames
                        refuses HTTP except in their internal dev mode.
    GECKO_MAX_PAYMENT — per-call cap for frames.ag `/x402/fetch`, USD as a
                        decimal string. Default: ``"0.50"``. Set higher in
                        production where pricing is real.

Security:
    - The apiToken is read from disk via `FramesAGWallet`; never logged.
    - The self-custody keypair is loaded only on demand — server startup
      never requires either.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Literal

import httpx

logger = logging.getLogger(__name__)

# Production URL — gecko-api is deployed there. Override via GECKO_API_URL
# for local dev (`http://localhost:8000`) or staging.
DEFAULT_API_URL = "https://api.geckovision.tech"
DEFAULT_MAX_PAYMENT_USD = "1.00"
# Generous: research POSTs may run a full pipeline (discover → embed → generate).
_DEFAULT_TIMEOUT = httpx.Timeout(300.0, connect=10.0)

Tier = Literal["basic", "pro"]


class GeckoAPIError(RuntimeError):
    """Raised when the API returns a non-2xx response we can't recover from."""


# ---------------------------------------------------------------------------
# v3 path — frames.ag /x402/fetch
# ---------------------------------------------------------------------------


def _frames_configured() -> bool:
    """True iff `~/.agentwallet/config.json` exists with an apiToken."""
    from gecko_mcp.wallet import CONFIG_PATH

    if not CONFIG_PATH.exists():
        return False
    try:
        cfg = json.loads(CONFIG_PATH.read_text())
    except Exception:
        return False
    return bool(cfg.get("apiToken"))


# ---------------------------------------------------------------------------
# v2 path — local KeypairSigner (kept as fallback)
# ---------------------------------------------------------------------------


def _build_signer() -> Any:
    """Build a KeypairSigner from the local wallet (v2 self-custody)."""
    from x402.mechanisms.svm.signers import KeypairSigner

    from gecko_mcp.wallet import get_keypair_for_signing

    return KeypairSigner(get_keypair_for_signing())


class _StubX402Transport(httpx.AsyncBaseTransport):
    """Handles x402 payment for stub-mode servers without Solana RPC calls.

    ExactSvmScheme.create_payment_payload() makes synchronous RPC calls to
    api.devnet.solana.com (getAccountInfo + getLatestBlockhash) which block
    the asyncio event loop. For stub servers StubFacilitatorClient.verify()
    returns is_valid=True without inspecting the payload, so we only need a
    structurally valid PaymentPayload — no blockhash, no mint lookup needed.
    """

    _RETRY_FLAG = "_stub_x402_retry"

    def __init__(self) -> None:
        self._inner = httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._inner.handle_async_request(request)
        if response.status_code != 402 or request.extensions.get(self._RETRY_FLAG):
            return response

        await response.aread()
        try:
            pay_hdr = response.headers.get("payment-required") or response.headers.get(
                "PAYMENT-REQUIRED"
            )
            if not pay_hdr:
                return response

            from x402.http.utils import (
                decode_payment_required_header,
                encode_payment_signature_header,
            )
            from x402.schemas import PaymentPayload

            payment_required = decode_payment_required_header(pay_hdr)
            if not payment_required.accepts:
                return response

            stub_payload = PaymentPayload(
                x402_version=2,
                payload={"transaction": "c3R1Yg=="},  # base64("stub") — never verified
                accepted=payment_required.accepts[0],
            )
            encoded = encode_payment_signature_header(stub_payload)

            new_headers = dict(request.headers)
            new_headers["PAYMENT-SIGNATURE"] = encoded
            new_extensions = dict(request.extensions)
            new_extensions[self._RETRY_FLAG] = True

            retry = httpx.Request(
                method=request.method,
                url=request.url,
                headers=new_headers,
                content=request.content,
                extensions=new_extensions,
            )
            return await self._inner.handle_async_request(retry)
        except Exception:
            return response  # fall back to the original 402 on any error

    async def aclose(self) -> None:
        await self._inner.aclose()


def _build_stub_client(api_url: str) -> httpx.AsyncClient:
    """x402 client for stub-mode servers — no Solana RPC calls.

    Stub servers' StubFacilitatorClient.verify() returns is_valid=True without
    inspecting the payment payload, so we only need a structurally valid
    PaymentPayload. _StubX402Transport builds one without fetching blockhash or
    mint info from the Solana RPC.
    """
    transport = _StubX402Transport()
    return httpx.AsyncClient(base_url=api_url, transport=transport, timeout=_DEFAULT_TIMEOUT)


def _build_self_custody_client(api_url: str) -> httpx.AsyncClient:
    """Construct the v2 self-custody client with x402AsyncTransport.

    Imported lazily so the v3 path doesn't pull in solana-py at module load.
    """
    from x402 import x402Client
    from x402.http.clients.httpx import x402AsyncTransport
    from x402.mechanisms.svm.exact import ExactSvmScheme

    signer = _build_signer()
    x402 = x402Client()
    x402.register("solana:*", ExactSvmScheme(signer=signer))
    x402.register("solana-devnet", ExactSvmScheme(signer=signer))
    x402.register("solana-mainnet", ExactSvmScheme(signer=signer))
    transport = x402AsyncTransport(x402)
    return httpx.AsyncClient(base_url=api_url, transport=transport, timeout=_DEFAULT_TIMEOUT)


# ---------------------------------------------------------------------------
# Public client
# ---------------------------------------------------------------------------


class GeckoAPIClient:
    """Thin async wrapper over `gecko-api` with x402 payment handling.

    Auto-detects frames.ag (v3) vs self-custody (v2) from the local config.
    Free endpoints always go through plain httpx — no payment proxy.
    """

    PAID_PATHS: frozenset[str] = frozenset(
        {
            "/research",
            "/research/pro",
            "/route",
            "/route/premium",
            "/route/upgrade",
            "/plan",
            # S23-REPORT-01 — /report/{session_id} is paid; the path prefix
            # check in _paid_post uses startswith so this sentinel is not used
            # directly, but documenting the paid nature here for discoverability.
        }
    )

    def __init__(
        self,
        api_url: str | None = None,
        http_client: httpx.AsyncClient | None = None,
        max_payment_usd: str | None = None,
        *,
        bearer: str | None = None,
        frames_username: str | None = None,
    ) -> None:
        self.api_url = (api_url or os.environ.get("GECKO_API_URL", DEFAULT_API_URL)).rstrip("/")
        self._max_payment = max_payment_usd or os.environ.get(
            "GECKO_MAX_PAYMENT", DEFAULT_MAX_PAYMENT_USD
        )
        self._http: httpx.AsyncClient | None = http_client
        # Bearer + username for /projects endpoints. Read lazily so tests
        # without a wallet config can still construct the client.
        self._bearer = bearer
        self._frames_username = frames_username
        # Held as `object` so the v2 codepath doesn't pay the import cost of
        # FramesAGWallet at module load. Cast on use inside _paid_post_via_frames.
        self._frames_wallet: object | None = None
        # Explicit http_client= bypasses frames.ag autodetection — tests inject
        # MockTransport-backed clients and expect the request to flow directly.
        self._mode: Literal["frames", "self-custody", "unset"] = (
            "self-custody" if http_client is not None else "unset"
        )

    async def _server_is_stub(self) -> bool:
        """Return True if the remote API reports payments=stub via /healthz.

        Distinguishes "healthz says non-stub" (legitimate live mode) from
        "healthz unreachable / unparseable" (transient failure). On transient
        failure we log a warning and return False so the caller falls back to
        frames/self-custody — but the warning surfaces the real cause instead
        of letting a network blip masquerade as a "frames.ag refuses HTTP"
        error downstream.
        """
        try:
            async with httpx.AsyncClient(timeout=5.0) as c:
                r = await c.get(f"{self.api_url}/healthz")
        except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as e:
            logger.warning(
                "api_client: healthz unreachable at %s (%s); falling back to live mode",
                self.api_url,
                type(e).__name__,
            )
            return False
        try:
            payload = r.json()
        except ValueError:
            logger.warning(
                "api_client: healthz at %s returned non-JSON (status=%d); assuming live mode",
                self.api_url,
                r.status_code,
            )
            return False
        return payload.get("payments") == "stub"

    async def _ensure_mode(self) -> None:
        if self._mode != "unset":
            return
        # Local dev (http://) — assume stub mode and use throwaway keypair.
        # The server still issues a real 402 challenge (ExactSvmScheme requires
        # feePayer in extra); StubFacilitatorClient.verify() accepts any sig.
        if self.api_url.startswith("http://"):
            self._mode = "self-custody"
            self._http = _build_stub_client(self.api_url)
            logger.info("api_client: stub-signer mode (local http://)")
            return
        # Remote stub mode — server's StubFacilitatorClient.verify() returns
        # is_valid=True unconditionally, so a throwaway keypair is enough.
        # No wallet file, no funded balance required.
        if await self._server_is_stub():
            self._mode = "self-custody"
            self._http = _build_stub_client(self.api_url)
            logger.info("api_client: stub-signer mode (remote stub, throwaway keypair)")
            return
        if _frames_configured():
            from gecko_mcp.wallet import FramesAGWallet

            self._frames_wallet = FramesAGWallet()
            self._mode = "frames"
            logger.info("api_client: frames.ag mode (v3)")
        else:
            self._mode = "self-custody"
            logger.info("api_client: self-custody mode (v2 fallback)")

    async def _free_client(self) -> httpx.AsyncClient:
        """httpx client for free endpoints. Plain HTTPS, no x402 wiring."""
        if self._http is None:
            await self._ensure_mode()
            if self._mode == "self-custody":
                self._http = _build_self_custody_client(self.api_url)
            else:
                self._http = httpx.AsyncClient(base_url=self.api_url, timeout=_DEFAULT_TIMEOUT)
        return self._http

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None
        wallet = self._frames_wallet
        if wallet is not None and hasattr(wallet, "aclose"):
            await wallet.aclose()
            self._frames_wallet = None

    async def __aenter__(self) -> GeckoAPIClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------------
    # Paid endpoints — route through frames.ag in v3, or x402AsyncTransport in v2
    # ------------------------------------------------------------------

    async def _paid_post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        await self._ensure_mode()

        if self._mode == "frames":
            return await self._paid_post_via_frames(path, body)
        # v2 fallback — original transport-based flow
        http = await self._free_client()
        try:
            response = await http.post(path, json=body)
        except httpx.HTTPError as exc:
            raise GeckoAPIError(f"could not reach gecko-api at {self.api_url}: {exc}") from exc
        if response.status_code >= 400:
            raise GeckoAPIError(f"{path} returned {response.status_code}: {response.text[:300]}")
        return _parse_json_object(response, path)

    async def _paid_post_via_frames(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """Route a paid call through frames.ag's `/x402/fetch` proxy."""
        assert self._frames_wallet is not None  # set by _ensure_mode

        target_url = f"{self.api_url}{path}"
        if not target_url.lower().startswith("https://"):
            raise GeckoAPIError(
                f"frames.ag refuses HTTP — set GECKO_API_URL to an HTTPS origin "
                f"(got {self.api_url!r}). For local dev, expose port 8000 via "
                "ngrok/cloudflared and set GECKO_API_URL=<tunnel-url>."
            )

        try:
            result = await self._frames_wallet.x402_fetch(  # type: ignore[attr-defined]
                target_url,
                method="POST",
                body=body,
                max_payment_usd=self._max_payment,
            )
        except httpx.HTTPStatusError as exc:
            # Surface the actual response body so 502/504 from frames.ag's
            # gateway is debuggable. Truncate to keep error messages readable.
            try:
                body_preview = exc.response.text[:300]
            except Exception:
                body_preview = "<no body>"
            raise GeckoAPIError(
                f"frames.ag /x402/fetch failed [{exc.response.status_code}]: {body_preview}"
            ) from exc
        except httpx.HTTPError as exc:
            raise GeckoAPIError(
                f"frames.ag /x402/fetch network error: {type(exc).__name__}: {exc}"
            ) from exc

        # frames returns {success, paid, response: {status, body, headers, ...}, error, errorCode}
        if not result.get("success"):
            code = result.get("errorCode") or "UNKNOWN"
            err = result.get("error") or "unknown error"
            raise GeckoAPIError(f"{path} via frames.ag failed [{code}]: {err}")

        upstream = result.get("response") or {}
        status = int(upstream.get("status") or 0)
        if status >= 400:
            raise GeckoAPIError(f"{path} returned upstream {status}")

        # response.body is a JSON-encoded string per frames.ag's contract
        raw_body = upstream.get("body")
        if isinstance(raw_body, str):
            try:
                parsed = json.loads(raw_body)
            except json.JSONDecodeError as exc:
                raise GeckoAPIError(f"{path} returned non-JSON body: {exc}") from exc
        else:
            parsed = raw_body

        if not isinstance(parsed, dict):
            raise GeckoAPIError(f"{path} returned non-object JSON: {type(parsed).__name__}")

        # Stamp the on-chain tx signature into the result if the API didn't.
        # frames returns it via response.headers["payment-response"] (base64).
        headers = {k.lower(): v for k, v in (upstream.get("headers") or {}).items()}
        pay_resp = headers.get("payment-response")
        if pay_resp:
            try:
                import base64

                decoded = json.loads(base64.b64decode(pay_resp).decode("utf-8"))
                tx_sig = decoded.get("transaction")
                if tx_sig and "x402_tx_signature" not in parsed:
                    parsed["x402_tx_signature"] = tx_sig
            except Exception:
                pass  # tx surfacing is best-effort; payment already settled
        return parsed

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    async def research(
        self,
        idea: str,
        tier: Tier = "basic",
        urls: list[str] | None = None,
        *,
        project_id: str | None = None,
        frames_username: str | None = None,
        budget_usd: float | None = None,
        estimated_cost_usd: float | None = None,
        poll_interval_s: float = 4.0,
        poll_deadline_s: float = 300.0,
        progress: Any | None = None,
        tier_preset: str | None = None,
    ) -> dict[str, Any]:
        """POST /research (basic) or /research/pro. Pays + polls for result.

        Wire shape (async):
            1. POST /research → 202 {session_id, status, poll_url} — payment
               settles synchronously inside this call (within frames.ag's
               upstream timeout, ~30s).
            2. Workflow runs server-side as a background task.
            3. Poll GET /sessions/{id}/result until 200 (success), 500
               (failure), or `poll_deadline_s` elapses.
        """
        path = "/research" if tier == "basic" else "/research/pro"
        body: dict[str, Any] = {"idea": idea, "tier": tier, "auto_approve": True}
        if urls is not None:
            body["urls"] = urls
        if project_id is not None:
            body["project_id"] = project_id
        if frames_username is not None:
            body["frames_username"] = frames_username
        if tier_preset is not None:
            body["tier_preset"] = tier_preset

        # Phase B5 v1 — best-effort client-side budget pre-flight. The server
        # has no per-project ceiling in v1; this trusts the user not to game
        # their own client. v2 enforces via a project-scoped Privy wallet.
        if project_id is not None and budget_usd is not None:
            await self._preflight_budget_check(
                project_id=project_id,
                budget_usd=budget_usd,
                estimated_cost_usd=estimated_cost_usd or 0.0,
            )

        # 1. Pay + create session.
        ack = await self._paid_post(path, body)
        sid = ack.get("session_id")
        if not isinstance(sid, str):
            # Non-async API contract — old server, treat ack as the full result.
            return ack

        # 2a. Pro tier with an events_token: stream the debate live, then
        # fall back to /result for the canonical ResearchResult payload.
        retry_token: str | None = None
        if tier == "pro" and ack.get("events_url") and ack.get("events_token"):
            try:
                final_payload = await self._consume_pro_sse(ack, progress)
            except Exception as exc:
                logger.warning("pro SSE failed, falling back to poll: %s", exc)
            else:
                # If the debate failed mid-flight, the SSE final event carries
                # a `retry_token` (24h, single-use). Stash it on the result so
                # the higher MCP layer can surface a "retry without recharge"
                # prompt to the user. Pure data-passthrough — no side effects.
                if isinstance(final_payload, dict):
                    tok = final_payload.get("retry_token")
                    if isinstance(tok, str) and tok:
                        retry_token = tok

        # 2b. Poll until done (works for both basic and pro).
        try:
            result = await self._poll_result(sid, poll_interval_s, poll_deadline_s, ack)
        except GeckoAPIError:
            if retry_token is not None:
                # Surface the retry token alongside the failure so the MCP
                # caller can offer a one-tap retry. We re-raise so the failure
                # itself isn't silently swallowed.
                raise
            raise
        if retry_token is not None and "retry_token" not in result:
            result["retry_token"] = retry_token
        return result

    async def _consume_pro_sse(
        self,
        ack: dict[str, Any],
        progress: Any | None,
    ) -> dict[str, Any] | None:
        """Open the SSE stream for a pro session and pump events to `progress`.

        Tolerates one transient drop. Returns the final SSE event payload
        (so callers can pick up `retry_token` on failure), or None if the
        stream closed without a final event.
        """
        from gecko_mcp.sse_client import stream_pro_events

        events_url = str(ack["events_url"])
        events_token = str(ack["events_token"])
        # `events_url` from the API is a relative path; absolutize against api_url.
        if events_url.startswith("/"):
            events_url = f"{self.api_url}{events_url}"

        async def _on_progress(line: str) -> None:
            if progress is None:
                return
            if asyncio.iscoroutinefunction(progress):
                await progress(line)
            else:
                progress(line)

        return await stream_pro_events(
            events_url=events_url,
            events_token=events_token,
            progress=_on_progress,
            timeout_s=300.0,
            reconnect_once=True,
        )

    async def _poll_result(
        self,
        session_id: str,
        interval_s: float,
        deadline_s: float,
        ack: dict[str, Any],
    ) -> dict[str, Any]:
        """Poll /sessions/{id}/result until 200, 500, or deadline."""
        http = await self._free_client()
        deadline = asyncio.get_event_loop().time() + deadline_s
        last_status_code: int = 0
        while True:
            try:
                response = await http.get(f"/sessions/{session_id}/result")
            except httpx.HTTPError as exc:
                raise GeckoAPIError(f"poll failed: {exc}") from exc

            if response.status_code == 200:
                result = _parse_json_object(response, f"/sessions/{session_id}/result")
                # Carry forward the on-chain tx signature the paid_post path
                # extracted from the PAYMENT-RESPONSE header (frames mode).
                tx = ack.get("x402_tx_signature")
                if isinstance(tx, str) and tx and "x402_tx_signature" not in result:
                    result["x402_tx_signature"] = tx
                return result
            if response.status_code == 500:
                detail = _safe_json(response)
                raise GeckoAPIError(
                    f"research session {session_id} failed: "
                    f"{detail.get('detail') if isinstance(detail, dict) else detail}"
                )
            if response.status_code == 425:
                last_status_code = 425
            elif response.status_code >= 400:
                raise GeckoAPIError(
                    f"poll {session_id} returned {response.status_code}: {response.text[:200]}"
                )

            if asyncio.get_event_loop().time() >= deadline:
                raise GeckoAPIError(
                    f"research session {session_id} did not complete within "
                    f"{deadline_s:.0f}s (last status: {last_status_code})"
                )
            await asyncio.sleep(interval_s)

    async def _preflight_budget_check(
        self,
        project_id: str,
        budget_usd: float,
        estimated_cost_usd: float,
    ) -> None:
        """Best-effort: refuse to issue the paid call if it would exceed budget.

        Hits GET /sessions/spent-by-project/{project_id} (free endpoint),
        compares spent + estimate against budget. Raises GeckoAPIError if
        over. v1 honesty marker: this is *only* enforced client-side.
        """
        http = await self._free_client()
        try:
            response = await http.get(f"/sessions/spent-by-project/{project_id}")
        except httpx.HTTPError as exc:
            logger.warning("budget pre-flight unreachable, skipping: %s", exc)
            return
        if response.status_code >= 400:
            logger.warning(
                "budget pre-flight returned %s, skipping: %s",
                response.status_code,
                response.text[:200],
            )
            return
        try:
            data = _parse_json_object(response, f"/sessions/spent-by-project/{project_id}")
        except GeckoAPIError:
            return
        spent = float(data.get("total_spent_usd") or 0.0)
        if spent + estimated_cost_usd > budget_usd:
            raise GeckoAPIError(
                f"project budget exceeded: spent ${spent:.4f} + estimated "
                f"${estimated_cost_usd:.4f} > budget ${budget_usd:.2f}"
            )

    async def retry_pro(
        self,
        session_id: str,
        retry_token: str,
        *,
        progress: Any | None = None,
        poll_interval_s: float = 4.0,
        poll_deadline_s: float = 300.0,
    ) -> dict[str, Any]:
        """Redeem a `retry_token` for a failed Pro session.

        POSTs /research/pro/{session_id}/retry?token=..., which is NOT gated
        by x402 — the token IS the credential. On 202 we get a fresh
        events_token, re-attach SSE, and poll /result the same way the
        original `research()` call did. The original payment is honored.
        """
        if not session_id:
            raise GeckoAPIError("retry_pro: session_id is required")
        if not retry_token:
            raise GeckoAPIError("retry_pro: retry_token is required")

        http = await self._free_client()
        try:
            response = await http.post(
                f"/research/pro/{session_id}/retry",
                params={"token": retry_token},
            )
        except httpx.HTTPError as exc:
            raise GeckoAPIError(f"could not reach gecko-api at {self.api_url}: {exc}") from exc
        if response.status_code != 202:
            raise GeckoAPIError(
                f"/research/pro/{session_id}/retry returned {response.status_code}: "
                f"{response.text[:300]}"
            )
        ack = _parse_json_object(response, f"/research/pro/{session_id}/retry")

        # Re-attach SSE for the live debate, then poll /result for the canonical
        # ResearchResult payload — same flow as the initial pro research call.
        if ack.get("events_url") and ack.get("events_token"):
            try:
                await self._consume_pro_sse(ack, progress)
            except Exception as exc:
                logger.warning("retry SSE failed, falling back to poll: %s", exc)

        return await self._poll_result(session_id, poll_interval_s, poll_deadline_s, ack)

    # S5-API-03: tiered /route price paths. Single mapping that the route()
    # method uses to pick the right URL based on `task_hint` + `prefer_premium`.
    # Default → $0.01, premium task hints → $0.05, prefer_premium → $0.20.
    _PREMIUM_TASK_HINTS: frozenset[str] = frozenset({"reasoning", "code"})

    @classmethod
    def _route_path_for_tier(cls, *, task_hint: str, prefer_premium: bool) -> str:
        if prefer_premium:
            return "/route/upgrade"
        if task_hint in cls._PREMIUM_TASK_HINTS:
            return "/route/premium"
        return "/route"

    async def route(
        self,
        prompt: str,
        *,
        task_hint: str = "default",
        max_cost_usd: float = 0.05,
        prefer_premium: bool = False,
        tier_preset: str = "balanced",
    ) -> dict[str, Any]:
        """POST /route — paid LLM routing through gecko-api (S4-ROUTE-02 / S5-API-03).

        Goes through the same x402 / frames.ag plumbing as /research. The
        client picks one of three paid paths based on `task_hint` +
        `prefer_premium`:

            default              → POST /route          ($0.01)
            reasoning / code     → POST /route/premium  ($0.05)
            prefer_premium=True  → POST /route/upgrade  ($0.20)

        Returns the RouteResult shape as a JSON-safe dict, with `tier_charged`
        + `prepay_usd` injected by the API.
        """
        body: dict[str, Any] = {
            "prompt": prompt,
            "task_hint": task_hint,
            "max_cost_usd": max_cost_usd,
            "prefer_premium": prefer_premium,
            "tier_preset": tier_preset,
        }
        path = self._route_path_for_tier(task_hint=task_hint, prefer_premium=prefer_premium)
        return await self._paid_post(path, body)

    async def plan(
        self,
        session_id: str,
        *,
        tier_preset: str = "balanced",
        project_id: str | None = None,
        frames_username: str | None = None,
    ) -> dict[str, Any]:
        """POST /plan — paid Advisor Panel through gecko-api (S5-API-01).

        Pays via the same x402 / frames.ag plumbing as /research at a flat
        $0.25. Returns the AdvisorPanel JSON shape.
        """
        body: dict[str, Any] = {
            "session_id": session_id,
            "tier_preset": tier_preset,
        }
        if project_id is not None:
            body["project_id"] = project_id
        if frames_username is not None:
            body["frames_username"] = frames_username
        return await self._paid_post("/plan", body)

    async def report(
        self,
        session_id: str,
        *,
        format: str = "html",
    ) -> dict[str, Any]:
        """POST /report/{session_id} — paid report generation (S23-REPORT-01).

        Pays via the same x402 / frames.ag plumbing as /plan at a flat
        $0.05. Returns ``{"html": "<html...>"}`` for format=html or
        ``{"markdown": "..."}`` for format=markdown.

        ``format`` must be a query parameter — the FastAPI endpoint declares it
        as a Query field, so JSON body values are silently ignored and the
        endpoint falls back to ``html``.
        """
        await self._ensure_mode()
        http = await self._free_client()
        path = f"/report/{session_id}?format={format}"
        try:
            response = await http.post(path, json={})
        except Exception as exc:
            raise GeckoAPIError(f"could not reach gecko-api: {exc}") from exc
        if response.status_code >= 400:
            raise GeckoAPIError(f"{path} returned {response.status_code}: {response.text[:300]}")
        ct = response.headers.get("content-type", "")
        if "text/html" in ct:
            return {"html": response.text}
        result = _parse_json_object(response, path)
        if isinstance(result, str):
            return {"html": result}
        return result

    async def ask(self, session_id: str, question: str) -> dict[str, Any]:
        """POST /sessions/{id}/ask — free follow-up against a paid session."""
        http = await self._free_client()
        try:
            response = await http.post(
                f"/sessions/{session_id}/ask",
                json={"question": question},
            )
        except httpx.HTTPError as exc:
            raise GeckoAPIError(f"could not reach gecko-api at {self.api_url}: {exc}") from exc

        if response.status_code >= 400:
            raise GeckoAPIError(
                f"/sessions/{session_id}/ask returned {response.status_code}: {response.text[:300]}"
            )
        return _parse_json_object(response, f"/sessions/{session_id}/ask")

    # ------------------------------------------------------------------
    # Projects (bearer-authed) — /projects CRUD against gecko-api.
    # ------------------------------------------------------------------

    def _load_bearer(self) -> tuple[str, str]:
        """Return (bearer, frames_username) from constructor args or wallet config.

        Raises GeckoAPIError pointing the user at `gecko-mcp wallet new` if
        no wallet config exists. Reads `~/.agentwallet/config.json` once and
        caches the result on the instance.
        """
        if self._bearer and self._frames_username:
            return self._bearer, self._frames_username
        from gecko_mcp.wallet import CONFIG_PATH

        if not CONFIG_PATH.exists():
            raise GeckoAPIError(
                f"no frames.ag credentials at {CONFIG_PATH}. Run `gecko-mcp wallet new` first."
            )
        try:
            cfg = json.loads(CONFIG_PATH.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise GeckoAPIError(f"could not read {CONFIG_PATH}: {exc}") from exc
        token = cfg.get("apiToken")
        username = cfg.get("username")
        if not isinstance(token, str) or not token:
            raise GeckoAPIError(f"{CONFIG_PATH} is missing apiToken")
        if not isinstance(username, str) or not username:
            raise GeckoAPIError(f"{CONFIG_PATH} is missing username")
        self._bearer = token
        self._frames_username = username
        return token, username

    def _auth_headers(self) -> dict[str, str]:
        token, username = self._load_bearer()
        return {
            "Authorization": f"Bearer {token}",
            "X-Frames-Username": username,
        }

    async def create_project(
        self,
        name: str,
        budget_usd: float | None = None,
    ) -> dict[str, Any]:
        """POST /projects — create a new project for the current frames.ag user."""
        http = await self._free_client()
        try:
            response = await http.post(
                "/projects",
                json={"name": name, "budget_usd": budget_usd},
                headers=self._auth_headers(),
            )
        except httpx.HTTPError as exc:
            raise GeckoAPIError(f"could not reach gecko-api at {self.api_url}: {exc}") from exc
        if response.status_code >= 400:
            raise GeckoAPIError(f"/projects returned {response.status_code}: {response.text[:300]}")
        return _parse_json_object(response, "/projects")

    async def list_projects(self) -> list[dict[str, Any]]:
        """GET /projects — list projects for the current user."""
        http = await self._free_client()
        try:
            response = await http.get("/projects", headers=self._auth_headers())
        except httpx.HTTPError as exc:
            raise GeckoAPIError(f"could not reach gecko-api at {self.api_url}: {exc}") from exc
        if response.status_code >= 400:
            raise GeckoAPIError(f"/projects returned {response.status_code}: {response.text[:300]}")
        result = response.json()
        if not isinstance(result, list):
            raise GeckoAPIError(f"/projects returned non-list JSON: {type(result).__name__}")
        return result

    async def get_project(self, name: str) -> dict[str, Any]:
        """GET /projects/{name}."""
        http = await self._free_client()
        try:
            response = await http.get(f"/projects/{name}", headers=self._auth_headers())
        except httpx.HTTPError as exc:
            raise GeckoAPIError(f"could not reach gecko-api at {self.api_url}: {exc}") from exc
        if response.status_code == 404:
            raise GeckoAPIError(f"project {name!r} not found")
        if response.status_code >= 400:
            raise GeckoAPIError(
                f"/projects/{name} returned {response.status_code}: {response.text[:300]}"
            )
        return _parse_json_object(response, f"/projects/{name}")

    async def delete_project(self, name: str) -> None:
        """DELETE /projects/{name} — soft-delete a project."""
        http = await self._free_client()
        try:
            response = await http.delete(f"/projects/{name}", headers=self._auth_headers())
        except httpx.HTTPError as exc:
            raise GeckoAPIError(f"could not reach gecko-api at {self.api_url}: {exc}") from exc
        if response.status_code == 404:
            raise GeckoAPIError(f"project {name!r} not found")
        if response.status_code >= 400:
            raise GeckoAPIError(
                f"/projects/{name} returned {response.status_code}: {response.text[:300]}"
            )

    async def get_project_economics(self, project_id: str) -> dict[str, Any]:
        """GET /projects/{project_id}/economics — bearer-authed (S2-09).

        Returns the wallet/budget/recent-sessions snapshot for the project.
        Surfaces a clean error if the API hasn't deployed the endpoint yet
        (web3-engineer's S2-05/S2-06 lands first; this method is wired so
        the MCP side is ready the moment it ships).
        """
        http = await self._free_client()
        path = f"/projects/{project_id}/economics"
        try:
            response = await http.get(path, headers=self._auth_headers())
        except httpx.HTTPError as exc:
            raise GeckoAPIError(f"could not reach gecko-api at {self.api_url}: {exc}") from exc
        if response.status_code == 401:
            raise GeckoAPIError("unauthorized — re-run `gecko-mcp wallet new`")
        if response.status_code == 404:
            # 404 is overloaded: project-not-found OR endpoint-not-deployed
            # (web3-engineer's commit pending). Probe /healthz so the user
            # knows which one applies.
            raise GeckoAPIError(
                f"project {project_id!r} not found, or this gecko-api build "
                "has not yet deployed /projects/{id}/economics (S2-05/06)"
            )
        if response.status_code >= 400:
            raise GeckoAPIError(f"{path} returned {response.status_code}: {response.text[:300]}")
        return _parse_json_object(response, path)

    async def classify(self, idea: str) -> dict[str, Any]:
        """POST /classify — paid classify-as-a-service (S13-COMMO-03).

        Returns the same JSON as the MCP gecko_classify tool: selected categories,
        full score map, and suggested sources. /classify is x402-gated but stub
        mode auto-passes.
        """
        return await self._paid_post("/classify", {"idea": idea})

    async def precedents(self, idea: str, top_k: int = 5) -> list[dict[str, Any]]:
        """POST /precedents — top-K flywheel precedents (free, no x402)."""
        http = await self._free_client()
        response = await http.post("/precedents", json={"idea": idea, "top_k": top_k})
        if response.status_code >= 400:
            raise GeckoAPIError(
                f"/precedents returned {response.status_code}: {response.text[:200]}"
            )
        result = response.json()
        if not isinstance(result, list):
            raise GeckoAPIError(f"/precedents returned non-list JSON: {type(result).__name__}")
        return result

    async def list_sources(self, session_id: str) -> list[dict[str, Any]]:
        """GET /sessions/{id}/sources — free."""
        http = await self._free_client()
        try:
            response = await http.get(f"/sessions/{session_id}/sources")
        except httpx.HTTPError as exc:
            raise GeckoAPIError(f"could not reach gecko-api at {self.api_url}: {exc}") from exc

        if response.status_code >= 400:
            raise GeckoAPIError(
                f"/sessions/{session_id}/sources returned {response.status_code}: "
                f"{response.text[:300]}"
            )
        result = response.json()
        if not isinstance(result, list):
            raise GeckoAPIError(f"sources returned non-list JSON: {type(result).__name__}")
        return result


def _parse_json_object(response: httpx.Response, path: str) -> dict[str, Any]:
    result = response.json()
    if not isinstance(result, dict):
        raise GeckoAPIError(f"{path} returned non-object JSON: {type(result).__name__}")
    return result


def _safe_json(response: httpx.Response) -> Any:
    """Parse response.json() defensively — returns the raw text if not JSON."""
    try:
        return response.json()
    except (json.JSONDecodeError, ValueError):
        return response.text
