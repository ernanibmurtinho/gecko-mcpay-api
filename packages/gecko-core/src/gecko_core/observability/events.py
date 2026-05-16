"""Global structured event emission to ``gecko_events.events``.

Design contract (S24 WS-D):
* Fire-and-forget. ``emit_event`` MUST NOT raise on Mongo failure — the
  hot path (verdict served, x402 settle, oracle cache hit) does not stop
  because the ledger is down.
* No-op when MONGODB_URI is unset. Local dev + stub-mode CI never need
  Mongo to be live; tests that assert event shape inject an in-memory
  client via :func:`set_events_collection_override`.
* Async-first. Production callers (FastAPI handlers, async runtime) hit
  the async API; the sync wrapper exists for the few sync Click paths.

The Mongo write itself is intentionally minimal (single insert, no read
roundtrip, no index touch) so a degraded Mongo doesn't backpressure
verdict latency.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

from gecko_core.types import EventKind

logger = logging.getLogger(__name__)

try:
    from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorCollection
except ImportError:  # pragma: no cover - motor is in deps; guard stripped envs
    AsyncIOMotorClient = None  # type: ignore[assignment,misc]
    AsyncIOMotorCollection = Any  # type: ignore[assignment,misc]


EVENTS_DB = "gecko_events"
EVENTS_COLLECTION = "events"


def _mongo_uri() -> str | None:
    uri = os.environ.get("MONGODB_URI") or os.environ.get("MONGO_URI")
    if not uri or uri == "__unset__":
        return None
    return uri


@lru_cache(maxsize=1)
def _client() -> AsyncIOMotorClient | None:  # type: ignore[type-arg]
    uri = _mongo_uri()
    if not uri or AsyncIOMotorClient is None:
        return None
    return AsyncIOMotorClient(uri)


_OVERRIDE_COLLECTION: AsyncIOMotorCollection | None = None  # type: ignore[type-arg]


def set_events_collection_override(coll: Any | None) -> None:
    """Test seam. Inject an in-memory / fake collection. ``None`` clears."""
    global _OVERRIDE_COLLECTION
    _OVERRIDE_COLLECTION = coll
    # Reset the lru-cached real client too so a test setting MONGODB_URI
    # mid-run gets a fresh connection.
    _client.cache_clear()


def events_collection() -> Any | None:
    """Return the active events collection, or ``None`` if unconfigured.

    Honours the test override first; falls back to the real Motor client.
    """
    if _OVERRIDE_COLLECTION is not None:
        return _OVERRIDE_COLLECTION
    client = _client()
    if client is None:
        return None
    return client[EVENTS_DB][EVENTS_COLLECTION]


async def emit_event(
    event: EventKind,
    payload: dict[str, Any] | None = None,
    *,
    wallet: str | None = None,
    ts: datetime | None = None,
) -> None:
    """Append one event to ``gecko_events.events``. Never raises.

    Args:
        event: closed-vocabulary EventKind literal.
        payload: free-form structured payload. Caller owns the shape.
        wallet: optional wallet address — indexed sparsely so wallet-filtered
            queries are cheap, but events without a wallet (e.g. system
            errors) don't bloat the index.
        ts: override timestamp; defaults to ``datetime.now(UTC)``. Tests
            pin this to assert against deterministic windows.
    """
    coll = events_collection()
    if coll is None:
        logger.debug("emit_event skipped (mongo unconfigured) event=%s", event)
        return
    doc: dict[str, Any] = {
        "ts": ts or datetime.now(UTC),
        "event": event,
        "wallet": wallet,
        "payload": payload or {},
    }
    try:
        await coll.insert_one(doc)
    except Exception as exc:  # never raise on the hot path
        logger.warning("emit_event failed event=%s err=%s", event, exc)


def emit_event_sync(
    event: EventKind,
    payload: dict[str, Any] | None = None,
    *,
    wallet: str | None = None,
) -> None:
    """Best-effort sync wrapper. Schedules a task on the running loop if
    there is one, otherwise runs to completion. Used by the few sync Click
    paths (doctor probe). Never raises.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        loop.create_task(emit_event(event, payload, wallet=wallet))
        return
    try:
        asyncio.run(emit_event(event, payload, wallet=wallet))
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("emit_event_sync failed event=%s err=%s", event, exc)


__all__ = [
    "EVENTS_COLLECTION",
    "EVENTS_DB",
    "emit_event",
    "emit_event_sync",
    "events_collection",
    "set_events_collection_override",
]
