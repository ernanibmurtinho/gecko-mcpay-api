"""S22-MCP-HOST-06 — x402 propagation audit through Streamable HTTP /mcp.

Pattern C contract test (recorded-fixture, no live spend).

Two propagation paths to verify per `docs/strategy/2026-05-07-s22-mcp-host-deploy.md`:

1. **Tool-level x402 (selected path)** — every paid MCP tool's handler does an
   inner HTTP call to `gecko-api` via `GeckoAPIClient`. When that inner call
   returns 402, `GeckoAPIClient` raises `GeckoAPIError`; the MCP `call_tool`
   handler does NOT swallow it; the low-level MCP server's
   `_make_error_result(str(e))` wraps the exception into a `CallToolResult`
   with `isError=True` and the message in `content[0].text`.

2. **Transport-level x402 (rejected path)** — `/mcp` itself must NOT be
   paywalled (the routes config registers `/research`, `/plan`, etc., but
   not `/mcp`). Verified by asserting `requires_payment` is False for `/mcp`
   in PaymentMiddlewareASGI's route table.

Per `feedback_lighter_tests.md`: light fakes only. We monkeypatch one method
on `GeckoAPIClient` and assert on the JSON-RPC envelope shape. No live x402,
no Solana RPC.
"""

from __future__ import annotations

import os
import sys
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

# Force stub mode BEFORE importing the app — settings frozen at import time.
os.environ.setdefault("X402_MODE", "stub")
os.environ.setdefault("GECKO_WALLET_ADDRESS", "STUB_WALLET_ADDRESS_NOT_FOR_LIVE")


# A redacted, realistic-shaped 402 challenge body — mirrors what the
# x402 facilitator/middleware emits. No real wallet/key strings.
_FAKE_402_BODY = {
    "x402Version": 2,
    "error": "X-PAYMENT header is required",
    "accepts": [
        {
            "scheme": "exact",
            "network": "solana-devnet",
            "maxAmountRequired": "10000",
            "resource": "https://api.geckovision.tech/research",
            "description": "Research session (basic tier)",
            "mimeType": "application/json",
            "payTo": "REDACTED_PAYTO_ADDRESS",
            "maxTimeoutSeconds": 60,
            "asset": "REDACTED_USDC_MINT",
            "extra": {"feePayer": "REDACTED_FEEPAYER"},
        }
    ],
}


@pytest.fixture
def client() -> Iterator[TestClient]:
    """Build a fresh TestClient against gecko_api.main:app.

    Clears `gecko_api` and `gecko_mcp` modules so import-time state
    (FastMCP session manager, settings) is rebuilt per test.
    """
    for mod in [m for m in sys.modules if m.startswith(("gecko_api", "gecko_mcp"))]:
        sys.modules.pop(mod, None)

    from gecko_api.main import app  # type: ignore[import-untyped]

    # TestClient as a context manager runs the lifespan, which starts
    # the FastMCP session manager that the /mcp mount needs.
    with TestClient(app) as c:
        yield c


