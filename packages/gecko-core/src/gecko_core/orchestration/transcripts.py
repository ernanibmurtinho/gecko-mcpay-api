"""Production judge-transcript capture (S12-HARDEN-03 + S12-HARDEN-05).

Every successful `research()` call writes one transcript record so public
verdicts surfaced via CDP Bazaar have an audit trail (compliance + post-hoc
review of misranked verdicts). Schema mirrors the eval-side capture in
`tests/eval/runner.py::_archive_live_transcript` so the two corpora can be
analyzed with the same tooling.

S12-HARDEN-05 — backend selection:

  GECKO_TRANSCRIPT_STORE = "mongo" | "filesystem" | "noop"

  Default resolution when the env var is unset:
    1. "mongo"      if MONGODB_URI is set (production: ECS Fargate has
                    ephemeral disk, so Mongo is the only durable choice).
    2. "filesystem" otherwise (local dev, CI without Mongo).
    3. "noop"       only when explicitly requested.

  Best-effort fallback: if "mongo" fails to write (network blip, auth
  failure, server unreachable) we log WARN and fall through to the
  filesystem store so a verdict is never lost. The `/research` response
  itself is never blocked — the calling site swallows exceptions too.

Mongo schema (collection `judge_transcripts`):
  - `_id`            ObjectId (auto)
  - `session_id`     str (UUID) — unique index for upsert/last-write-wins
  - All eval-parity fields (idea_id, idea_text, judge_prose, parsed_verdict,
    agent_turns, actual_verdict, actual_verdict_v2, gap_classification,
    gap_summary, tier, advisor_voices, advisor_consensus, ...)
  - `created_at`     BSON Date — for "show me every GO verdict in last 30d"
  Indexes:
    - unique(session_id)
    - compound(actual_verdict_v2, created_at desc)

Filesystem path resolution (legacy + fallback):
  1. `GECKO_TRANSCRIPT_DIR` env var (operator override).
  2. `/var/lib/gecko/judge_transcripts/` if the parent is writable.
  3. `/tmp/gecko/transcripts/` last-resort fallback.

Toggle: `GECKO_TRANSCRIPT_CAPTURE` (default true). Set to `0`/`false`/`no`
to disable — tests do this so the suite doesn't litter $TMP.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from gecko_core.models import ResearchResult, Verdict

logger = logging.getLogger(__name__)


# Mirrors `gecko_core.workflows._JUDGE_VERDICT_RE` and
# `tests/eval/rubric._V2_VERDICT_RE` — single contract for the post-S11
# `Final verdict: PIVOT|REFINE|GO` line. Accepts the legacy KILL|BUILD
# tokens for transcripts captured before the S17-TONE-01 rename so the eval
# parity payload keeps working on historical fixtures.
_JUDGE_VERDICT_RE = re.compile(
    r"(?im)^\s*(?:final\s+)?verdict\s*[:\-]\s*(PIVOT|REFINE|GO|KILL|BUILD)\b"
)


_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}

# Mongo collection name — single source of truth for the runbook + tests.
_MONGO_COLLECTION = "judge_transcripts"


def is_capture_enabled() -> bool:
    """Read `GECKO_TRANSCRIPT_CAPTURE`. Default true in production."""
    raw = (os.environ.get("GECKO_TRANSCRIPT_CAPTURE") or "").strip().lower()
    if raw in _FALSE_VALUES:
        return False
    if raw in _TRUE_VALUES:
        return True
    return True


def _resolve_transcript_dir() -> Path:
    """Pick the on-disk directory for transcript records (filesystem store)."""
    override = (os.environ.get("GECKO_TRANSCRIPT_DIR") or "").strip()
    if override:
        return Path(override)

    candidate = Path("/var/lib/gecko/judge_transcripts")
    parent = candidate.parent
    try:
        if parent.exists() and os.access(parent, os.W_OK):
            candidate.mkdir(parents=True, exist_ok=True)
            return candidate
    except OSError:  # pragma: no cover — defensive
        pass

    return Path("/tmp/gecko/transcripts")


def _extract_judge_prose(result: ResearchResult) -> str:
    summary = getattr(result, "pro_session_summary", None)
    if isinstance(summary, str) and summary:
        return summary
    return ""


def _extract_agent_turns(result: ResearchResult) -> dict[str, dict[str, Any]] | None:
    transcript = getattr(result, "transcript", None)
    if not isinstance(transcript, dict):
        return None
    raw_turns = transcript.get("turns")
    if not isinstance(raw_turns, list):
        return None
    out: dict[str, dict[str, Any]] = {}
    for turn in raw_turns:
        if not isinstance(turn, dict):
            continue
        agent = turn.get("agent")
        if not isinstance(agent, str):
            continue
        out[agent] = {
            "text": str(turn.get("content") or ""),
            "tokens_in": int(turn.get("tokens_in") or 0),
            "tokens_out": int(turn.get("tokens_out") or 0),
        }
    return out or None


def _parse_verdict_token(judge_prose: str) -> str:
    if not judge_prose:
        return "UNKNOWN"
    match = _JUDGE_VERDICT_RE.search(judge_prose)
    if match is None:
        return "UNKNOWN"
    return match.group(1).upper()


def _build_payload(*, session_id: UUID, idea: str, result: ResearchResult) -> dict[str, Any]:
    """Build the eval-parity payload. Used by both stores."""
    judge_prose = _extract_judge_prose(result)
    agent_turns = _extract_agent_turns(result)

    structured = getattr(result, "verdict", None)
    if isinstance(structured, Verdict):
        v1_map = {Verdict.GO: "ship", Verdict.PIVOT: "kill", Verdict.REFINE: "pivot"}
        actual_verdict = v1_map[structured]
        actual_verdict_v2 = structured.value
    else:
        actual_verdict = "unknown"
        actual_verdict_v2 = "UNKNOWN"

    validation_report = getattr(result, "validation_report", None)
    gap_classification = getattr(validation_report, "gap_classification", None)
    gap_summary = getattr(validation_report, "gap_summary", None)

    return {
        # Schema kept aligned with `tests/eval/runner.py::_archive_live_transcript`.
        "id": str(session_id),
        "session_id": str(session_id),
        "idea_id": None,  # only populated by eval-side runner
        "idea_text": idea,
        "tier": getattr(result, "tier", "basic"),
        "judge_prose": judge_prose,
        "parsed_verdict": _parse_verdict_token(judge_prose),
        "actual_verdict": actual_verdict,
        "actual_verdict_v2": actual_verdict_v2,
        "gap_classification": str(gap_classification) if gap_classification else None,
        "gap_summary": gap_summary if isinstance(gap_summary, str) else None,
        "agent_turns": agent_turns,
        # Reserved keys mirror eval-side schema for jq/mongosh parity.
        "advisor_voices": None,
        "advisor_consensus": None,
        "rubric_score": None,
        "expected_verdict": None,
        "expected_verdict_v2": None,
        # ISO string for filesystem; Mongo store overwrites with BSON Date.
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


# ─── Store protocol + implementations ─────────────────────────────────────


class TranscriptStore(Protocol):
    """Pluggable transcript backend. `write` is best-effort; raise to signal
    failure so the dispatcher can fall through to a fallback store."""

    def write(self, record: dict[str, Any]) -> str | None:
        """Persist `record`. Return a stable identifier for logging
        (filesystem path or Mongo collection name). Raises on failure."""
        ...


class _FilesystemStore:
    """Original disk-backed store. Writes one JSON file per session_id."""

    def write(self, record: dict[str, Any]) -> str | None:
        out_dir = _resolve_transcript_dir()
        out_dir.mkdir(parents=True, exist_ok=True)
        session_id = record["session_id"]
        out_path = out_dir / f"{session_id}.json"
        out_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        return str(out_path)


class _NoopStore:
    """Explicit drop-on-the-floor store. Used when an operator wants no
    capture at all (rare; differs from `GECKO_TRANSCRIPT_CAPTURE=false` only
    in intent — both end at the same place)."""

    def write(self, record: dict[str, Any]) -> str | None:
        return None


def _mongo_uri() -> str | None:
    uri = os.environ.get("MONGODB_URI") or os.environ.get("MONGO_URI")
    if not uri or uri == "__unset__":
        # SSM sentinel — see infra/push-ssm-params.sh. Treat as truly unset
        # so the dispatcher silently degrades to the filesystem store.
        return None
    return uri


def _mongo_db_name() -> str:
    # Default matches `gecko_core.cache.mongo._db_name()`.
    return os.environ.get("MONGODB_DB", "gecko_cache")


@lru_cache(maxsize=1)
def _sync_mongo_client() -> Any | None:
    """Lazy sync MongoClient. Returns None if pymongo is unavailable or
    `MONGODB_URI` is unset. Cached for the process lifetime so we don't
    build a client per write.

    We use the synchronous `pymongo.MongoClient` (not motor) because
    `capture()` is a sync function called from async code via the event
    loop's running thread; spawning a nested loop just to write a record
    is more failure-prone than a short blocking write.
    """
    uri = _mongo_uri()
    if not uri:
        return None
    try:
        from pymongo import MongoClient
    except ImportError:  # pragma: no cover - pymongo ships with motor
        logger.warning("transcript capture: pymongo unavailable; skipping mongo store")
        return None
    # Short timeouts so a misconfigured Mongo can't stall the verdict path.
    return MongoClient(uri, serverSelectionTimeoutMS=2000, connectTimeoutMS=2000)


@lru_cache(maxsize=1)
def _ensure_mongo_indexes(coll_id: int) -> None:
    """Create unique(session_id) and compound(actual_verdict_v2, created_at)
    indexes once per process. Idempotent in Mongo."""
    coll = _mongo_collection_unsafe()
    if coll is None:
        return
    try:
        coll.create_index("session_id", unique=True)
        coll.create_index([("actual_verdict_v2", 1), ("created_at", -1)])
    except Exception as exc:  # pragma: no cover — index creation is best-effort
        logger.warning("transcript capture: mongo index creation failed: %s", exc)


def _mongo_collection_unsafe() -> Any | None:
    client = _sync_mongo_client()
    if client is None:
        return None
    return client[_mongo_db_name()][_MONGO_COLLECTION]


class _MongoStore:
    """MongoDB-backed store. Upserts on `session_id` so re-runs of the same
    session don't duplicate (last-write-wins)."""

    def write(self, record: dict[str, Any]) -> str | None:
        coll = _mongo_collection_unsafe()
        if coll is None:
            # Caller treats None as a soft miss → fall through to FS.
            raise RuntimeError("mongo not configured (MONGODB_URI unset)")

        # Lazily ensure indexes once per process (keyed by client id()).
        client = _sync_mongo_client()
        if client is not None:
            _ensure_mongo_indexes(id(client))

        # Replace the ISO string with a BSON Date for Mongo so the compound
        # `(actual_verdict_v2, created_at)` index is range-queryable.
        doc = dict(record)
        doc["created_at"] = datetime.now(UTC)

        coll.update_one(
            {"session_id": doc["session_id"]},
            {"$set": doc},
            upsert=True,
        )
        return f"mongo://{_mongo_db_name()}/{_MONGO_COLLECTION}"


