"""Schema-drift guard for SessionStatus (Pattern A).

The Python `SessionStatus` Literal in `gecko_core.models` and the SQL
`sessions_status_check` CHECK constraint must agree exactly. Adding a
new value (e.g. `interrupted` in the 2026-05-06 ECS-shutdown ticket)
requires:

  1. Touch `gecko_core.models.SessionStatus`.
  2. Add a migration that drops + re-creates the CHECK with the new set.

If the two drift, sessions land at runtime with PostgreSQL error 23514
("new row violates check constraint") — exactly the failure mode
`test_payment_mode_consistency.py` was written to prevent. This test
replicates that pattern for status.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import get_args

from gecko_core.models import SessionStatus

_REPO_ROOT = Path(__file__).resolve().parents[3]
_MIGRATIONS_DIR = _REPO_ROOT / "infra" / "supabase" / "migrations"


def _latest_status_check_values() -> tuple[str, ...]:
    """Return the set of values from the most recent migration that
    defines a CHECK on sessions.status."""
    pattern = re.compile(
        r"status\s+IN\s*\(\s*((?:'[a-z_]+'(?:\s*,\s*)?)+)\s*\)",
        re.IGNORECASE,
    )
    last_values: tuple[str, ...] | None = None
    for path in sorted(_MIGRATIONS_DIR.glob("*.sql")):
        sql = path.read_text(encoding="utf-8")
        for raw in pattern.findall(sql):
            last_values = tuple(v.strip().strip("'") for v in raw.split(","))
    if last_values is None:  # pragma: no cover — migrations directory would be broken
        raise RuntimeError("no sessions.status CHECK constraint found in migrations/")
    return last_values


def test_canonical_status_values() -> None:
    """Lock the canonical list. Adding/removing a status forces a deliberate
    update here, which prompts the developer to also write the migration."""
    assert set(get_args(SessionStatus)) == {
        "pending",
        "indexing",
        "generating",
        "complete",
        "failed",
        "interrupted",
    }


def test_sql_check_matches_session_status_literal() -> None:
    """The active SQL CHECK on sessions.status must match the Python Literal."""
    sql_values = _latest_status_check_values()
    assert set(sql_values) == set(get_args(SessionStatus)), (
        f"SQL CHECK values {sql_values} drifted from SessionStatus "
        f"{get_args(SessionStatus)}. Add a new migration that updates "
        "sessions_status_check, or update models.py if SQL is right."
    )
