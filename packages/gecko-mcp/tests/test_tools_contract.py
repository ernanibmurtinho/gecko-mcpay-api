"""Tools-list contract test (S22-MCP-HOST-04).

The skill manifest at `app.geckovision.tech/skill.md` pins the (name,
description, inputSchema) triple for every tool the hosted MCP exposes.
Any drift in those fields silently breaks installs across Claude Desktop,
Cursor, and Manus — `tools/list` is the wire contract.

This test snapshots the FastMCP tool registry to a fixture and fails red
on any mismatch. To intentionally change a tool surface:
    1. Bump `gecko-mcp` version in pyproject.toml.
    2. Regenerate the fixture (see `_recompute_snapshot` below).
    3. Coordinate with `product-designer` to update skill.md.

Replicates the discipline of `tests/test_payment_mode_consistency.py`:
single source of truth, mechanical drift detection.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from gecko_mcp.server import mcp

FIXTURE = Path(__file__).parent / "fixtures" / "tools_contract_2026_05_07.json"


def _snapshot_for_tool(name: str, description: str, parameters: dict[str, Any]) -> dict[str, str]:
    schema_canon = json.dumps(parameters, sort_keys=True, ensure_ascii=False)
    return {
        "name": name,
        "description_hash": hashlib.sha256(description.encode("utf-8")).hexdigest(),
        "input_schema_hash": hashlib.sha256(schema_canon.encode("utf-8")).hexdigest(),
    }


def _current_snapshot() -> list[dict[str, str]]:
    tools = mcp._tool_manager.list_tools()
    rows = [_snapshot_for_tool(t.name, t.description or "", t.parameters or {}) for t in tools]
    return sorted(rows, key=lambda r: r["name"])


def test_tools_contract_matches_fixture() -> None:
    """Mechanical drift detector for the public tools/list surface."""
    expected = json.loads(FIXTURE.read_text())
    actual = _current_snapshot()
    assert actual == expected, (
        "Tools contract drifted. Either revert the change or bump the "
        "gecko-mcp version, regenerate the fixture, and coordinate the "
        "skill manifest update.\n"
        f"Expected names: {[r['name'] for r in expected]}\n"
        f"Actual names:   {[r['name'] for r in actual]}"
    )


def test_tools_contract_count() -> None:
    """Belt-and-braces: catch tool additions/removals before hash diff."""
    expected = json.loads(FIXTURE.read_text())
    actual = _current_snapshot()
    assert len(actual) == len(expected), (
        f"Tool count changed: fixture has {len(expected)}, server has {len(actual)}."
    )
