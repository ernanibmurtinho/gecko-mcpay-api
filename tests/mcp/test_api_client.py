"""Tests for GeckoAPIClient.

Strategy: inject a fully-constructed httpx.AsyncClient backed by
httpx.MockTransport. We don't exercise the real x402 signing path (covered
by upstream x402 tests) — what we DO assert here is:

    1. The GeckoAPIClient calls the right URL + body for each endpoint.
    2. When the underlying transport surfaces a retried request carrying a
       PAYMENT-SIGNATURE header, the client returns the 200 body. This proves
       our wiring forwards retries from the x402 transport correctly.
    3. ``ask`` and ``list_sources`` succeed on first try without needing
       payment.
    4. The signer factory is lazy — no wallet is touched unless explicitly
       requested.
"""

from __future__ import annotations

import json

import httpx
import pytest
from gecko_mcp.api_client import (
    DEFAULT_API_URL,
    GeckoAPIClient,
    GeckoAPIError,
)


def _ok_research_body(
    session_id: str = "11111111-1111-1111-1111-111111111111",
) -> dict[str, object]:
    return {
        "session_id": session_id,
        "tier": "basic",
        "business_plan": {},
        "validation_report": {},
        "prd": {},
        "sources": [],
    }


def _encoded_payment_required() -> str:
    """Build a valid PAYMENT-REQUIRED header value matching gecko-api's 402."""
    from x402.http.utils import encode_payment_required_header
    from x402.schemas.payments import PaymentRequired, PaymentRequirements

    requirements = PaymentRequired(
        x402_version=2,
        accepts=[
            PaymentRequirements(
                scheme="exact",
                network="solana-devnet",
                asset="4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU",
                amount="20000000",
                pay_to="11111111111111111111111111111111",
                max_timeout_seconds=60,
            )
        ],
    )
    return encode_payment_required_header(requirements)


def _build_402_then_202_then_result_handler(
    capture: dict[str, object],
    success_body: dict[str, object],
) -> httpx.MockTransport:
    """Mock transport modeling the v3 async + x402 contract.

    Flow:
        1. POST /research (no payment)              → 402 + PAYMENT-REQUIRED
        2. POST /research (with PAYMENT-SIGNATURE)  → 202 {session_id, ...}
        3. GET  /sessions/{id}/result               → 200 + ResearchResult

    Mirrors gecko-api: 402 has empty body + base64 PAYMENT-REQUIRED header.
    """
    post_count = {"n": 0}
    payment_required_header = _encoded_payment_required()
    sid = success_body.get("session_id", "11111111-1111-1111-1111-111111111111")

    def handler(request: httpx.Request) -> httpx.Response:
        capture["last_url"] = str(request.url)
        capture["last_method"] = request.method
        capture["last_body"] = json.loads(request.content) if request.content else None

        if request.method == "POST" and request.url.path.startswith("/research"):
            post_count["n"] += 1
            capture["call_count"] = post_count["n"]
            capture["post_url"] = str(request.url)
            capture["post_body"] = capture["last_body"]
            if post_count["n"] == 1:
                return httpx.Response(
                    status_code=402,
                    headers={"PAYMENT-REQUIRED": payment_required_header},
                    json={},
                )
            capture["retry_payment_signature"] = request.headers.get("PAYMENT-SIGNATURE")
            return httpx.Response(
                status_code=202,
                json={
                    "session_id": sid,
                    "status": "processing",
                    "poll_url": f"/sessions/{sid}/result",
                },
            )

        if request.method == "GET" and request.url.path.endswith("/result"):
            return httpx.Response(status_code=200, json=success_body)

        return httpx.Response(status_code=404, json={"detail": "unhandled"})

    return httpx.MockTransport(handler)


