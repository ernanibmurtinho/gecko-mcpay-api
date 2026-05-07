"""Regression test for S22-FIX-13.

The FastMCP wrappers were registered with a generic `async def _wrapper(**kwargs)`
signature. FastMCP's default `func_metadata()` introspection turned that into
an arg model with a single required `kwargs` field, which both:

1. Polluted `tools/list` (when clients introspected via the arg-model schema
   instead of the pinned `parameters`), and
2. Made `tools/call` reject the natural `arguments={"idea": "X"}` shape with
   `Field required: kwargs` from pydantic.

This test pins the post-fix invariants:
- `tool.inputSchema` advertised by FastMCP carries the per-tool field names
  (e.g. `idea` for `gecko_research`), not a single `kwargs` placeholder.
- The arg model the FastMCP runtime validates against accepts the natural
  top-level kwargs shape without demanding a literal `kwargs` field.

Per `feedback_lighter_tests.md`: targeted assertions on a small set of
known fields per tool — no full server-startup fixture, no live API.
"""

from __future__ import annotations

import asyncio

from gecko_mcp.server import mcp

# Sentinel field-set per tool. Not exhaustive — just enough to prove the
# real schema (not the `**kwargs` placeholder) is what's being advertised.
_EXPECTED_FIELDS: dict[str, set[str]] = {
    "gecko_research": {"idea", "tier", "urls", "tier_preset", "project_id"},
    "gecko_ask": {"session_id", "question"},
    "gecko_sources": {"session_id"},
    "gecko_classify": {"idea"},
    "gecko_precedents": {"idea"},
    "gecko_route": {"prompt"},
}


def test_no_tool_advertises_kwargs_field() -> None:
    """No tool's input schema may carry the `**kwargs` wrapper placeholder."""
    tools = asyncio.run(mcp.list_tools())
    assert tools, "FastMCP registry is empty — registration broke"
    for t in tools:
        schema = t.inputSchema or {}
        props = schema.get("properties", {})
        assert "kwargs" not in props, (
            f"Tool {t.name!r} input schema leaks the `kwargs` placeholder "
            f"from the FastMCP wrapper signature: {sorted(props.keys())}"
        )
        required = schema.get("required") or []
        assert required != ["kwargs"], (
            f"Tool {t.name!r} requires literal `kwargs` field — wrapper bug"
        )


def test_known_tools_expose_expected_fields() -> None:
    """Spot-check a handful of tools to confirm real schemas are surfaced."""
    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
    for tool_name, expected in _EXPECTED_FIELDS.items():
        assert tool_name in tools, f"Tool {tool_name!r} missing from registry"
        props = (tools[tool_name].inputSchema or {}).get("properties", {})
        missing = expected - set(props.keys())
        assert not missing, (
            f"Tool {tool_name!r} input schema missing expected fields "
            f"{sorted(missing)}; advertises {sorted(props.keys())}"
        )


def test_call_tool_accepts_natural_argument_shape() -> None:
    """`tools/call` must accept top-level args, not require a `kwargs` envelope.

    We assert the pydantic arg-validation layer no longer rejects the
    natural shape. The tool body itself will raise (because we pass an
    invalid `tier`), but the error must come from the tool's own
    validation — not from `Field required: kwargs`.
    """
    from mcp.server.fastmcp.exceptions import ToolError

    async def _run() -> str:
        try:
            await mcp.call_tool("gecko_research", {"idea": "X", "tier": "nonsense"})
        except ToolError as exc:
            return str(exc)
        return ""

    msg = asyncio.run(_run())
    assert "kwargs" not in msg.lower() or "tier" in msg.lower(), (
        f"call_tool still rejecting on kwargs envelope: {msg!r}"
    )
    # Positive assertion: the error reached the tool body's own validation.
    assert "tier" in msg, f"Expected tier-validation error, got: {msg!r}"
