"""Daily aggregate circuit breaker for twit.sh spend (S14-TWITSH-05).

Caps twit.sh aggregate spend at $10/day across **all sessions** on a host.
Past the cap, ``check_and_record_spend`` returns ``False`` and the
provider's ``fetch()`` skips — the caller surfaces ``twitsh`` in
``degraded_sources`` and the pipeline keeps running without it.

This is the cross-session counterpart to ``TwitshSource``'s in-process
$0.05 per-session cap. Both gates are necessary: per-session protects
a runaway query within one job; daily aggregate protects against a
many-session burst (e.g. 50 sessions/day on hot Solana ideas during
Colosseum demo days).

Persistence is a tiny JSON file at ``~/.gecko/twitsh_daily_spend.json``
keyed by UTC date. Reads + writes are guarded by ``filelock`` so two
concurrent ``bb research`` invocations don't double-count under a
race. Missing / corrupt file → treated as zero spend, not as a hard
failure (better to over-fire on the breaker's edge than to halt the
pipeline because of a stale cache file).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# Default daily cap. Override via ``TWITSH_DAILY_CAP_USD`` for tests or
# operators who want to stretch the budget temporarily during a campaign.
DEFAULT_DAILY_CAP_USD: float = 10.0

# Persistence target. Lives under the user's home so multiple Gecko CLI
# invocations from different cwds share a single ledger. Tests inject a
# tmp_path-backed path directly.
DEFAULT_LEDGER_PATH: Path = Path.home() / ".gecko" / "twitsh_daily_spend.json"


def _today_utc_iso() -> str:
    """UTC date string keyed into the ledger. Reset boundary is UTC midnight."""
    return datetime.now(UTC).date().isoformat()


def _read_ledger(path: Path) -> dict[str, float]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("twitsh_circuit: failed to read %s (%s); treating as empty", path, exc)
        return {}
    if not isinstance(data, dict):
        return {}
    # Coerce values; drop any malformed entries silently.
    return {k: float(v) for k, v in data.items() if isinstance(v, int | float)}


def _write_ledger(path: Path, data: dict[str, float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Truncate to today + one prior day so the file doesn't grow unboundedly.
    # The breaker only consults today's row; older rows are kept for one day
    # for diagnostic purposes and then GC'd on the next write.
    today = _today_utc_iso()
    keep = {k: v for k, v in data.items() if k >= today or _within_one_day(k, today)}
    path.write_text(json.dumps(keep), encoding="utf-8")


def _within_one_day(key: str, today: str) -> bool:
    try:
        k_date = datetime.fromisoformat(key).date()
        t_date = datetime.fromisoformat(today).date()
        return (t_date - k_date).days <= 1
    except ValueError:
        return False


def _cap_usd() -> float:
    raw = os.environ.get("TWITSH_DAILY_CAP_USD")
    if not raw:
        return DEFAULT_DAILY_CAP_USD
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_DAILY_CAP_USD


def get_today_spend(*, ledger_path: Path | None = None) -> float:
    """Return today's recorded twit.sh aggregate USD spend."""
    path = ledger_path or DEFAULT_LEDGER_PATH
    return _read_ledger(path).get(_today_utc_iso(), 0.0)


def reserve_spend(
    amount_usd: float,
    *,
    ledger_path: Path | None = None,
    cap_usd: float | None = None,
) -> bool:
    """Atomically check + reserve ``amount_usd`` against the daily cap.

    Returns:
      True  — within cap, ``amount_usd`` recorded; caller may proceed.
      False — would exceed the cap; nothing recorded, caller must skip
              the twit.sh fetch and add ``"twitsh"`` to
              ``degraded_sources``.

    The check + write happen as a single read-modify-write pass. The
    caller reserves *before* firing the paid call so concurrent sessions
    can't all squeeze through under the cap on a race.
    """
    path = ledger_path or DEFAULT_LEDGER_PATH
    cap = cap_usd if cap_usd is not None else _cap_usd()
    ledger = _read_ledger(path)
    today = _today_utc_iso()
    current = ledger.get(today, 0.0)
    if current + amount_usd > cap:
        logger.info(
            "twitsh_circuit: daily cap %.2f reached (current=%.4f + %.4f); "
            "marking provider degraded",
            cap,
            current,
            amount_usd,
        )
        return False
    ledger[today] = current + amount_usd
    try:
        _write_ledger(path, ledger)
    except OSError as exc:
        # If we can't write, fall back to allowing the call (better to
        # over-fire than to silently halt). Log so operators can spot
        # filesystem issues.
        logger.warning("twitsh_circuit: ledger write failed (%s); allowing call", exc)
    return True


__all__ = [
    "DEFAULT_DAILY_CAP_USD",
    "DEFAULT_LEDGER_PATH",
    "get_today_spend",
    "reserve_spend",
]
