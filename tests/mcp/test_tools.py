"""Tests for the MCP tool wrappers (v2: thin shim over GeckoAPIClient).

The wrappers are JSON-in / JSON-out around GeckoAPIClient. We mock the
client (not the API) and assert wiring: argument shape, JSON serialization,
schema fidelity. The api_client tests cover the HTTP path.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from gecko_mcp import server as server_module
from gecko_mcp.server import call_tool, list_tools


def _research_payload() -> dict[str, Any]:
    return {
        "session_id": "11111111-1111-1111-1111-111111111111",
        "tier": "basic",
        "business_plan": {"problem": "p"},
        "validation_report": {},
        "prd": {},
        "sources": [],
    }


def _source_payload() -> dict[str, Any]:
    return {
        "url": "https://example.com/article",
        "type": "web",
        "chunk_count": 3,
        "indexed_at": "2026-01-01T00:00:00+00:00",
    }


class _FakeClient:
    """Stand-in for GeckoAPIClient. Records calls; returns canned payloads."""

    def __init__(self) -> None:
        self.research_calls: list[dict[str, Any]] = []
        self.ask_calls: list[dict[str, Any]] = []
        self.sources_calls: list[dict[str, Any]] = []

    async def research(
        self,
        idea: str,
        tier: str = "basic",
        urls: list[str] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.research_calls.append({"idea": idea, "tier": tier, "urls": urls, **kwargs})
        return _research_payload()

    async def ask(self, session_id: str, question: str) -> dict[str, Any]:
        self.ask_calls.append({"session_id": session_id, "question": question})
        return {"session_id": session_id, "answer": f"a:{question}", "citations": []}

    async def list_sources(self, session_id: str) -> list[dict[str, Any]]:
        self.sources_calls.append({"session_id": session_id})
        return [_source_payload()]


@pytest.fixture(autouse=True)
def _swap_client(monkeypatch: pytest.MonkeyPatch) -> _FakeClient:
    fake = _FakeClient()
    monkeypatch.setattr(server_module, "_client", fake)
    return fake


async def test_list_tools_exposes_three_tools() -> None:
    # Name kept for git history; surface is 4 tools as of S2-09.
    tools = await list_tools()
    names = {t.name for t in tools}
    assert names == {
        "gecko_research",
        "gecko_ask",
        "gecko_sources",
        "gecko_project_economics",
    }
    by_name = {t.name: t for t in tools}
    assert by_name["gecko_research"].inputSchema["required"] == ["idea"]
    assert by_name["gecko_ask"].inputSchema["required"] == ["session_id", "question"]
    assert by_name["gecko_sources"].inputSchema["required"] == ["session_id"]
    assert by_name["gecko_project_economics"].inputSchema["required"] == ["project_id"]


async def test_gecko_research_wrapper(_swap_client: _FakeClient) -> None:
    out = await call_tool(
        "gecko_research",
        {"idea": "build a hotel guide", "tier": "basic", "urls": ["https://x.com"]},
    )
    assert len(out) == 1
    payload = json.loads(out[0].text)
    assert payload["session_id"] == "11111111-1111-1111-1111-111111111111"
    assert _swap_client.research_calls == [
        {
            "idea": "build a hotel guide",
            "tier": "basic",
            "urls": ["https://x.com"],
            "project_id": None,
        }
    ]


async def test_gecko_research_defaults_tier_basic_no_urls(_swap_client: _FakeClient) -> None:
    await call_tool("gecko_research", {"idea": "x"})
    assert _swap_client.research_calls == [
        {"idea": "x", "tier": "basic", "urls": None, "project_id": None}
    ]


async def test_gecko_research_rejects_bad_tier() -> None:
    with pytest.raises(ValueError, match="tier"):
        await call_tool("gecko_research", {"idea": "x", "tier": "ultra"})


async def test_gecko_ask_wrapper(_swap_client: _FakeClient) -> None:
    out = await call_tool("gecko_ask", {"session_id": "abc", "question": "why?"})
    payload = json.loads(out[0].text)
    assert payload["session_id"] == "abc"
    assert payload["answer"] == "a:why?"
    assert _swap_client.ask_calls == [{"session_id": "abc", "question": "why?"}]


async def test_gecko_sources_wrapper(_swap_client: _FakeClient) -> None:
    out = await call_tool("gecko_sources", {"session_id": "abc"})
    payload = json.loads(out[0].text)
    assert isinstance(payload, list)
    assert payload[0]["type"] == "web"
    assert payload[0]["chunk_count"] == 3
    assert _swap_client.sources_calls == [{"session_id": "abc"}]


async def test_unknown_tool_raises() -> None:
    with pytest.raises(ValueError, match="unknown tool"):
        await call_tool("gecko_nope", {})


def test_get_client_is_lazy_and_memoized(monkeypatch: pytest.MonkeyPatch) -> None:
    """Server must not construct a client (or load wallet) until first use."""
    monkeypatch.setattr(server_module, "_client", None)
    constructed: list[int] = []

    class _Stub:
        def __init__(self) -> None:
            constructed.append(1)

    monkeypatch.setattr(server_module, "GeckoAPIClient", _Stub)
    a = server_module._get_client()
    b = server_module._get_client()
    assert a is b
    assert len(constructed) == 1
