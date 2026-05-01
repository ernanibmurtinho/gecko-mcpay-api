"""S12.5-TEST-02 — schema-drift guard for PaymentMode / X402Mode.

Sprint 12 Track A added 'cdp' to one of the four parallel definitions and
shipped. The other three didn't get the memo and three follow-up commits
(76bf278, 7e97c59, a7f7cc5) mopped up the breakage at production-runtime.

This test asserts every parallel definition agrees with the canonical
PAYMENT_MODES tuple in `gecko_core.payments.modes`:

  1. `gecko_core.payments.x402_client.X402Mode` Literal
  2. `gecko_core.sessions.store.PaymentMode` Literal
  3. `gecko_api.settings.X402Mode` Literal
  4. `gecko_api.settings.Settings.from_env` accepted-set
  5. The SQL CHECK constraint on sessions.payment_mode (latest migration
     that mentions it wins).

Adding a new mode now requires updating PAYMENT_MODES + every Literal +
the SQL migration. If any of those drift, this test fails with a clear
"X is in PAYMENT_MODES but not Y" message. PR review can rely on it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import get_args

import pytest
from gecko_core.payments.modes import PAYMENT_MODES, PaymentMode, X402Mode
from gecko_core.payments.x402_client import (
    PaymentMode as ClientPaymentMode,
)
from gecko_core.payments.x402_client import (
    X402Mode as ClientX402Mode,
)
from gecko_core.sessions.store import PaymentMode as SessionPaymentMode

# Path to the migrations directory (resolved relative to repo root).
_REPO_ROOT = Path(__file__).resolve().parents[3]
_MIGRATIONS_DIR = _REPO_ROOT / "infra" / "supabase" / "migrations"


def test_canonical_modes_value() -> None:
    """Lock the canonical list. If we add a mode, this assertion changes
    and the developer is forced to think about the SQL migration too."""
    assert PAYMENT_MODES == ("stub", "live", "frames", "cdp")


def test_payment_mode_literal_matches_runtime_tuple() -> None:
    """Static type alias and runtime tuple cannot drift inside modes.py."""
    assert get_args(PaymentMode) == PAYMENT_MODES
    assert get_args(X402Mode) == PAYMENT_MODES


def test_x402_client_literal_matches() -> None:
    """gecko_core.payments.x402_client re-exports — should be identical."""
    assert get_args(ClientX402Mode) == PAYMENT_MODES
    assert get_args(ClientPaymentMode) == PAYMENT_MODES


def test_session_store_literal_matches() -> None:
    """sessions.store keeps its own Literal (circular-import constraint).

    THIS is the test that would have caught a7f7cc5 before the live smoke.
    """
    assert get_args(SessionPaymentMode) == PAYMENT_MODES, (
        "gecko_core.sessions.store.PaymentMode drifted from PAYMENT_MODES. "
        "Update both literals together; see modes.py for canonical list."
    )


def test_gecko_api_settings_literal_matches() -> None:
    """gecko-api re-exports X402Mode from modes.py — should be identical.

    THIS would have caught 76bf278.
    """
    from gecko_api.settings import X402Mode as ApiX402Mode

    assert get_args(ApiX402Mode) == PAYMENT_MODES


def test_gecko_api_settings_env_validation_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings.from_env must accept (and only accept) PAYMENT_MODES.

    Any value in PAYMENT_MODES must load cleanly; any value not in
    PAYMENT_MODES must raise.
    """
    from gecko_api.settings import Settings

    # Clear unrelated env so resolve_network doesn't fight us.
    monkeypatch.setenv("X402_NETWORK", "solana-devnet")
    monkeypatch.setenv("GECKO_WALLET_ADDRESS", "STUB")
    monkeypatch.delenv("X402_FACILITATOR_URL", raising=False)
    monkeypatch.delenv("CDP_API_KEY_ID", raising=False)
    monkeypatch.delenv("CDP_API_KEY_SECRET", raising=False)

    for mode in PAYMENT_MODES:
        monkeypatch.setenv("X402_MODE", mode)
        try:
            Settings.from_env()
        except ValueError:
            pytest.fail(f"Settings.from_env rejected canonical mode {mode!r}")
        except Exception:
            # Other errors (e.g. missing CDP creds for cdp mode) are not the
            # literal-validation surface we're testing; tolerate them.
            pass

    # And the negative case.
    monkeypatch.setenv("X402_MODE", "definitely-not-a-mode")
    with pytest.raises(ValueError, match="unknown X402_MODE"):
        Settings.from_env()


# ---------------------------------------------------------------------------
# SQL CHECK constraint scan
# ---------------------------------------------------------------------------


def _latest_payment_mode_check_values() -> tuple[str, ...]:
    """Walk migrations in name order; the last file that sets a CHECK on
    sessions.payment_mode wins. Returns the parsed value tuple.

    Recognised forms (matches both styles in our migrations history):
        CHECK (payment_mode IN ('stub', 'live', 'frames', 'cdp'))
        ADD CONSTRAINT <name> CHECK (payment_mode IN (...))
    """
    pattern = re.compile(
        r"payment_mode\s+IN\s*\(\s*((?:'[a-z]+'(?:\s*,\s*)?)+)\s*\)",
        re.IGNORECASE,
    )
    last_values: tuple[str, ...] | None = None
    for path in sorted(_MIGRATIONS_DIR.glob("*.sql")):
        sql = path.read_text(encoding="utf-8")
        matches = pattern.findall(sql)
        for raw in matches:
            values = tuple(v.strip().strip("'") for v in raw.split(","))
            last_values = values
    if last_values is None:  # pragma: no cover — would mean migrations broke
        raise RuntimeError("no payment_mode CHECK constraint found in migrations/")
    return last_values


def test_sql_check_constraint_matches_payment_modes() -> None:
    """The active SQL CHECK on sessions.payment_mode must match PAYMENT_MODES.

    THIS is the test that would have caught 7e97c59 — adding 'cdp' to the
    Python literal without updating the DB constraint failed CDP-mode
    session inserts with PostgreSQL error 23514.
    """
    sql_values = _latest_payment_mode_check_values()
    assert set(sql_values) == set(PAYMENT_MODES), (
        f"SQL CHECK values {sql_values} drifted from PAYMENT_MODES "
        f"{PAYMENT_MODES}. Add a new migration that updates "
        "sessions_payment_mode_check, or update modes.py if SQL is right."
    )
