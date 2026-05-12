"""State store — Mongo writer + in-memory stub.

The runtime depends on the :class:`StateStore` protocol, not the Motor
implementation. Tests use :class:`InMemoryStateStore`. Production wires
:class:`MongoStateStore`.

Writes are async and idempotent on the natural keys
(agent_id; agent_id+position_id; agent_id+idea_hash). Journal writes are
append-only — no upsert, every record is a new doc.

Per the SE-1 ticket: writes are non-blocking on the hot path. The runtime
queues journal writes to a background task; this module just exposes the
primitives.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any, Protocol

from gecko_core.trade_agent.state.models import (
    AgentJournalEntry,
    AgentPosition,
    AgentState,
    AgentVerdictCacheEntry,
)

logger = logging.getLogger(__name__)

TRADE_AGENT_DB = "gecko_trade_agent"


class StateStoreError(Exception):
    """Raised on a non-recoverable state-store failure."""


class StateStore(Protocol):
    """Async writer protocol — the only surface the runtime sees."""

    async def upsert_agent_state(self, state: AgentState) -> None: ...
    async def get_agent_state(self, agent_id: str) -> AgentState | None: ...
    async def list_agent_states(
        self, *, user_wallet: str | None = None, status: str | None = None
    ) -> list[AgentState]: ...
    async def update_heartbeat(self, agent_id: str, ts: datetime) -> None: ...
    async def update_status(self, agent_id: str, status: str) -> None: ...
    async def upsert_position(self, position: AgentPosition) -> None: ...
    async def list_open_positions(self, agent_id: str) -> list[AgentPosition]: ...
    async def write_journal(self, entry: AgentJournalEntry) -> None: ...
    async def tail_journal(self, agent_id: str, limit: int = 20) -> list[AgentJournalEntry]: ...
    async def get_verdict_cache(
        self, agent_id: str, idea_hash: str
    ) -> AgentVerdictCacheEntry | None: ...
    async def upsert_verdict_cache(self, entry: AgentVerdictCacheEntry) -> None: ...


# --------------------------------------------------------------------------
# In-memory stub. Default test substrate. Mimics protocol exactly so the
# runtime can't accidentally rely on Mongo-specific behaviour.
# --------------------------------------------------------------------------


class InMemoryStateStore:
    """Test stub. All ops are O(n) — fine for unit-test workloads."""

    def __init__(self) -> None:
        self._states: dict[str, AgentState] = {}
        self._positions: dict[tuple[str, str], AgentPosition] = {}
        self._journal: list[AgentJournalEntry] = []
        self._verdict_cache: dict[tuple[str, str], AgentVerdictCacheEntry] = {}
        self._lock = asyncio.Lock()

    async def upsert_agent_state(self, state: AgentState) -> None:
        async with self._lock:
            self._states[state.agent_id] = state

    async def get_agent_state(self, agent_id: str) -> AgentState | None:
        async with self._lock:
            return self._states.get(agent_id)

    async def list_agent_states(
        self, *, user_wallet: str | None = None, status: str | None = None
    ) -> list[AgentState]:
        async with self._lock:
            out: list[AgentState] = []
            for s in self._states.values():
                if user_wallet is not None and s.user_wallet != user_wallet:
                    continue
                if status is not None and s.status != status:
                    continue
                out.append(s)
            return out

    async def update_heartbeat(self, agent_id: str, ts: datetime) -> None:
        async with self._lock:
            s = self._states.get(agent_id)
            if s is None:
                return
            s.last_heartbeat_at = ts

    async def update_status(self, agent_id: str, status: str) -> None:
        async with self._lock:
            s = self._states.get(agent_id)
            if s is None:
                return
            # mypy: status is a Literal in the model; runtime accepts the
            # full set the protocol declares. Cast via dict round-trip.
            s.status = status  # type: ignore[assignment]
            if status == "stopped":
                s.stopped_at = datetime.now(UTC)

    async def upsert_position(self, position: AgentPosition) -> None:
        async with self._lock:
            self._positions[(position.agent_id, position.position_id)] = position

    async def list_open_positions(self, agent_id: str) -> list[AgentPosition]:
        async with self._lock:
            return [
                p
                for (aid, _), p in self._positions.items()
                if aid == agent_id and p.status == "open"
            ]

    async def write_journal(self, entry: AgentJournalEntry) -> None:
        async with self._lock:
            self._journal.append(entry)

    async def tail_journal(self, agent_id: str, limit: int = 20) -> list[AgentJournalEntry]:
        async with self._lock:
            filtered = [e for e in self._journal if e.agent_id == agent_id]
            return filtered[-limit:][::-1]

    async def get_verdict_cache(
        self, agent_id: str, idea_hash: str
    ) -> AgentVerdictCacheEntry | None:
        async with self._lock:
            return self._verdict_cache.get((agent_id, idea_hash))

    async def upsert_verdict_cache(self, entry: AgentVerdictCacheEntry) -> None:
        async with self._lock:
            self._verdict_cache[(entry.agent_id, entry.idea_hash)] = entry


# --------------------------------------------------------------------------
# Mongo implementation. Imported only when MONGODB_URI is configured; the
# Motor import sits inside the constructor so dev environments without
# Mongo can still run tests.
# --------------------------------------------------------------------------


class MongoStateStore:
    """Motor-backed writer for production runtime."""

    def __init__(self, mongo_uri: str, *, db_name: str = TRADE_AGENT_DB) -> None:
        try:
            from motor.motor_asyncio import AsyncIOMotorClient
        except ImportError as exc:  # pragma: no cover
            raise StateStoreError("motor not installed") from exc
        self._client = AsyncIOMotorClient(mongo_uri)
        self._db = self._client[db_name]

    async def upsert_agent_state(self, state: AgentState) -> None:
        payload = state.model_dump()
        await self._db["agent_state"].update_one(
            {"agent_id": state.agent_id}, {"$set": payload}, upsert=True
        )

    async def get_agent_state(self, agent_id: str) -> AgentState | None:
        doc = await self._db["agent_state"].find_one({"agent_id": agent_id})
        if doc is None:
            return None
        doc.pop("_id", None)
        return AgentState.model_validate(doc)

    async def list_agent_states(
        self, *, user_wallet: str | None = None, status: str | None = None
    ) -> list[AgentState]:
        query: dict[str, Any] = {}
        if user_wallet is not None:
            query["user_wallet"] = user_wallet
        if status is not None:
            query["status"] = status
        cursor = self._db["agent_state"].find(query)
        out: list[AgentState] = []
        async for doc in cursor:
            doc.pop("_id", None)
            out.append(AgentState.model_validate(doc))
        return out

    async def update_heartbeat(self, agent_id: str, ts: datetime) -> None:
        await self._db["agent_state"].update_one(
            {"agent_id": agent_id}, {"$set": {"last_heartbeat_at": ts}}
        )

    async def update_status(self, agent_id: str, status: str) -> None:
        update: dict[str, Any] = {"status": status}
        if status == "stopped":
            update["stopped_at"] = datetime.now(UTC)
        await self._db["agent_state"].update_one({"agent_id": agent_id}, {"$set": update})

    async def upsert_position(self, position: AgentPosition) -> None:
        payload = position.model_dump()
        await self._db["agent_positions"].update_one(
            {"agent_id": position.agent_id, "position_id": position.position_id},
            {"$set": payload},
            upsert=True,
        )

    async def list_open_positions(self, agent_id: str) -> list[AgentPosition]:
        cursor = self._db["agent_positions"].find({"agent_id": agent_id, "status": "open"})
        out: list[AgentPosition] = []
        async for doc in cursor:
            doc.pop("_id", None)
            out.append(AgentPosition.model_validate(doc))
        return out

    async def write_journal(self, entry: AgentJournalEntry) -> None:
        await self._db["agent_journal"].insert_one(entry.model_dump())

    async def tail_journal(self, agent_id: str, limit: int = 20) -> list[AgentJournalEntry]:
        cursor = self._db["agent_journal"].find({"agent_id": agent_id}).sort("ts", -1).limit(limit)
        out: list[AgentJournalEntry] = []
        async for doc in cursor:
            doc.pop("_id", None)
            out.append(AgentJournalEntry.model_validate(doc))
        return out

    async def get_verdict_cache(
        self, agent_id: str, idea_hash: str
    ) -> AgentVerdictCacheEntry | None:
        doc = await self._db["agent_verdict_cache"].find_one(
            {"agent_id": agent_id, "idea_hash": idea_hash}
        )
        if doc is None:
            return None
        doc.pop("_id", None)
        return AgentVerdictCacheEntry.model_validate(doc)

    async def upsert_verdict_cache(self, entry: AgentVerdictCacheEntry) -> None:
        payload = entry.model_dump()
        await self._db["agent_verdict_cache"].update_one(
            {"agent_id": entry.agent_id, "idea_hash": entry.idea_hash},
            {"$set": payload},
            upsert=True,
        )


__all__ = [
    "TRADE_AGENT_DB",
    "InMemoryStateStore",
    "MongoStateStore",
    "StateStore",
    "StateStoreError",
]
