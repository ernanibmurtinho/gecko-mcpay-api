"""Hot-path data clients for the trade-agent runtime.

Three primitives:

* :class:`HotpathCache` — async-safe, in-process TTL cache. The only
  read surface for trader-mode entry-gate decisions; ``chunks`` is never
  on the hot path.
* :class:`HeliusWebSocketClient` — Solana account/program subscription
  manager with exponential backoff + transparent re-subscribe and a
  polling fallback when the websocket stays down.
* :class:`PythHermesClient` — Hermes SSE price-feed client.

W3-1 + W3-2 in `docs/strategy/2026-05-11-trade-vertical-expansion.md` §7.
"""

from .cache import HotpathCache
from .helius import HeliusWebSocketClient
from .pyth import PriceUpdate, PythHermesClient

__all__ = [
    "HeliusWebSocketClient",
    "HotpathCache",
    "PriceUpdate",
    "PythHermesClient",
]
