"""Local SQLite cache of last-known-good chunk rows (S14-HARDEN-01).

Backs ``bb research --degraded`` so a research run can complete against
recently-seen chunk data even when Supabase is unreachable. Per the
build plan dogfood findings F-S14-1 + F-S14-2, the most common failure
mode is a Supabase 5xx burst that takes the pipeline down for tens of
minutes; degraded mode lets a founder still get a (downgraded)
validation report instead of a hard halt.

Schema (single SQLite file at ``~/.gecko/cache.db``):

    sessions(
        session_id TEXT PRIMARY KEY,
        idea       TEXT NOT NULL,
        cached_at  TEXT NOT NULL  -- ISO 8601 UTC
    )

    chunks(
        session_id TEXT NOT NULL,
        chunk_index INTEGER NOT NULL,
        source_url TEXT NOT NULL,
        text       TEXT NOT NULL,
        PRIMARY KEY (session_id, chunk_index),
        FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
    )

We cap the cache at the 50 most recent sessions by ``cached_at``; older
sessions are evicted on every write. This keeps the SQLite file small
(<10MB even with chatty pro sessions) and guarantees ``--degraded`` can
load instantly.

Live mode is unaffected: writes are best-effort and never fail the
pipeline; reads happen only when the caller explicitly opts in.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# Default cache file. Tests inject a tmp_path-backed path directly.
DEFAULT_CACHE_PATH: Path = Path.home() / ".gecko" / "cache.db"

# Most-recent session retention cap.
MAX_CACHED_SESSIONS: int = 50


class LocalCacheError(RuntimeError):
    """Raised when degraded-mode fall-back can't recover a usable result."""


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            idea       TEXT NOT NULL,
            cached_at  TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS chunks (
            session_id  TEXT NOT NULL,
            chunk_index INTEGER NOT NULL,
            source_url  TEXT NOT NULL,
            text        TEXT NOT NULL,
            PRIMARY KEY (session_id, chunk_index),
            FOREIGN KEY (session_id)
                REFERENCES sessions(session_id) ON DELETE CASCADE
        )
        """
    )
    return conn


def write_session(
    *,
    session_id: str,
    idea: str,
    chunks: list[dict[str, object]],
    cache_path: Path | None = None,
) -> None:
    """Persist ``chunks`` for ``session_id`` and evict overflow.

    Best-effort: any failure logs a WARN and returns. The pipeline must
    not break because the local cache is in a bad state.

    ``chunks`` shape: ``{"chunk_index": int, "source_url": str, "text": str}``.
    """
    path = cache_path or DEFAULT_CACHE_PATH
    try:
        conn = _connect(path)
        try:
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO sessions(session_id, idea, cached_at) VALUES (?, ?, ?)",
                    (session_id, idea, datetime.now(UTC).isoformat()),
                )
                # Wipe + re-insert chunks for this session so updates
                # land cleanly (chunk_index may change if a re-run
                # produced different chunks).
                conn.execute("DELETE FROM chunks WHERE session_id = ?", (session_id,))
                rows = []
                for c in chunks:
                    raw_idx = c.get("chunk_index", 0) or 0
                    try:
                        idx = int(raw_idx) if isinstance(raw_idx, int | str | float) else 0
                    except (TypeError, ValueError):
                        idx = 0
                    rows.append(
                        (
                            session_id,
                            idx,
                            str(c.get("source_url", "")),
                            str(c.get("text", "")),
                        )
                    )
                conn.executemany(
                    "INSERT INTO chunks(session_id, chunk_index, source_url, text) "
                    "VALUES (?, ?, ?, ?)",
                    rows,
                )
                # Evict overflow: keep only the MAX_CACHED_SESSIONS most
                # recent sessions. Cascade deletes drop their chunks.
                conn.execute(
                    """
                    DELETE FROM sessions
                    WHERE session_id NOT IN (
                        SELECT session_id FROM sessions
                        ORDER BY cached_at DESC
                        LIMIT ?
                    )
                    """,
                    (MAX_CACHED_SESSIONS,),
                )
        finally:
            conn.close()
    except sqlite3.Error as exc:
        logger.warning("local_cache: write failed (%s); live mode unaffected", exc)


def read_recent_chunks(
    *,
    idea_substring: str | None = None,
    limit: int = 100,
    cache_path: Path | None = None,
) -> list[dict[str, object]]:
    """Read chunks from the most-recent matching session.

    When ``idea_substring`` is set, picks the most recent session whose
    ``idea`` contains the substring (case-insensitive). Falls back to
    the absolute most-recent session if no match. Returns an empty list
    if the cache is empty or unreadable.
    """
    path = cache_path or DEFAULT_CACHE_PATH
    if not path.exists():
        return []
    try:
        conn = _connect(path)
        try:
            cursor = conn.execute("SELECT session_id, idea FROM sessions ORDER BY cached_at DESC")
            picked: str | None = None
            if idea_substring:
                needle = idea_substring.lower()
                for sid, idea in cursor:
                    if needle in (idea or "").lower():
                        picked = sid
                        break
            if picked is None:
                # Fall back to most recent session of any idea.
                cursor = conn.execute(
                    "SELECT session_id FROM sessions ORDER BY cached_at DESC LIMIT 1"
                )
                row = cursor.fetchone()
                if row is None:
                    return []
                picked = row[0]
            rows = conn.execute(
                "SELECT chunk_index, source_url, text FROM chunks "
                "WHERE session_id = ? ORDER BY chunk_index ASC LIMIT ?",
                (picked, limit),
            ).fetchall()
            return [
                {"chunk_index": idx, "source_url": url, "text": txt} for (idx, url, txt) in rows
            ]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        logger.warning("local_cache: read failed (%s)", exc)
        return []


def cached_session_count(cache_path: Path | None = None) -> int:
    """Return the number of cached sessions (for `bb doctor` / diagnostics)."""
    path = cache_path or DEFAULT_CACHE_PATH
    if not path.exists():
        return 0
    try:
        conn = _connect(path)
        try:
            row = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()
            return int(row[0] if row else 0)
        finally:
            conn.close()
    except sqlite3.Error:
        return 0


__all__ = [
    "DEFAULT_CACHE_PATH",
    "MAX_CACHED_SESSIONS",
    "LocalCacheError",
    "cached_session_count",
    "read_recent_chunks",
    "write_session",
]