def _build_simple_handler(
    capture: dict[str, object],
    body: object,
    status: int = 200,
) -> httpx.MockTransport:
    """Mock transport for the new async contract — no x402, no 402 retry.

    POST /research        → 202 {session_id, status, poll_url}
    GET  /sessions/.../result → 200 + ResearchResult (the body argument)
    """
    sid = (
        body.get("session_id", "11111111-1111-1111-1111-111111111111")
        if isinstance(body, dict)
        else "11111111-1111-1111-1111-111111111111"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        # Preserve back-compat: free endpoint tests (/ask, /sources) inspect
        # `url`/`method`/`body` for the single call they make. Paid-research
        # tests inspect `post_url`/`post_body` to skip past the poll GET.
        capture["url"] = str(request.url)
        capture["method"] = request.method
        capture["body"] = json.loads(request.content) if request.content else None

        if request.method == "POST" and request.url.path.startswith("/research"):
            capture["post_url"] = str(request.url)
            capture["post_body"] = capture["body"]
            return httpx.Response(
                status_code=202,
                json={
                    "session_id": sid,
                    "status": "processing",
                    "poll_url": f"/sessions/{sid}/result",
                },
            )
        if request.method == "GET" and request.url.path.endswith("/result"):
            return httpx.Response(status_code=status, json=body)
        if request.method in ("POST", "GET"):
            # Free endpoints (/ask, /sources) — return the body as-is.
            return httpx.Response(status_code=status, json=body)
        return httpx.Response(status_code=404, json={"detail": "unhandled"})

    return httpx.MockTransport(handler)


def test_default_api_url_falls_back_to_localhost(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GECKO_API_URL", raising=False)
    client = GeckoAPIClient()
    assert client.api_url == DEFAULT_API_URL


def test_explicit_api_url_strips_trailing_slash() -> None:
    client = GeckoAPIClient(api_url="https://api.geckovision.tech/")
    assert client.api_url == "https://api.geckovision.tech"


def test_construction_does_not_load_wallet(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub mode shouldn't require a wallet — neither should construction."""

    def explode() -> object:
        raise AssertionError("wallet should not be loaded at construction time")

    monkeypatch.setattr("gecko_mcp.api_client._build_signer", explode)
    GeckoAPIClient()  # must not raise


async def test_research_handles_402_and_retries_with_payment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """402 → client retries with PAYMENT-SIGNATURE → 200 is returned."""
    capture: dict[str, object] = {}
    success = _ok_research_body()

    inner = _build_402_then_202_then_result_handler(capture, success)

    # Build an httpx.AsyncClient that uses x402AsyncTransport on top of our
    # mock transport. We supply a FakeSigner that doesn't need a wallet.
    from x402 import x402Client
    from x402.http.clients.httpx import x402AsyncTransport

    # Patch x402Client's create_payment_payload to skip real signing.
    # Returns a payload with x402_version=2 so the http client encodes it
    # under the PAYMENT-SIGNATURE header (rather than X-PAYMENT for v1).
    class _FakePayload:
        x402_version = 2

        def model_dump_json(self, *args: object, **kwargs: object) -> str:
            return json.dumps({"x402Version": 2, "scheme": "exact", "network": "solana-devnet"})

    async def _fake_create(*_args: object, **_kwargs: object) -> _FakePayload:
        return _FakePayload()

    x402 = x402Client()
    monkeypatch.setattr(x402, "create_payment_payload", _fake_create)
    transport = x402AsyncTransport(x402, transport=inner)

    http = httpx.AsyncClient(base_url="http://test", transport=transport, timeout=30)
    client = GeckoAPIClient(api_url="http://test", http_client=http)

    result = await client.research("a hotel guide for Brazil", tier="basic")

    assert capture["call_count"] == 2  # original POST + retry POST (no x402 v3rd)
    assert capture["post_url"] == "http://test/research"
    assert isinstance(capture["post_body"], dict)
    assert capture["post_body"]["idea"] == "a hotel guide for Brazil"
    assert capture["post_body"]["tier"] == "basic"
    assert capture["post_body"]["auto_approve"] is True
    # Retry path attached the v2 signature header.
    assert capture["retry_payment_signature"], "expected PAYMENT-SIGNATURE on retry"
    # `result` is the polled ResearchResult after the 202 handshake.
    assert result["session_id"] == "11111111-1111-1111-1111-111111111111"
    # Final captured request was the GET /result poll.
    assert capture["last_method"] == "GET"
    assert str(capture["last_url"]).endswith("/result")

    await client.aclose()


async def test_research_pro_routes_to_pro_path() -> None:
    capture: dict[str, object] = {}
    transport = _build_simple_handler(capture, _ok_research_body())
    http = httpx.AsyncClient(base_url="http://test", transport=transport)
    client = GeckoAPIClient(api_url="http://test", http_client=http)

    await client.research("idea", tier="pro", urls=["https://x.com/a"])

    assert capture["post_url"] == "http://test/research/pro"
    assert isinstance(capture["post_body"], dict)
    assert capture["post_body"]["urls"] == ["https://x.com/a"]
    assert capture["post_body"]["tier"] == "pro"

    await client.aclose()


async def test_ask_succeeds_on_first_try() -> None:
    capture: dict[str, object] = {}
    body = {"session_id": "abc", "answer": "yes", "citations": []}
    transport = _build_simple_handler(capture, body)
    http = httpx.AsyncClient(base_url="http://test", transport=transport)
    client = GeckoAPIClient(api_url="http://test", http_client=http)

    result = await client.ask(session_id="abc", question="why?")

    assert capture["url"] == "http://test/sessions/abc/ask"
    assert capture["method"] == "POST"
    assert isinstance(capture["body"], dict)
    assert capture["body"]["question"] == "why?"
    assert result == body

    await client.aclose()


async def test_list_sources_succeeds_on_first_try() -> None:
    capture: dict[str, object] = {}
    body = [
        {
            "url": "https://x.com",
            "type": "web",
            "chunk_count": 1,
            "indexed_at": "2026-01-01T00:00:00Z",
        }
    ]
    transport = _build_simple_handler(capture, body)
    http = httpx.AsyncClient(base_url="http://test", transport=transport)
    client = GeckoAPIClient(api_url="http://test", http_client=http)

    result = await client.list_sources(session_id="abc")

    assert capture["url"] == "http://test/sessions/abc/sources"
    assert capture["method"] == "GET"
    assert result == body

    await client.aclose()


async def test_research_propagates_5xx_as_geckoapi_error() -> None:
    """Async contract: a 500 from /sessions/{id}/result surfaces as GeckoAPIError.

    With the v3 async handshake, the POST returns 202 unconditionally; failures
    happen at the polling step where the API returns 500 with the persisted
    error message. We assert the error wording includes the upstream detail.
    """
    capture: dict[str, object] = {}
    transport = _build_simple_handler(capture, {"detail": "boom"}, status=500)
    http = httpx.AsyncClient(base_url="http://test", transport=transport)
    client = GeckoAPIClient(api_url="http://test", http_client=http)

    with pytest.raises(GeckoAPIError, match="failed"):
        await client.research("idea")

    await client.aclose()


async def test_research_passes_project_id_in_body() -> None:
    """Phase B5 v1 — project_id + frames_username must be POSTed to /research."""
    capture: dict[str, object] = {}
    transport = _build_simple_handler(capture, _ok_research_body())
    http = httpx.AsyncClient(base_url="http://test", transport=transport)
    client = GeckoAPIClient(api_url="http://test", http_client=http)

    await client.research(
        "a hotel guide for Brazil",
        tier="basic",
        project_id="11111111-2222-3333-4444-555555555555",
        frames_username="alice",
    )

    assert isinstance(capture["post_body"], dict)
    body = capture["post_body"]
    assert body["project_id"] == "11111111-2222-3333-4444-555555555555"
    assert body["frames_username"] == "alice"

    await client.aclose()


async def test_research_omits_project_fields_when_unset() -> None:
    capture: dict[str, object] = {}
    transport = _build_simple_handler(capture, _ok_research_body())
    http = httpx.AsyncClient(base_url="http://test", transport=transport)
    client = GeckoAPIClient(api_url="http://test", http_client=http)

    await client.research("a hotel guide for Brazil", tier="basic")

    assert isinstance(capture["post_body"], dict)
    body = capture["post_body"]
    assert "project_id" not in body
    assert "frames_username" not in body

    await client.aclose()


async def test_research_preflight_blocks_when_over_budget() -> None:
    """If budget pre-flight reports overspend, raise BEFORE the paid POST."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and "/sessions/spent-by-project/" in request.url.path:
            return httpx.Response(
                status_code=200,
                json={
                    "project_id": "p1",
                    "total_spent_usd": 4.95,
                    "sessions_count": 5,
                },
            )
        # If we ever issue the paid POST, fail loudly so the test catches it.
        if request.method == "POST":
            raise AssertionError("paid POST should not run when over budget")
        return httpx.Response(status_code=404)

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(base_url="http://test", transport=transport)
    client = GeckoAPIClient(api_url="http://test", http_client=http)

    with pytest.raises(GeckoAPIError, match="budget exceeded"):
        await client.research(
            "a hotel guide for Brazil",
            tier="basic",
            project_id="11111111-2222-3333-4444-555555555555",
            budget_usd=5.00,
            estimated_cost_usd=0.10,
        )

    await client.aclose()


async def test_research_preflight_passes_when_under_budget() -> None:
    capture: dict[str, object] = {}
    success = _ok_research_body()
    sid = success["session_id"]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and "/sessions/spent-by-project/" in request.url.path:
            return httpx.Response(
                status_code=200,
                json={
                    "project_id": "p1",
                    "total_spent_usd": 1.0,
                    "sessions_count": 2,
                },
            )
        if request.method == "POST" and request.url.path.startswith("/research"):
            capture["post_body"] = json.loads(request.content)
            return httpx.Response(
                status_code=202,
                json={
                    "session_id": sid,
                    "status": "processing",
                    "poll_url": f"/sessions/{sid}/result",
                },
            )
        if request.method == "GET" and request.url.path.endswith("/result"):
            return httpx.Response(status_code=200, json=success)
        return httpx.Response(status_code=404)

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(base_url="http://test", transport=transport)
    client = GeckoAPIClient(api_url="http://test", http_client=http)

    result = await client.research(
        "a hotel guide for Brazil",
        tier="basic",
        project_id="11111111-2222-3333-4444-555555555555",
        budget_usd=5.00,
        estimated_cost_usd=0.10,
    )
    assert result["session_id"] == sid
    assert isinstance(capture["post_body"], dict)
    assert capture["post_body"]["project_id"] == "11111111-2222-3333-4444-555555555555"

    await client.aclose()


async def test_create_project_attaches_bearer_and_username() -> None:
    capture: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        capture["url"] = str(request.url)
        capture["method"] = request.method
        capture["auth"] = request.headers.get("authorization")
        capture["frames_username"] = request.headers.get("x-frames-username")
        capture["body"] = json.loads(request.content) if request.content else None
        return httpx.Response(
            status_code=201,
            json={
                "project_id": "p-1",
                "name": "demo",
                "budget_usd": 5.0,
                "wallet_address": None,
                "wallet_provider": "frames-policy",
                "created_at": "2026-04-27T00:00:00Z",
            },
        )

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(base_url="http://test", transport=transport)
    client = GeckoAPIClient(
        api_url="http://test",
        http_client=http,
        bearer="mf_token",
        frames_username="alice",
    )

    out = await client.create_project(name="demo", budget_usd=5.0)
    assert out["project_id"] == "p-1"
    assert capture["method"] == "POST"
    assert capture["url"] == "http://test/projects"
    assert capture["auth"] == "Bearer mf_token"
    assert capture["frames_username"] == "alice"
    assert isinstance(capture["body"], dict)
    assert capture["body"]["name"] == "demo"
    assert capture["body"]["budget_usd"] == 5.0
    await client.aclose()


async def test_list_projects_uses_bearer() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization")
        captured["frames_username"] = request.headers.get("x-frames-username")
        return httpx.Response(status_code=200, json=[{"project_id": "p1", "name": "demo"}])

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(base_url="http://test", transport=transport)
    client = GeckoAPIClient(
        api_url="http://test",
        http_client=http,
        bearer="mf_t",
        frames_username="alice",
    )
    out = await client.list_projects()
    assert isinstance(out, list)
    assert captured["auth"] == "Bearer mf_t"
    assert captured["frames_username"] == "alice"
    await client.aclose()


async def test_get_project_404_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code=404, json={"detail": "not found"})

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(base_url="http://test", transport=transport)
    client = GeckoAPIClient(
        api_url="http://test",
        http_client=http,
        bearer="mf_t",
        frames_username="alice",
    )
    with pytest.raises(GeckoAPIError, match="not found"):
        await client.get_project("nope")
    await client.aclose()


async def test_delete_project_calls_delete() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        return httpx.Response(status_code=204)

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(base_url="http://test", transport=transport)
    client = GeckoAPIClient(
        api_url="http://test",
        http_client=http,
        bearer="mf_t",
        frames_username="alice",
    )
    await client.delete_project("demo")
    assert captured["method"] == "DELETE"
    assert captured["url"] == "http://test/projects/demo"
    await client.aclose()


async def test_stub_x402_transport_retries_without_rpc_calls() -> None:
    """_StubX402Transport builds PaymentPayload without Solana RPC calls.

    Root cause of the Manus hang: ExactSvmScheme.create_payment_payload() makes
    synchronous SolanaClient.get_account_info() + get_latest_blockhash() calls
    that block asyncio. _StubX402Transport bypasses this entirely.
    """
    from gecko_mcp.api_client import _StubX402Transport

    post_count: dict[str, int] = {"n": 0}
    captured: dict[str, object] = {}
    sid = "22222222-2222-2222-2222-222222222222"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and "/research" in request.url.path:
            post_count["n"] += 1
            if post_count["n"] == 1:
                # First call — no payment → challenge
                return httpx.Response(
                    status_code=402,
                    headers={"PAYMENT-REQUIRED": _encoded_payment_required()},
                    json={},
                )
            # Second call — with payment → accepted
            captured["payment_signature"] = request.headers.get("payment-signature")
            return httpx.Response(
                status_code=202,
                json={"session_id": sid, "status": "processing"},
            )
        if request.method == "GET" and "/result" in request.url.path:
            return httpx.Response(status_code=200, json=_ok_research_body(sid))
        return httpx.Response(status_code=404, json={})

    transport = _StubX402Transport()
    transport._inner = httpx.MockTransport(handler)  # type: ignore[assignment]
    http = httpx.AsyncClient(base_url="http://test", transport=transport, timeout=5)
    client = GeckoAPIClient(api_url="http://test", http_client=http)

    result = await client.research("stub test idea")

    assert post_count["n"] == 2, "expected exactly 2 POST /research calls (challenge + retry)"
    assert captured["payment_signature"], "retry must carry PAYMENT-SIGNATURE header"
    assert result["session_id"] == sid

    await client.aclose()


@pytest.mark.skip(
    reason=(
        "v2-only test: GeckoAPIClient no longer exposes ._get_signer(); the "
        "self-custody signer is built inside _build_self_custody_client() once "
        "per client. Lazy-load behaviour is still verified by "
        "test_construction_does_not_load_wallet."
    )
)
async def test_get_signer_calls_build_signer_lazily() -> None:
    pass
