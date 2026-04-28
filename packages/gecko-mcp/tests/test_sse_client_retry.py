"""SSE final-event `retry_token` propagation.

When a Pro debate fails, the API emits a synthetic final SSE event whose
JSON payload includes a `retry_token` field. `stream_pro_events` must
return that final payload as-is so the higher MCP layer can stash the
token and prompt the user to retry without recharge.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from gecko_mcp.sse_client import stream_pro_events


def _sse_bytes(events: list[dict[str, Any]]) -> bytes:
    out: list[str] = []
    for ev in events:
        out.append(f"event: turn\ndata: {json.dumps(ev)}\n\n")
    return "".join(out).encode("utf-8")


def _make_transport(payload: bytes, status: int = 200) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code=status, content=payload)

    return httpx.MockTransport(handler)


async def test_final_event_carries_retry_token() -> None:
    """A failure-final event with retry_token must surface that field."""
    payload = _sse_bytes(
        [
            {"type": "turn_end", "agent": "analyst", "content": "TAM ok.", "seq": 1},
            {
                "type": "final",
                "agent": None,
                "content": '{"error":"OpenAI rate limit","retry_token":"r.abc"}',
                "seq": 2,
                "retry_token": "abc.def.signed",
                "error": "OpenAI rate limit",
            },
        ]
    )
    transport = _make_transport(payload)
    http = httpx.AsyncClient(transport=transport)

    final = await stream_pro_events(
        events_url="http://test/research/pro/sid/events",
        events_token="t",
        http_client=http,
    )

    assert final is not None
    assert final["type"] == "final"
    assert final.get("retry_token") == "abc.def.signed"
    assert final.get("error") == "OpenAI rate limit"

    await http.aclose()


async def test_final_event_without_retry_token_is_unchanged() -> None:
    """A successful final event has no retry_token field — propagation is a no-op."""
    payload = _sse_bytes(
        [
            {"type": "final", "content": "done", "agent": None, "seq": 1},
        ]
    )
    transport = _make_transport(payload)
    http = httpx.AsyncClient(transport=transport)

    final = await stream_pro_events(
        events_url="http://test/x",
        events_token="t",
        http_client=http,
    )

    assert final is not None
    assert final["type"] == "final"
    assert "retry_token" not in final

    await http.aclose()
