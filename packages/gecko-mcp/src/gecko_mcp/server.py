"""Gecko MCP server.

Exposes three tools (the contract for the Claude Code skill bootstrap):

    gecko_research — full discover -> index -> pay -> generate workflow
    gecko_ask      — follow-up question against an existing session
    gecko_sources  — list indexed sources for a session

Thin transport layer. v2: the server no longer imports `gecko_core` directly.
It is just another HTTP client of `gecko-api`, paying through the same x402
gate as any other agent. All real work happens server-side; this module
parses MCP arguments, calls `GeckoAPIClient`, and JSON-serializes the result.

Environment:
    GECKO_API_URL — base URL for `gecko-api`. Default:
                    ``https://api.geckovision.tech`` (production). Override
                    to ``http://localhost:8000`` for local dev.
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from gecko_mcp.api_client import GeckoAPIClient
from gecko_mcp.clawrouter_supervisor import warm_clawrouter

server: Server = Server("gecko-mcp")

# Lazy module-level client — built on first tool call so server startup
# succeeds even if the API is down or env is incomplete (Claude Code's MCP
# bootstrap must not fail because of transient network issues).
_client: GeckoAPIClient | None = None


def _get_client() -> GeckoAPIClient:
    global _client
    if _client is None:
        _client = GeckoAPIClient()
    return _client


_RESEARCH_DESCRIPTION = (
    "Run the full Builder Bootstrap workflow for a startup idea. Discovers "
    "sources (or uses the URLs you provide), indexes them into a session "
    "knowledge base, runs the x402 payment gate (stub mode by default), and "
    "generates a one-page business plan, validation report, and PRD with "
    "inline citations. Returns a ResearchResult JSON payload including the "
    "session_id needed for follow-up `gecko_ask` calls."
)

_ASK_DESCRIPTION = (
    "Ask a follow-up question grounded in an existing session's knowledge "
    "base. Cites the chunks used inline. Requires the session_id returned "
    "by a prior `gecko_research` call."
)

_SOURCES_DESCRIPTION = (
    "List every source indexed into a session's knowledge base (URL, type, "
    "chunk count, and indexed_at timestamp). Useful for transparency before "
    "running `gecko_ask`."
)

_PROJECT_ECONOMICS_DESCRIPTION = (
    "Per-project economics snapshot (S2-09): privy wallet address, live USDC "
    "balance, budget cap + spend, and the 5 most recent paid sessions. Use "
    "this to answer 'how much have I spent on project X?' and 'does my "
    "project wallet have enough balance for the next run?'. Distinct from "
    "`session_id`-scoped economics — pass a project UUID."
)


@server.list_tools()  # type: ignore[no-untyped-call,untyped-decorator]
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="gecko_research",
            description=_RESEARCH_DESCRIPTION,
            inputSchema={
                "type": "object",
                "properties": {
                    "idea": {
                        "type": "string",
                        "description": "Plain-language startup idea (1-2 sentences).",
                    },
                    "tier": {
                        "type": "string",
                        "enum": ["basic", "pro"],
                        "default": "basic",
                        "description": (
                            "'basic' = single-pass generation. 'pro' = multi-agent "
                            "AutoGen GroupChat (slower, deeper)."
                        ),
                    },
                    "urls": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional seed URLs (web articles or YouTube). When "
                            "omitted, sources are auto-discovered via Tavily."
                        ),
                    },
                    "auto_approve": {
                        "type": "boolean",
                        "default": True,
                        "description": (
                            "If True, skip the source-approval prompt and proceed "
                            "directly to the payment gate. MCP clients should "
                            "leave this True — there is no interactive prompt."
                        ),
                    },
                    "project_id": {
                        "type": "string",
                        "description": (
                            "Optional project UUID to attach this session to. "
                            "Phase B5 v1: enables client-side budget enforcement "
                            "and audit-trail (paid_from_wallet_address)."
                        ),
                    },
                },
                "required": ["idea"],
            },
        ),
        Tool(
            name="gecko_ask",
            description=_ASK_DESCRIPTION,
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session UUID returned by `gecko_research`.",
                    },
                    "question": {
                        "type": "string",
                        "description": "Plain-language follow-up question.",
                    },
                },
                "required": ["session_id", "question"],
            },
        ),
        Tool(
            name="gecko_sources",
            description=_SOURCES_DESCRIPTION,
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session UUID returned by `gecko_research`.",
                    },
                },
                "required": ["session_id"],
            },
        ),
        Tool(
            name="gecko_project_economics",
            description=_PROJECT_ECONOMICS_DESCRIPTION,
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": {
                        "type": "string",
                        "description": (
                            "Project UUID. Get one from `gecko project list` "
                            "or the V2 web app at app.geckovision.tech."
                        ),
                    },
                },
                "required": ["project_id"],
            },
        ),
    ]


@server.call_tool()  # type: ignore[untyped-decorator]
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    client = _get_client()

    if name == "gecko_research":
        idea = str(arguments["idea"])
        tier_raw = arguments.get("tier", "basic")
        if tier_raw not in ("basic", "pro"):
            raise ValueError(f"tier must be 'basic' or 'pro', got {tier_raw!r}")
        urls_raw = arguments.get("urls")
        urls: list[str] | None = [str(u) for u in urls_raw] if isinstance(urls_raw, list) else None
        # `auto_approve` is accepted in the schema for forward-compat but
        # the API always runs auto-approved; there is no MCP prompt surface.
        project_id_raw = arguments.get("project_id")
        project_id = str(project_id_raw) if project_id_raw else None

        result = await client.research(idea, tier=tier_raw, urls=urls, project_id=project_id)
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    if name == "gecko_ask":
        result = await client.ask(
            session_id=str(arguments["session_id"]),
            question=str(arguments["question"]),
        )
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    if name == "gecko_sources":
        sources = await client.list_sources(session_id=str(arguments["session_id"]))
        return [TextContent(type="text", text=json.dumps(sources, indent=2))]

    if name == "gecko_project_economics":
        result = await client.get_project_economics(project_id=str(arguments["project_id"]))
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    raise ValueError(f"unknown tool: {name}")


async def serve() -> None:
    """Run the MCP server over stdio.

    Self-warming: brings up ClawRouter on demand if it isn't already
    reachable (and the user didn't override GECKO_LLM_ENDPOINT). The proxy
    is torn down when stdio closes.
    """
    async with warm_clawrouter(), stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())
