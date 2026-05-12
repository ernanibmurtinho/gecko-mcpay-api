"""Canonical home for cross-cutting ``Literal`` enums (Pattern A).

Per CLAUDE.md project conventions: "single source of truth for shared
Literal types". Every consumer imports from here — never redeclare.

Today this module is the canonical home for the trade-agent runtime
literals (S24 DE-4) plus the global observability ``EventKind`` (S24
WS-D). The longer-standing ``ProviderKind`` / ``FreshnessTier`` /
``ContentKind`` literals live in :mod:`gecko_core.sources.types` for
historical reasons — they are re-exported from here so new consumers
have one obvious import surface.

Adding a new value to any literal in this module is a Pattern A change.
Walk every:

  1. Mongo bootstrap validator (``scripts/mongo/bootstrap_trade_agent.py``)
  2. Migration script(s) under ``infra/mongo/migrations/``
  3. Drift test in ``tests/test_literal_consistency.py``

…before merging. The drift test fails loudly when any of these disagree.
"""

from __future__ import annotations

from typing import Final, Literal, get_args

# Re-export the existing canon for one-stop imports.
from gecko_core.sources.types import (
    CONTENT_KIND_VALUES,
    FRESHNESS_TIER_VALUES,
    PROVIDER_KINDS,
    ContentKind,
    FreshnessTier,
    ProviderKind,
)

# ---------------------------------------------------------------------------
# Trade-agent runtime literals (S24 DE-4).
#
# Mirrored by:
#   - Validator blocks in scripts/mongo/bootstrap_trade_agent.py
#   - infra/mongo/migrations/2026-05-12-s24-agent-collections.py
#   - tests/test_literal_consistency.py (drift guard)
#
# The trade_agent.state.models module re-exports these aliases so existing
# code paths (``from gecko_core.trade_agent.state import AgentStatus``)
# keep working. New code should import from gecko_core.types directly.
# ---------------------------------------------------------------------------

AgentMode = Literal["advisor", "trader"]
"""Runtime mode. v0.1 ships advisor-only; trader lands v0.2.
See docs/strategy/2026-05-11-trade-vertical-expansion.md §3."""

AGENT_MODE_VALUES: Final[tuple[AgentMode, ...]] = get_args(AgentMode)

AgentStatus = Literal[
    "starting",
    "running",
    "paused",
    "stopped",
    "halted",
    "resuming",
]
"""Lifecycle status of a trade-agent process.

  starting  — bootstrap, oracle pro-tier pre-warm in flight
  running   — ticking on cadence + triggers
  paused    — user-paused; spec retained, no ticks
  stopped   — user-stopped or graceful shutdown; final state
  halted    — circuit-breaker tripped; awaiting human acknowledge
  resuming  — transition state mid-restart (cold-start state hydrate)
"""

AGENT_STATUS_VALUES: Final[tuple[AgentStatus, ...]] = get_args(AgentStatus)

AgentJournalEvent = Literal[
    "agent_started",
    "agent_stopped",
    "agent_paused",
    "agent_resumed",
    "opportunity",
    "opportunity_journaled",
    "entry",
    "exit",
    "verdict_called",
    "verdict_cache_hit",
    "circuit_breaker_trip",
    "halted",
    "paused",
    "resumed",
    "stopped",
    "spec_swap",
    "heartbeat_stale",
    "exec_error",
]
"""Append-only event types written to ``agent_journal``.

Free-form ``payload`` carries event-specific structure. The closed-vocabulary
``event`` token gates downstream observability filters; adding a new value
must be paired with the bootstrap validator update (Pattern A)."""

AGENT_JOURNAL_EVENT_VALUES: Final[tuple[AgentJournalEvent, ...]] = get_args(AgentJournalEvent)

# ---------------------------------------------------------------------------
# Global structured observability events (S24 WS-D).
#
# Distinct from AgentJournalEvent: per-agent journal is per-agent
# narrative; EventKind is global, cross-cutting telemetry that powers
# /metrics, cost ledger, and the WS-D cache-hit-ratio alert.
# ---------------------------------------------------------------------------

EventKind = Literal[
    "verdict.served",
    "cache.hit",
    "cache.miss",
    "oracle.cost_usd",
    "agent.tick",
    "agent.opportunity",
    "agent.error",
    "x402.settle",
    "x402.refund",
    "x402.live_blocked",
]
"""Cross-cutting event taxonomy written to the global ``events``
collection (TTL 180d). Per docs/strategy/2026-05-12-s24-plan.md §4 WS-D."""

EVENT_KIND_VALUES: Final[tuple[EventKind, ...]] = get_args(EventKind)


__all__ = [
    "AGENT_JOURNAL_EVENT_VALUES",
    "AGENT_MODE_VALUES",
    "AGENT_STATUS_VALUES",
    "CONTENT_KIND_VALUES",
    "EVENT_KIND_VALUES",
    "FRESHNESS_TIER_VALUES",
    "PROVIDER_KINDS",
    "AgentJournalEvent",
    "AgentMode",
    "AgentStatus",
    "ContentKind",
    "EventKind",
    "FreshnessTier",
    "ProviderKind",
]
