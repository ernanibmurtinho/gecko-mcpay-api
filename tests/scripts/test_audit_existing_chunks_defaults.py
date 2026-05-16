"""S30-#38 — guard the Pattern A fix in scripts/data/audit_existing_chunks.py.

Founder caught the bug: the audit script defaulted to ``--db gecko`` and
respected ``MONGO_DB``, while production runs against ``gecko_rag`` and
``MONGODB_CHUNK_DB``. These tests pin the new behavior: defaults route
through ``gecko_core.db.mongo`` (single source of truth), overrides still
work.

No network — we stub ``chunks_collection`` so ``run_audit`` is exercised
end-to-end against a tiny fake.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

# Load the script as a module — ``scripts/`` is not on sys.path as a package.
_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "data" / "audit_existing_chunks.py"
_spec = importlib.util.spec_from_file_location("audit_existing_chunks", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
audit_mod = importlib.util.module_from_spec(_spec)
sys.modules["audit_existing_chunks"] = audit_mod
_spec.loader.exec_module(audit_mod)


class _FakeCursor:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self._docs = list(docs)

    def __aiter__(self) -> _FakeCursor:
        return self

    async def __anext__(self) -> dict[str, Any]:
        if not self._docs:
            raise StopAsyncIteration
        return self._docs.pop(0)


class _FakeCollection:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self._docs = docs
        self.find_calls: list[tuple[Any, ...]] = []

    def find(self, filt: Any, *, projection: Any = None, batch_size: int = 0) -> _FakeCursor:
        self.find_calls.append((filt, projection, batch_size))
        return _FakeCursor(self._docs)


# ---------------------------------------------------------------------------
# argparse defaults: when no flags are passed, defaults are None so
# ``run_audit`` routes through the canonical accessor.
# ---------------------------------------------------------------------------


def test_argparse_defaults_are_none() -> None:
    parser = audit_mod._build_parser()
    args = parser.parse_args([])
    assert args.mongodb_uri is None
    assert args.db is None
    assert args.collection is None
    # Overrides still parse:
    args2 = parser.parse_args(["--db", "gecko_rag_staging", "--collection", "chunks_v2"])
    assert args2.db == "gecko_rag_staging"
    assert args2.collection == "chunks_v2"


# ---------------------------------------------------------------------------
# chunk_db_name() honors MONGODB_CHUNK_DB env (canonical accessor contract
# — this script depends on that contract).
# ---------------------------------------------------------------------------


def test_chunk_db_name_respects_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from gecko_core.db import mongo as mongo_mod

    monkeypatch.setenv("MONGODB_CHUNK_DB", "gecko_rag_staging")
    assert mongo_mod.chunk_db_name() == "gecko_rag_staging"
    monkeypatch.delenv("MONGODB_CHUNK_DB", raising=False)
    assert mongo_mod.chunk_db_name() == mongo_mod.CHUNK_DB_DEFAULT == "gecko_rag"


# ---------------------------------------------------------------------------
# Default path: run_audit() with no overrides must call
# gecko_core.db.mongo.chunks_collection() — the canonical accessor.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_audit_default_uses_canonical_accessor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    docs = [
        {"_id": "a", "text": "x" * 400, "provider_kind": "web", "source_url": "u"},
        {"_id": "b", "text": "short", "provider_kind": "web", "source_url": "u"},
    ]
    fake = _FakeCollection(docs)

    called = {"n": 0}

    def fake_chunks_collection() -> _FakeCollection:
        called["n"] += 1
        return fake

    monkeypatch.setattr(audit_mod, "chunks_collection", fake_chunks_collection)

    report = await audit_mod.run_audit(top_samples=5, batch_size=10)
    assert called["n"] == 1, "default path must call canonical chunks_collection()"
    assert report["total_chunks"] == 2
    assert "web" in report["by_provider_kind"]


# ---------------------------------------------------------------------------
# Override path: --db / --collection / --mongodb-uri bypass the canonical
# accessor and open a one-off client. Verifies backward-compat for staging.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_audit_override_bypasses_canonical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeCollection(
        [{"_id": "a", "text": "x" * 400, "provider_kind": "web", "source_url": "u"}]
    )

    captured: dict[str, Any] = {}

    class _FakeClient:
        def __init__(self, uri: str) -> None:
            captured["uri"] = uri

        def __getitem__(self, db_name: str) -> dict[str, _FakeCollection]:
            captured["db"] = db_name
            return {"chunks_v2": fake}

        def close(self) -> None:
            captured["closed"] = True

    # Patch motor lazy import.
    import types

    fake_motor = types.ModuleType("motor.motor_asyncio")
    fake_motor.AsyncIOMotorClient = _FakeClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "motor.motor_asyncio", fake_motor)

    # Sentinel: the canonical accessor must NOT be called on the override path.
    def _never_called() -> None:
        raise AssertionError("override path must not call chunks_collection()")

    monkeypatch.setattr(audit_mod, "chunks_collection", _never_called)

    report = await audit_mod.run_audit(
        top_samples=5,
        batch_size=10,
        mongodb_uri="mongodb://stub",
        db_name="gecko_rag_staging",
        collection_name="chunks_v2",
    )
    assert captured["uri"] == "mongodb://stub"
    assert captured["db"] == "gecko_rag_staging"
    assert captured.get("closed") is True
    assert report["total_chunks"] == 1
