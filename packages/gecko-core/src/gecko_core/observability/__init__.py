"""S24 WS-D — structured observability + economics ledger.

Single entrypoint :func:`emit_event` writes one document to the
``gecko_events.events`` collection. The writer is fire-and-forget and
never raises — if Mongo is unset or unreachable we log a debug line and
return, so observability never takes down a hot path.

The collection (TTL 180d, indexed on ts/event/wallet) was bootstrapped by
``infra/mongo/migrations/2026-05-12-s24-agent-collections.py``.

Read API: :func:`aggregate_metrics` powers ``GET /metrics``.
"""

from __future__ import annotations

from gecko_core.observability.events import (
    EVENTS_COLLECTION,
    EVENTS_DB,
    emit_event,
    emit_event_sync,
    events_collection,
)
from gecko_core.observability.metrics import aggregate_metrics

__all__ = [
    "EVENTS_COLLECTION",
    "EVENTS_DB",
    "aggregate_metrics",
    "emit_event",
    "emit_event_sync",
    "events_collection",
]
