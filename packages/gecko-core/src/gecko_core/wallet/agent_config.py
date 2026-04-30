"""Read frames.ag AgentWallet credentials from the canonical config path.

Extracted from ``gecko_mcp.wallet._read_config`` (S9-DOCTOR-01) so doctor's
``frames.ag wallet`` row and the MCP wallet facade share one parsing path.

The apiToken is treated as a password: never logged, echoed, or returned in
error messages. Callers that want to surface presence should mask it via the
existing ``_mask`` helper in `gecko_cli.commands.doctor`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = Path.home() / ".agentwallet" / "config.json"


class AgentConfigError(RuntimeError):
    """Raised when the on-disk frames.ag config exists but is malformed."""


def read_agent_config(path: Path | None = None) -> dict[str, Any] | None:
    """Return the parsed frames.ag config dict, or ``None`` if absent.

    Returns ``None`` (not raising) when the file simply doesn't exist —
    common on fresh installs that haven't run the frames.ag connect skill.
    Raises :class:`AgentConfigError` only when the file is present but
    unreadable / not JSON, so callers can distinguish "not configured" from
    "configured but broken".
    """
    p = path or DEFAULT_CONFIG_PATH
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise AgentConfigError(f"frames.ag config at {p} is unreadable: {exc}") from exc
    if not isinstance(data, dict):
        raise AgentConfigError(f"frames.ag config at {p} is not a JSON object")
    return data


def read_agent_token(path: Path | None = None) -> str | None:
    """Return the apiToken from the frames.ag config, or ``None`` if absent.

    Returns ``None`` when the file is missing, the apiToken key is missing,
    or the value is empty. Never raises on a missing file — the typical
    "not connected yet" case must not crash callers like ``bb doctor``.
    """
    try:
        cfg = read_agent_config(path)
    except AgentConfigError:
        return None
    if not cfg:
        return None
    token = cfg.get("apiToken")
    if not isinstance(token, str) or not token:
        return None
    return token


__all__ = [
    "DEFAULT_CONFIG_PATH",
    "AgentConfigError",
    "read_agent_config",
    "read_agent_token",
]
