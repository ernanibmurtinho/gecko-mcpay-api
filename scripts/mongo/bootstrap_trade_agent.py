"""DEPRECATED — superseded by ``infra/mongo/migrations/2026-05-12-s24-agent-collections.py``.

S24 WS-A (data-engineer) introduced a single canonical Mongo migration
that owns collection + index + validator creation for both the
``gecko_trade_agent`` and ``gecko_events`` databases. The schema is the
contract; the migration is the only writer.

The migration differs from this script in three ways that matter:

1. Collections — drops ``agent_positions`` + ``agent_verdict_cache``
   (positions land with trader-mode in v0.2; verdict cache moved into the
   ``OracleWrapper`` in-process LRU + ``gecko_cache.verdicts``). Adds
   ``events`` (cross-cutting telemetry, lives in ``gecko_events``).
2. ``agent_state`` schema — adds ``spec_name``, ``journal_count``,
   ``last_tick_at``. ``status_last_tick_desc`` compound index supports
   the observability "stale agents" probe.
3. ``agent_journal`` schema — required field is ``event_type``
   (closed AgentJournalEvent literal) rather than ``event``. The
   :class:`MongoStateStore` writer mirrors both names on insert so the
   Pydantic model + tests stay backward compatible.
4. ``agent_hotpath_snapshot`` schema — keys on (``agent_id``,
   ``protocol``) with ``as_of`` TTL 5min, replacing the prior
   (``agent_id``, ``mint``, ``seen_at``) shape. Reflects the move from
   per-mint warm cache to per-protocol snapshot for v0.1's protocol-level
   strategies.

Running this script is a no-op that exits with code 0 + a deprecation
warning. Use::

    python -m infra.mongo.migrations.2026-05-12-s24-agent-collections --verbose

(or whatever the migration runner wires up) instead.
"""

from __future__ import annotations

import sys

_MESSAGE = (
    "[deprecated] scripts/mongo/bootstrap_trade_agent.py is retired. "
    "Use infra/mongo/migrations/2026-05-12-s24-agent-collections.py — "
    "it owns the canonical agent_state / agent_journal / "
    "agent_hotpath_snapshot / events schema for S24+."
)


def main() -> int:
    print(_MESSAGE, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
