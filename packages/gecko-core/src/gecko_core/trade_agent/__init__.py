"""gecko-trade-agent runtime package.

Public surface:

* :class:`AgentRuntime` / :class:`TradeAgent` — the async runtime spine.
* :class:`AgentSpec` + :func:`load_spec` — coach-emitted strategy validation.
* :class:`AgentState` and friends — Mongo-shaped state records.

Hot-path data clients live under :mod:`.hotpath` (owned by web3-engineer
in a parallel ticket); the runtime imports them by protocol.
"""

from gecko_core.trade_agent.runtime import AgentRuntime, TradeAgent
from gecko_core.trade_agent.spec import AgentSpec, SpecValidationError, load_spec
from gecko_core.trade_agent.state import AgentState

__all__ = [
    "AgentRuntime",
    "AgentSpec",
    "AgentState",
    "SpecValidationError",
    "TradeAgent",
    "load_spec",
]
