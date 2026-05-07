"""S24-MCP-03: subprocess smoke test — starts gecko-mcp serve and verifies tools/list.

This test catches the stale-binary problem: if the running gecko-mcp binary
doesn't include a recently-added tool, the unit test in test_tools.py (which
imports directly from source) would pass while Claude Code shows the old list.

Strategy: spawn `gecko-mcp serve` as a subprocess, connect with the MCP
ClientSession, call tools/list, assert every expected tool is present.
"""

from __future__ import annotations

import sys

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

EXPECTED_TOOLS = {
    "gecko_research",
    "gecko_ask",
    "gecko_sources",
    "gecko_classify",
    "gecko_precedents",
    "gecko_available_sources",
    "gecko_route",
    "gecko_scaffold",
    "gecko_advise",
    "gecko_plan",
    "gecko_pulse",
    "gecko_memory_save",
    "gecko_memory_recall",
    "gecko_memory_search",
    "gecko_memory_query",
    "gecko_resume",
    "gecko_review",
    "gecko_report",
    "gecko_project_economics",
}


async def test_serve_subprocess_exposes_all_tools() -> None:
    """gecko-mcp serve must expose every tool in EXPECTED_TOOLS via tools/list."""
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "gecko_mcp.cli", "serve"],
        env={"GECKO_API_URL": "https://api.geckovision.tech"},
    )

    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        result = await session.list_tools()

    names = {t.name for t in result.tools}
    missing = EXPECTED_TOOLS - names
    assert not missing, f"Tools missing from gecko-mcp serve: {sorted(missing)}"


async def test_serve_subprocess_no_unexpected_tool_removals() -> None:
    """Each tool in EXPECTED_TOOLS must survive refactors — a removal is a breaking change."""
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "gecko_mcp.cli", "serve"],
        env={"GECKO_API_URL": "https://api.geckovision.tech"},
    )

    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        result = await session.list_tools()

    names = {t.name for t in result.tools}
    # All expected tools must be present — extras are fine (new tools are additive).
    assert names >= EXPECTED_TOOLS, (
        f"Breaking tool removal detected: {sorted(EXPECTED_TOOLS - names)}"
    )
