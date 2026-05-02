"""S14-TEST-POLICY-02 — schema-drift policy generalized.

Extends the S12.5-TEST-02 ``PaymentMode`` shape to every shared ``Literal``
type that has a SQL-side mirror in
``infra/supabase/migrations/``. For each one we walk the migrations
directory in name order, find the latest ``CHECK (<column> IN (...))``
clause that targets the relevant column, parse the value tuple, and
assert it equals the canonical Python ``Literal``.

The covered enums (S14):

  * ``Tier``         — ``sessions.tier``           — basic/pro
  * ``SessionStatus``— ``sessions.status``         — pending/indexing/generating/complete/failed
  * ``Verdict``      — ``gecko_precedent.verdict`` — legacy ship/kill/pivot
  * ``PaymentMode``  — ``sessions.payment_mode``   — stub/live/frames/cdp
  * ``SessionPhase`` — ``sessions.phase``          — pre_product/during_build/ongoing
  * ``ProEventType`` — ``pro_events.event_type``   — turn_start/turn_end/final/error

Notes on scope:

  * ``PaymentMode`` is double-covered here AND in
    ``test_payment_mode_consistency.py`` — that's intentional. The older
    test asserts five parallel Python definitions agree with each other;
    this one asserts every shared Literal that has a SQL mirror agrees
    with its CHECK constraint, so adding a new enum doesn't require also
    remembering to copy the older test's shape.
  * The new ``models.Verdict`` enum (PIVOT/REFINE/GO per S11+S17) does NOT
    yet have a SQL mirror — it lives only in the orchestration layer's
    headline output. We only schema-drift-test the legacy
    ``sessions.store.Verdict`` (ship/kill/pivot) which is the one
    persisted in ``gecko_precedent``. When S15 adds a SQL column for the
    new verdict shape, add an entry below.

Deliberate-divergence test: ``test_divergence_is_detected`` fabricates a
fake "Python enum" / "SQL CHECK" pair where the Python side has a value
the SQL side lacks, and asserts the same comparator function flags it.
That guarantees the comparator catches a real drift and isn't a no-op
that always passes.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import get_args

import pytest
from gecko_core.models import SessionStatus, Tier
from gecko_core.payments.modes import PAYMENT_MODES
from gecko_core.sessions.store import (
    PaymentMode,
    ProEventType,
    SessionPhase,
    Verdict,
)

# The migrations dir is at <repo>/infra/supabase/migrations/.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_MIGRATIONS_DIR = _REPO_ROOT / "infra" / "supabase" / "migrations"


# ---------------------------------------------------------------------------
# Generic CHECK-constraint scanner. Reused for every enum below.
# ---------------------------------------------------------------------------


def _latest_check_values(
    column: str,
    *,
    migrations_dir: Path | None = None,
) -> tuple[str, ...]:
    """Walk migrations in name order; the last file that sets a CHECK on
    ``<column> IN (...)`` wins. Returns the parsed value tuple.

    Recognised forms (matches every style in our migrations history):

        CHECK (column IN ('a', 'b'))
        column TEXT NOT NULL CHECK (column IN ('a','b'))
        ADD CONSTRAINT <name> CHECK (column IN ('a','b'))
    """
    if migrations_dir is None:
        migrations_dir = _MIGRATIONS_DIR
    pattern = re.compile(
        rf"\b{re.escape(column)}\s+IN\s*\(\s*((?:'[a-z_]+'(?:\s*,\s*)?)+)\s*\)",
        re.IGNORECASE,
    )
    last_values: tuple[str, ...] | None = None
    for path in sorted(migrations_dir.glob("*.sql")):
        sql = path.read_text(encoding="utf-8")
        matches = pattern.findall(sql)
        for raw in matches:
            values = tuple(v.strip().strip("'") for v in raw.split(","))
            last_values = values
    if last_values is None:
        raise RuntimeError(
            f"no CHECK constraint found in {migrations_dir} for column {column!r}; "
            "either the column was renamed, or the migration that defines it "
            "doesn't use a CHECK (column IN (...)) form. Update the "
            "_latest_check_values pattern if a new form is intentional."
        )
    return last_values


def _assert_literal_matches_sql(
    *,
    name: str,
    python_values: tuple[str, ...],
    sql_column: str,
) -> None:
    """Compare a Python ``Literal``'s args against the latest SQL CHECK.

    Compared as sets so order doesn't matter. Surfaces a verbose
    "X is in Python but not SQL" / "X is in SQL but not Python" message
    so the offending diff is obvious in CI output.
    """
    sql_values = _latest_check_values(sql_column)
    py_set = set(python_values)
    sql_set = set(sql_values)
    only_python = py_set - sql_set
    only_sql = sql_set - py_set
    assert not only_python and not only_sql, (
        f"{name} drift detected (Python Literal vs SQL CHECK on "
        f"{sql_column!r}):\n"
        f"  python:  {sorted(py_set)}\n"
        f"  sql:     {sorted(sql_set)}\n"
        f"  only in python: {sorted(only_python)}\n"
        f"  only in sql:    {sorted(only_sql)}\n"
        "Add a new migration with the missing values OR remove them from "
        "the Python Literal — whichever side is right."
    )


# ---------------------------------------------------------------------------
# Per-enum schema-drift assertions.
# ---------------------------------------------------------------------------


def test_tier_matches_sql() -> None:
    _assert_literal_matches_sql(
        name="Tier",
        python_values=get_args(Tier),
        sql_column="tier",
    )


def test_session_status_matches_sql() -> None:
    _assert_literal_matches_sql(
        name="SessionStatus",
        python_values=get_args(SessionStatus),
        sql_column="status",
    )


def test_verdict_matches_sql() -> None:
    _assert_literal_matches_sql(
        name="Verdict (legacy: ship/kill/pivot — sessions.store.Verdict)",
        python_values=get_args(Verdict),
        sql_column="verdict",
    )


def test_payment_mode_matches_sql() -> None:
    """PaymentMode is also covered by test_payment_mode_consistency.py;
    duplicating here on purpose so the generalized policy is the
    single-source authority going forward."""
    _assert_literal_matches_sql(
        name="PaymentMode",
        python_values=get_args(PaymentMode),
        sql_column="payment_mode",
    )
    # Belt-and-suspenders: also pin against PAYMENT_MODES.
    assert set(get_args(PaymentMode)) == set(PAYMENT_MODES)


def test_session_phase_matches_sql() -> None:
    _assert_literal_matches_sql(
        name="SessionPhase",
        python_values=get_args(SessionPhase),
        sql_column="phase",
    )


def test_pro_event_type_matches_sql() -> None:
    _assert_literal_matches_sql(
        name="ProEventType",
        python_values=get_args(ProEventType),
        sql_column="event_type",
    )


# ---------------------------------------------------------------------------
# Deliberate-divergence test — proves the comparator is not a no-op.
# ---------------------------------------------------------------------------


def test_divergence_is_detected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Construct a fake Python-vs-SQL pair with a deliberate drift, run the
    same comparator, and assert it raises.

    This is the meta-test: if someone refactors ``_latest_check_values``
    or ``_assert_literal_matches_sql`` and accidentally turns it into a
    no-op (e.g. comparing a set to itself), this case fails loud. Without
    it, every other test in this file could go green on a broken
    comparator.

    Strategy: write a tmp migrations directory with a single SQL file
    declaring CHECK (foo IN ('a','b')), redirect the module-level
    _MIGRATIONS_DIR to it via monkeypatch, then call
    ``_assert_literal_matches_sql`` with a Python tuple that has an
    extra value 'c'. The assertion must raise, naming 'c' as the drift.
    """
    # Build an isolated migrations dir.
    fake_dir = tmp_path / "migrations"
    fake_dir.mkdir()
    (fake_dir / "00000000000001_fake_drift.sql").write_text(
        "CREATE TABLE fakes (foo TEXT NOT NULL CHECK (foo IN ('a','b')));\n",
        encoding="utf-8",
    )

    # Sanity: the scanner picks up what we wrote when pointed at fake_dir.
    sql_values = _latest_check_values("foo", migrations_dir=fake_dir)
    assert set(sql_values) == {"a", "b"}

    # Drive the real comparator with the synthetic migrations dir by
    # redirecting the module-level _MIGRATIONS_DIR (which the helper now
    # reads at call time, not at import time).
    import sys

    this_module = sys.modules[__name__]
    monkeypatch.setattr(this_module, "_MIGRATIONS_DIR", fake_dir, raising=True)

    with pytest.raises(AssertionError) as excinfo:
        _assert_literal_matches_sql(
            name="FakeEnum",
            python_values=("a", "b", "c"),
            sql_column="foo",
        )
    assert "only in python: ['c']" in str(excinfo.value)