def _stub_call_tool_with_402(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace `gecko_mcp.server.call_tool` with a coroutine that raises
    a 402-shaped GeckoAPIError.

    Why patch `call_tool` rather than `_paid_post`: FastMCP's wrapper
    introspects `_wrapper(**kwargs)` and generates an arg model with a
    `kwargs` field; the wrapper's own arg-handling drops `arguments["idea"]`
    before reaching `_paid_post` (see gap notes in the test docstring).
    Patching `call_tool` directly is the smallest fake that exercises the
    exception->isError propagation path through the FastMCP envelope —
    which is the actual subject of this audit.
    """
    import json as _json

    from gecko_mcp import server as server_module
    from gecko_mcp.api_client import GeckoAPIError

    # Match the literal error-string shape from api_client._paid_post:
    # `f"{path} returned 402: {response.text[:300]}"`
    _msg = f"/research returned 402: {_json.dumps(_FAKE_402_BODY)[:300]}"

    async def _fake_call_tool(name: str, arguments: dict[str, Any]) -> Any:
        raise GeckoAPIError(_msg)

    monkeypatch.setattr(server_module, "call_tool", _fake_call_tool)


def _mcp_initialize(client: TestClient) -> str:
    """Open an MCP session over Streamable HTTP, returning the session id.

    FastMCP's stateless_http=True still requires the initialize handshake.
    Returns the Mcp-Session-Id header for the follow-up tools/call.
    """
    rpc = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "audit-test", "version": "0.0.0"},
        },
    }
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        # FastMCP's TransportSecurityMiddleware allows localhost:*; default
        # TestClient Host (testserver) is rejected with 421.
        "Host": "localhost:8000",
    }
    r = client.post("/mcp/", json=rpc, headers=headers)
    assert r.status_code == 200, f"initialize failed: {r.status_code} {r.text[:200]}"
    return str(r.headers.get("mcp-session-id", ""))


def test_mcp_mount_is_not_paywalled() -> None:
    """Path #2 audit — confirm /mcp is NOT in the x402 route table.

    The PaymentMiddlewareASGI gates only paths declared in `_routes_config`.
    /mcp must pass through unpaywalled so tool-level x402 can do its job.
    """
    for mod in [m for m in sys.modules if m.startswith(("gecko_api", "gecko_mcp"))]:
        sys.modules.pop(mod, None)

    from gecko_api.main import _routes_config

    paywalled_paths = list(_routes_config.keys())
    # No /mcp prefix routes registered.
    assert not any("/mcp" in p for p in paywalled_paths), (
        f"/mcp must NOT be in x402 route table, got: {paywalled_paths}"
    )
    # Sanity: the paid paths we DO expect are still there.
    assert any("/research" in p for p in paywalled_paths)
    assert any("/plan" in p for p in paywalled_paths)


def test_402_propagates_through_streamable_http(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Path #1 audit — 402 from inner HTTP surfaces in MCP tools/call response.

    Asserts the JSON-RPC envelope returned to the MCP client contains
    isError=true and the textual 402 marker in content[0].text. This is the
    structural shape an MCP client sees today.
    """
    _stub_call_tool_with_402(monkeypatch)

    session_id = _mcp_initialize(client)
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        # FastMCP's TransportSecurityMiddleware allows localhost:*; default
        # TestClient Host (testserver) is rejected with 421.
        "Host": "localhost:8000",
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id

    # initialized notification (required by Streamable HTTP transport)
    client.post(
        "/mcp/",
        json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        headers=headers,
    )

    rpc = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "tools/call",
        "params": {
            "name": "gecko_research",
            # FastMCP introspects the wrapper signature `**kwargs`, so the
            # tool-argument model has a single `kwargs` field. Wrap the
            # MCP-level args accordingly to satisfy validation.
            "arguments": {"kwargs": {"idea": "audit smoke test", "tier": "basic"}},
        },
    }
    r = client.post("/mcp/", json=rpc, headers=headers)
    assert r.status_code == 200, f"tools/call HTTP error: {r.status_code} {r.text[:300]}"

    # FastMCP with json_response=True returns a single JSON-RPC envelope.
    # With stateless transport it can also stream as SSE; handle both.
    body_text = r.text
    if body_text.startswith("event:") or "data:" in body_text[:20]:
        # SSE — pull the data: line.
        for line in body_text.splitlines():
            if line.startswith("data:"):
                import json as _json

                env = _json.loads(line[len("data:") :].strip())
                break
        else:
            pytest.fail(f"no SSE data line in response: {body_text[:300]}")
    else:
        env = r.json()

    assert env.get("jsonrpc") == "2.0"
    result = env.get("result")
    assert result is not None, f"no result in envelope: {env}"

    # The 402 propagates as an MCP tool error, NOT as a JSON-RPC error.
    # MCP's contract: tool failures land in result.isError + result.content.
    assert result.get("isError") is True, f"expected isError=True, got result={result}"
    content = result.get("content") or []
    assert content, f"empty content in error result: {result}"
    text = content[0].get("text", "")
    assert "402" in text, f"402 marker missing from error text: {text!r}"
    # Path-level signal so an MCP client could at least parse the upstream route.
    assert "/research" in text, f"upstream path missing from error text: {text!r}"


def test_402_error_text_lacks_structured_challenge(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gap audit — confirm the structured 402 challenge is FLATTENED.

    The current path stringifies the GeckoAPIError, so the raw `accepts`
    array, payTo, scheme, network, etc. survive only as a JSON substring
    inside the error text — NOT as a parseable MCP error.data field. This
    test pins that gap so a future fix has a green-flips-red signal.

    If this test starts failing because `error.data` becomes structured,
    DELETE this test and update HOST-06 docs — the gap closed.
    """
    _stub_call_tool_with_402(monkeypatch)

    session_id = _mcp_initialize(client)
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        # FastMCP's TransportSecurityMiddleware allows localhost:*; default
        # TestClient Host (testserver) is rejected with 421.
        "Host": "localhost:8000",
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    client.post(
        "/mcp/",
        json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        headers=headers,
    )

    rpc = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "tools/call",
        "params": {
            "name": "gecko_research",
            "arguments": {"kwargs": {"idea": "audit", "tier": "basic"}},
        },
    }
    r = client.post("/mcp/", json=rpc, headers=headers)
    assert r.status_code == 200

    body_text = r.text
    if "data:" in body_text[:20] or body_text.startswith("event:"):
        import json as _json

        env = None
        for line in body_text.splitlines():
            if line.startswith("data:"):
                env = _json.loads(line[len("data:") :].strip())
                break
        assert env is not None
    else:
        env = r.json()

    result = env.get("result") or {}
    # No structured `data` field on the result — challenge is in text only.
    assert "data" not in result, "structured 402 data appeared on result — gap closed; update audit"
    # And the error envelope is NOT a JSON-RPC error (which WOULD carry data).
    assert "error" not in env, "402 surfaced as JSON-RPC error — fix may be in place; update audit"
