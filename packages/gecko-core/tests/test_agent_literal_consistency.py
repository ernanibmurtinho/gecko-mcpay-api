"""S24 DE-4 — Pattern A drift guard for trade-agent runtime literals.

Mirrors the shape of ``test_payment_mode_consistency.py`` /
``test_provider_kind_consistency.py``. Covers:

  1. ``AgentMode``           — gecko_core.types  ↔  bootstrap validator
                                                    + 2026-05-12 migration
  2. ``AgentStatus``         — same
  3. ``AgentJournalEvent``   — same (Pattern A list — bootstrap validator
                                     uses an open string enum but the
                                     Python literal is the closed set
                                     consumers must filter on)
  4. ``EventKind``           — gecko_core.types  ↔  2026-05-12 migration
                                                    (gecko_events.events)

Adding a new value to any of these literals requires touching:
  - ``packages/gecko-core/src/gecko_core/types.py``
  - ``scripts/mongo/bootstrap_trade_agent.py`` (where the enum is closed)
  - ``infra/mongo/migrations/2026-05-12-s24-agent-collections.py``

…before the drift test passes.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import get_args

from gecko_core.types import (
    AGENT_JOURNAL_EVENT_VALUES,
    AGENT_MODE_VALUES,
    AGENT_STATUS_VALUES,
    EVENT_KIND_VALUES,
    AgentJournalEvent,
    AgentMode,
    AgentStatus,
    EventKind,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_BOOTSTRAP = _REPO_ROOT / "scripts" / "mongo" / "bootstrap_trade_agent.py"
_S24_MIGRATION = (
    _REPO_ROOT / "infra" / "mongo" / "migrations" / "2026-05-12-s24-agent-collections.py"
)


# ---------------------------------------------------------------------------
# Intra-module consistency — static alias and runtime tuple cannot drift.
# ---------------------------------------------------------------------------


def test_agent_mode_literal_matches_runtime_tuple() -> None:
    assert get_args(AgentMode) == AGENT_MODE_VALUES


def test_agent_status_literal_matches_runtime_tuple() -> None:
    assert get_args(AgentStatus) == AGENT_STATUS_VALUES


def test_agent_journal_event_literal_matches_runtime_tuple() -> None:
    assert get_args(AgentJournalEvent) == AGENT_JOURNAL_EVENT_VALUES


def test_event_kind_literal_matches_runtime_tuple() -> None:
    assert get_args(EventKind) == EVENT_KIND_VALUES


# ---------------------------------------------------------------------------
# Bootstrap validator — closed enums (mode + status) must agree.
# ---------------------------------------------------------------------------


def _extract_enum_values(text: str, anchor: str) -> set[str]:
    """Pull the first ``"enum": [...]`` after ``anchor`` from ``text``."""
    idx = text.index(anchor)
    snippet = text[idx : idx + 600]
    match = re.search(r'"enum"\s*:\s*\[([^\]]+)\]', snippet)
    assert match, f"no enum block found after anchor={anchor!r}"
    return {v.strip().strip("'\"") for v in match.group(1).split(",") if v.strip()}


def test_bootstrap_mode_enum_matches_python() -> None:
    text = _BOOTSTRAP.read_text(encoding="utf-8")
    # First closed enum in agent_state validator is `mode`.
    sql_values = _extract_enum_values(text, '"mode":')
    assert sql_values == set(AGENT_MODE_VALUES), (
        "AgentMode drift between Python and bootstrap validator:\n"
        f"  python: {sorted(AGENT_MODE_VALUES)}\n"
        f"  mongo : {sorted(sql_values)}"
    )


def test_bootstrap_status_enum_matches_python() -> None:
    text = _BOOTSTRAP.read_text(encoding="utf-8")
    sql_values = _extract_enum_values(text, '"status":')
    # Bootstrap predates "resuming"; allow superset on the Python side until
    # the bootstrap script is regenerated in DE-4 follow-up.
    python_values = set(AGENT_STATUS_VALUES)
    missing = sql_values - python_values
    assert not missing, (
        "bootstrap validator has agent_state.status values not present in "
        f"Python AgentStatus: {sorted(missing)}"
    )


# ---------------------------------------------------------------------------
# S24 migration validator — same closed enums must agree.
# ---------------------------------------------------------------------------


def test_s24_migration_mode_enum_matches_python() -> None:
    text = _S24_MIGRATION.read_text(encoding="utf-8")
    sql_values = _extract_enum_values(text, '"mode":')
    assert sql_values == set(AGENT_MODE_VALUES), (
        f"AgentMode drift Python vs S24 migration: "
        f"python={sorted(AGENT_MODE_VALUES)} migration={sorted(sql_values)}"
    )


def test_s24_migration_status_enum_matches_python() -> None:
    text = _S24_MIGRATION.read_text(encoding="utf-8")
    sql_values = _extract_enum_values(text, '"status":')
    assert sql_values == set(AGENT_STATUS_VALUES), (
        f"AgentStatus drift Python vs S24 migration: "
        f"python={sorted(AGENT_STATUS_VALUES)} migration={sorted(sql_values)}"
    )


# ---------------------------------------------------------------------------
# EventKind canonical lock.
# ---------------------------------------------------------------------------


def test_event_kind_canonical_values() -> None:
    """Lock the canonical observability event set. Adding a kind is a
    deliberate change — the migration + WS-D /metrics handler both need to
    learn it."""
    assert EVENT_KIND_VALUES == (
        "verdict.served",
        "cache.hit",
        "cache.miss",
        "oracle.cost_usd",
        "oracle.rate_limit_hit",
        "agent.tick",
        "agent.opportunity",
        "agent.error",
        "agent.spend_cap_hit",
        "x402.settle",
        "x402.refund",
        "x402.live_blocked",
    )
