"""Wallet helpers shared across CLI / MCP / API.

The frames.ag AgentWallet apiToken is cached at ``~/.agentwallet/config.json``
by frames.ag's connect skill. Several call sites (gecko-mcp's wallet facade,
the bb doctor's frames.ag check) need to read that token; this package is the
single source of truth so we don't duplicate parsing logic.
"""

from gecko_core.wallet.agent_config import (
    DEFAULT_CONFIG_PATH,
    AgentConfigError,
    read_agent_config,
    read_agent_token,
)

__all__ = [
    "DEFAULT_CONFIG_PATH",
    "AgentConfigError",
    "read_agent_config",
    "read_agent_token",
]
