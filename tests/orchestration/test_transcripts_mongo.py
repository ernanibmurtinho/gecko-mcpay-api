"""S12-HARDEN-05 — MongoDB-backed transcript store.

Asserts the Mongo backend writes the eval-parity payload, the fallback
chain (mongo → filesystem) kicks in on connection failure, and the env
toggles (`GECKO_TRANSCRIPT_STORE`, `MONGODB_URI`) behave correctly.

CI uses `mongomock` (sync, in-process pymongo fake). Real Mongo is only
hit when `MONGODB_URI` is set in the test env AND a separate integration
suite opts in — by default we always patch in mongomock so unit tests
stay hermetic and free.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from tests.orchestration.test_transcripts import _result


def _patch_mongomock(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Swap the lazy `_sync_mongo_client` for a mongomock fake. Returns
    the mongomock client so tests can assert on the collection."""
    import mongomock
    from gecko_core.orchestration import transcripts as t

    fake = mongomock.MongoClient()
    # Clear LRU caches from prior tests in the same process.
    t._sync_mongo_client.cache_clear()
    t._ensure_mongo_indexes.cache_clear()
    monkeypatch.setattr(t, "_sync_mongo_client", lambda: fake)
    return fake


def test_mongo_store_writes_record(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Happy path: GECKO_TRANSCRIPT_STORE=mongo + MONGODB_URI set →
    record lands in the `judge_transcripts` collection with eval-parity
    fields and a BSON Date `created_at`."""
    from gecko_core.orchestration.transcripts import capture

    monkeypatch.setenv("GECKO_TRANSCRIPT_CAPTURE", "true")
    monkeypatch.setenv("GECKO_TRANSCRIPT_STORE", "mongo")
    monkeypatch.setenv("MONGODB_URI", "mongodb://localhost:27017")
    monkeypatch.setenv("MONGODB_DB", "gecko_test")
    monkeypatch.setenv("GECKO_TRANSCRIPT_DIR", str(tmp_path))  # FS not used here

    fake = _patch_mongomock(monkeypatch)

    sid = uuid4()
    out = capture(
        session_id=sid,
        idea="a hotel guide for Brazil",
        result=_result(
            tier="pro",
            verdict_token="GO",
            judge_prose="Final verdict: GO\nThe wedge is real.",
        ),
    )

    assert isinstance(out, str)
    assert out.startswith("mongo://gecko_test/judge_transcripts")

    coll = fake["gecko_test"]["judge_transcripts"]
    docs = list(coll.find({}))
    assert len(docs) == 1
    doc = docs[0]
    assert doc["session_id"] == str(sid)
    assert doc["idea_text"] == "a hotel guide for Brazil"
    assert doc["actual_verdict_v2"] == "GO"
    assert doc["actual_verdict"] == "ship"
    assert doc["parsed_verdict"] == "GO"
    assert doc["agent_turns"] is not None
    assert "judge" in doc["agent_turns"]
    # Mongo-side stores BSON Date, not ISO string.
    assert isinstance(doc["created_at"], datetime)

    # Filesystem fallback was NOT exercised (mongo write succeeded).
    assert list(tmp_path.iterdir()) == []


def test_mongo_store_upserts_on_session_id(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Re-running the same session must not duplicate — last-write-wins
    via the unique(session_id) index + upsert."""
    from gecko_core.orchestration.transcripts import capture

    monkeypatch.setenv("GECKO_TRANSCRIPT_CAPTURE", "true")
    monkeypatch.setenv("GECKO_TRANSCRIPT_STORE", "mongo")
    monkeypatch.setenv("MONGODB_URI", "mongodb://localhost:27017")
    monkeypatch.setenv("MONGODB_DB", "gecko_test")
    monkeypatch.setenv("GECKO_TRANSCRIPT_DIR", str(tmp_path))

    fake = _patch_mongomock(monkeypatch)
    sid = uuid4()

    capture(session_id=sid, idea="first idea", result=_result(verdict_token="REFINE"))
    capture(
        session_id=sid,
        idea="second idea",
        result=_result(verdict_token="GO", judge_prose="Final verdict: GO\nUpdated."),
    )

    coll = fake["gecko_test"]["judge_transcripts"]
    docs = list(coll.find({"session_id": str(sid)}))
    assert len(docs) == 1
    assert docs[0]["idea_text"] == "second idea"
    assert docs[0]["actual_verdict_v2"] == "GO"


def test_mongo_failure_falls_through_to_filesystem(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """If the mongo write raises (network blip, auth fail, server down)
    the dispatcher logs a WARN and falls through to the filesystem store
    so the verdict is never lost."""
    from gecko_core.orchestration import transcripts as t
    from gecko_core.orchestration.transcripts import capture

    monkeypatch.setenv("GECKO_TRANSCRIPT_CAPTURE", "true")
    monkeypatch.setenv("GECKO_TRANSCRIPT_STORE", "mongo")
    monkeypatch.setenv("MONGODB_URI", "mongodb://localhost:27017")
    monkeypatch.setenv("MONGODB_DB", "gecko_test")
    monkeypatch.setenv("GECKO_TRANSCRIPT_DIR", str(tmp_path))

    # Force the mongo write to explode regardless of mongomock setup.
    t._sync_mongo_client.cache_clear()
    t._ensure_mongo_indexes.cache_clear()

    class _BoomCollection:
        def update_one(self, *_a: Any, **_k: Any) -> None:
            raise ConnectionError("simulated mongo network blip")

        def create_index(self, *_a: Any, **_k: Any) -> None:
            raise ConnectionError("simulated mongo network blip")

    monkeypatch.setattr(t, "_mongo_collection_unsafe", lambda: _BoomCollection())
    # `_sync_mongo_client` is consulted by `_ensure_mongo_indexes`; stub it
    # so the cache key path doesn't itself raise.
    monkeypatch.setattr(t, "_sync_mongo_client", lambda: object())

    sid = uuid4()
    with caplog.at_level(logging.WARNING, logger="gecko_core.orchestration.transcripts"):
        out = capture(session_id=sid, idea="fallback test", result=_result(verdict_token="PIVOT"))

    # Filesystem store wrote the record.
    assert isinstance(out, Path)
    assert out == tmp_path / f"{sid}.json"
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["session_id"] == str(sid)
    assert payload["actual_verdict_v2"] == "PIVOT"

    # Mongo failure was logged.
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("mongo store write failed" in r.getMessage() for r in warnings), (
        f"expected mongo failure warning, got: {[r.getMessage() for r in warnings]}"
    )


def test_store_noop_skips_all_writes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`GECKO_TRANSCRIPT_STORE=noop` → neither Mongo nor FS sees a write."""
    from gecko_core.orchestration.transcripts import capture

    monkeypatch.setenv("GECKO_TRANSCRIPT_CAPTURE", "true")
    monkeypatch.setenv("GECKO_TRANSCRIPT_STORE", "noop")
    monkeypatch.setenv("MONGODB_URI", "mongodb://localhost:27017")
    monkeypatch.setenv("GECKO_TRANSCRIPT_DIR", str(tmp_path))

    fake = _patch_mongomock(monkeypatch)

    out = capture(session_id=uuid4(), idea="noop", result=_result())
    assert out is None
    assert list(tmp_path.iterdir()) == []
    assert list(fake["gecko_cache"]["judge_transcripts"].find({})) == []


def test_mongo_requested_but_uri_unset_degrades_to_filesystem(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`GECKO_TRANSCRIPT_STORE=mongo` with no `MONGODB_URI` — silently
    degrade to filesystem (the mongo write raises 'mongo not configured',
    caught by the dispatcher, falls through to FS). No exception bubbles."""
    from gecko_core.orchestration import transcripts as t
    from gecko_core.orchestration.transcripts import capture

    monkeypatch.setenv("GECKO_TRANSCRIPT_CAPTURE", "true")
    monkeypatch.setenv("GECKO_TRANSCRIPT_STORE", "mongo")
    monkeypatch.delenv("MONGODB_URI", raising=False)
    monkeypatch.delenv("MONGO_URI", raising=False)
    monkeypatch.setenv("GECKO_TRANSCRIPT_DIR", str(tmp_path))

    # Reset cached client.
    t._sync_mongo_client.cache_clear()
    t._ensure_mongo_indexes.cache_clear()

    sid = uuid4()
    out = capture(session_id=sid, idea="degrade", result=_result(verdict_token="REFINE"))

    assert isinstance(out, Path)
    assert out == tmp_path / f"{sid}.json"
    assert out.exists()


def test_default_picks_mongo_when_uri_set(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No explicit GECKO_TRANSCRIPT_STORE + MONGODB_URI set → default to mongo."""
    from gecko_core.orchestration.transcripts import capture

    monkeypatch.setenv("GECKO_TRANSCRIPT_CAPTURE", "true")
    monkeypatch.delenv("GECKO_TRANSCRIPT_STORE", raising=False)
    monkeypatch.setenv("MONGODB_URI", "mongodb://localhost:27017")
    monkeypatch.setenv("MONGODB_DB", "gecko_test")
    monkeypatch.setenv("GECKO_TRANSCRIPT_DIR", str(tmp_path))

    fake = _patch_mongomock(monkeypatch)

    out = capture(session_id=uuid4(), idea="default mongo", result=_result())
    assert isinstance(out, str)
    assert out.startswith("mongo://")
    assert list(fake["gecko_test"]["judge_transcripts"].find({}))
    # FS untouched.
    assert list(tmp_path.iterdir()) == []


def test_default_picks_filesystem_when_uri_unset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No GECKO_TRANSCRIPT_STORE + no MONGODB_URI → default to filesystem."""
    from gecko_core.orchestration import transcripts as t
    from gecko_core.orchestration.transcripts import capture

    monkeypatch.setenv("GECKO_TRANSCRIPT_CAPTURE", "true")
    monkeypatch.delenv("GECKO_TRANSCRIPT_STORE", raising=False)
    monkeypatch.delenv("MONGODB_URI", raising=False)
    monkeypatch.delenv("MONGO_URI", raising=False)
    monkeypatch.setenv("GECKO_TRANSCRIPT_DIR", str(tmp_path))

    t._sync_mongo_client.cache_clear()

    sid = uuid4()
    out = capture(session_id=sid, idea="fs default", result=_result())
    assert isinstance(out, Path)
    assert out == tmp_path / f"{sid}.json"