# ─── Dispatcher ───────────────────────────────────────────────────────────


_VALID_STORES = {"mongo", "filesystem", "noop"}


def _resolve_store_choice() -> str:
    """Pick the active store. Explicit env var wins; otherwise default
    to mongo when MONGODB_URI is set, else filesystem."""
    raw = (os.environ.get("GECKO_TRANSCRIPT_STORE") or "").strip().lower()
    if raw in _VALID_STORES:
        return raw
    if raw:
        logger.warning(
            "transcript capture: unknown GECKO_TRANSCRIPT_STORE=%r; falling back to default",
            raw,
        )
    return "mongo" if _mongo_uri() else "filesystem"


def _build_store(choice: str) -> TranscriptStore:
    if choice == "mongo":
        return _MongoStore()
    if choice == "noop":
        return _NoopStore()
    return _FilesystemStore()


def capture(
    *,
    session_id: UUID,
    idea: str,
    result: ResearchResult,
) -> Path | str | None:
    """Persist one transcript record for a successful research run.

    Best-effort: any error is logged + swallowed. We never want a transcript
    write to take down a paid `/research` response.

    Returns the backend-specific identifier on success (Path for filesystem,
    `mongo://...` URI for Mongo), None when capture is disabled, the chosen
    store is `noop`, or every backend in the fallback chain failed.
    """
    if not is_capture_enabled():
        return None

    payload = _build_payload(session_id=session_id, idea=idea, result=result)
    choice = _resolve_store_choice()

    # Attempt chain: requested store first, then filesystem fallback when
    # mongo fails (so a verdict is never lost to a network blip).
    attempts: list[tuple[str, TranscriptStore]] = [(choice, _build_store(choice))]
    if choice == "mongo":
        attempts.append(("filesystem", _FilesystemStore()))

    for label, store in attempts:
        try:
            ident = store.write(payload)
        except Exception as exc:
            logger.warning("transcript capture: %s store write failed: %s", label, exc)
            continue
        if ident is None:
            # NoopStore — explicit "do nothing".
            logger.debug("transcript capture: %s store returned None (no-op)", label)
            return None
        logger.info(
            "transcript captured store=%s session_id=%s ident=%s verdict=%s",
            label,
            session_id,
            ident,
            payload["actual_verdict_v2"],
        )
        # Filesystem returns a path string for backwards compat with tests
        # that assert `out == tmp_path / f"{sid}.json"`.
        return Path(ident) if label == "filesystem" else ident

    return None


__all__ = ["TranscriptStore", "capture", "is_capture_enabled"]
