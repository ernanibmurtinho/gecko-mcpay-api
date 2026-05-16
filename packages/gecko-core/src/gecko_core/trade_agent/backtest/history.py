"""History sources for the backtest harness.

A :class:`HistorySource` yields hot-path-shaped events in chronological
order. The harness consumes them like the live runtime consumes a
Helius / Pyth subscription — same primitive call shape, deterministic
ordering.

v0.1 ships two implementations + one stub:

* :class:`FixtureHistorySource` — reads recorded JSON from
  ``tests/fixtures/backtest/``. Deterministic; the test default.
* :class:`MongoHotpathHistorySource` — replays a window of the
  ``agent_hotpath_snapshot`` collection. Opt-in via ``--source mongo``;
  requires ``MONGODB_URI``.
* :class:`PythHistoricalHistorySource` — stub for the Hermes historical
  endpoint. Raises ``NotImplementedError`` if invoked. Wired so future
  work has a one-place-to-fill landing pad.

Event shape (matches the runtime's loose hot-path payload):
``{"ts": float, "mint": str, "price": float, "kind": str, ...}``
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterable
from pathlib import Path
from typing import Any, Protocol


class HistorySource(Protocol):
    """An async iterable of hot-path events in chronological order."""

    def __aiter__(self) -> AsyncIterator[dict[str, Any]]: ...


class FixtureHistorySource:
    """Reads a JSON fixture and yields its events in order.

    Two accepted JSON shapes:

    1. ``{"events": [{"ts": ..., "mint": ..., ...}, ...]}``
    2. ``{"prices": [{"ts": ..., "price": ...}, ...], "mint": "..."}``
       — convenience: a flat price series is upgraded to events with
       ``kind=tick`` and reasonable defaults (drawdown_pct derived from
       running peak, momentum left at zero).
    """

    def __init__(self, path: Path | str | None = None, *, data: dict[str, Any] | None = None):
        if data is not None:
            self._data = data
        elif path is not None:
            self._data = json.loads(Path(path).read_text(encoding="utf-8"))
        else:
            raise ValueError("FixtureHistorySource requires path or data")
        self._events = list(self._expand(self._data))

    @staticmethod
    def _expand(data: dict[str, Any]) -> Iterable[dict[str, Any]]:
        if "events" in data:
            for ev in data["events"]:
                yield dict(ev)
            return
        if "prices" in data:
            mint = data.get("mint", "FIXTURE_MINT")
            peak = 0.0
            for row in data["prices"]:
                price = float(row["price"])
                if price > peak:
                    peak = price
                drawdown_pct = 0.0 if peak <= 0 else (peak - price) / peak * 100
                yield {
                    "ts": float(row["ts"]),
                    "mint": mint,
                    "price": price,
                    "kind": row.get("kind", "tick"),
                    "drawdown_pct": drawdown_pct,
                    "momentum": float(row.get("momentum", 0.0)),
                    "session_high": peak,
                }
            return
        raise ValueError("FixtureHistorySource: data must contain 'events' or 'prices'")

    @property
    def events(self) -> list[dict[str, Any]]:
        """Materialised event list (deterministic order)."""
        return list(self._events)

    async def __aiter__(self) -> AsyncIterator[dict[str, Any]]:
        for ev in self._events:
            yield dict(ev)


class MongoHotpathHistorySource:
    """Replay events from the ``agent_hotpath_snapshot`` collection.

    Opt-in source — requires Mongo wired. Kept thin: pulls all snapshot
    docs in the window and yields them. Tests do NOT exercise this
    branch (mark live-only).
    """

    def __init__(
        self,
        *,
        agent_id: str,
        window_start_ts: float,
        window_end_ts: float,
        client: Any | None = None,
    ) -> None:
        self.agent_id = agent_id
        self.window_start_ts = window_start_ts
        self.window_end_ts = window_end_ts
        self._client = client

    async def __aiter__(self) -> AsyncIterator[dict[str, Any]]:
        if self._client is None:
            raise RuntimeError(
                "MongoHotpathHistorySource requires a live Mongo client; "
                "use FixtureHistorySource for tests."
            )
        coll = self._client["gecko"]["agent_hotpath_snapshot"]
        cursor = coll.find(
            {
                "agent_id": self.agent_id,
                "ts": {"$gte": self.window_start_ts, "$lte": self.window_end_ts},
            }
        ).sort("ts", 1)
        async for doc in cursor:
            doc.pop("_id", None)
            yield doc


class PythHistoricalHistorySource:
    """Stub for Hermes historical OHLC. Not implemented in v0.1."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._args = args
        self._kwargs = kwargs

    async def __aiter__(self) -> AsyncIterator[dict[str, Any]]:
        raise NotImplementedError(
            "PythHistoricalHistorySource is a stub. Wire the Hermes "
            "historical endpoint when needed; do not call live."
        )
        # Make this a generator function so the protocol is satisfied
        # even if the raise above is removed.
        yield  # pragma: no cover


__all__ = [
    "FixtureHistorySource",
    "HistorySource",
    "MongoHotpathHistorySource",
    "PythHistoricalHistorySource",
]
