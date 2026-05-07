"""S20-RAG-02 — tests for the filterable-index migration script.

Stubs the pymongo collection so no Atlas API calls are issued.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest
from gecko_core.db.mongo import (
    CHUNKS_VECTOR_FILTER_FIELDS,
    CHUNKS_VECTOR_LEGACY_FILTER_FIELDS,
    SEARCH_INDEX_NAME,
    VECTOR_INDEX_NAME,
)

# Load the migration script by path — it lives under scripts/, which is
# not a top-level package on sys.path. Self-contained loader avoids
# needing a conftest.py / pyproject.toml change.
_SCRIPT_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "mongo" / "s20_rag02_filterable_index.py"
)
_spec = importlib.util.spec_from_file_location("s20_rag02_filterable_index", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
sys.modules["s20_rag02_filterable_index"] = _mod
_spec.loader.exec_module(_mod)

apply_index = _mod.apply_index
build_text_index_definition = _mod.build_text_index_definition
build_vector_index_definition = _mod.build_vector_index_definition
diff_index = _mod.diff_index
run = _mod.run


class _FakeCollection:
    def __init__(self, indexes: list[dict[str, Any]] | None = None) -> None:
        self._indexes: list[dict[str, Any]] = list(indexes or [])
        self.create_calls: list[dict[str, Any]] = []
        self.drop_calls: list[str] = []

    def list_search_indexes(self) -> list[dict[str, Any]]:
        return list(self._indexes)

    def create_search_index(self, definition: dict[str, Any]) -> None:
        self.create_calls.append(definition)
        # Make subsequent list_search_indexes() see it.
        existing = {i["name"]: i for i in self._indexes}
        existing[definition["name"]] = {
            "name": definition["name"],
            "latestDefinition": definition["definition"],
        }
        self._indexes = list(existing.values())

    def drop_search_index(self, name: str) -> None:
        self.drop_calls.append(name)
        self._indexes = [i for i in self._indexes if i["name"] != name]


def _live_def_with_filters(name: str, paths: list[str]) -> dict[str, Any]:
    fields: list[dict[str, Any]] = [
        {
            "type": "vector",
            "path": "embedding",
            "numDimensions": 1024,
            "similarity": "cosine",
        }
    ]
    for p in paths:
        fields.append({"type": "filter", "path": p})
    return {"name": name, "latestDefinition": {"fields": fields}}


# ---------------------------------------------------------------------------
# 1. dry-run prints intended definition; no Atlas API calls.
# ---------------------------------------------------------------------------


def test_dry_run_makes_no_api_calls(capsys: pytest.CaptureFixture[str]) -> None:
    coll = _FakeCollection()
    rc = run(["--dry-run"], collection=coll)
    out = capsys.readouterr().out
    assert rc == 0
    assert "DRY RUN" in out
    assert VECTOR_INDEX_NAME in out
    assert SEARCH_INDEX_NAME in out
    # Confirm the canonical filter paths are in the printed JSON.
    for p in CHUNKS_VECTOR_FILTER_FIELDS:
        assert p in out
    assert coll.create_calls == []
    assert coll.drop_calls == []


# ---------------------------------------------------------------------------
# 2. apply mode creates the index when none exists.
# ---------------------------------------------------------------------------


def test_apply_creates_index_when_missing() -> None:
    coll = _FakeCollection()
    status = apply_index(
        coll,
        definition=build_vector_index_definition(),
        rebuild=False,
    )
    assert "created" in status
    assert len(coll.create_calls) == 1
    assert coll.create_calls[0]["name"] == VECTOR_INDEX_NAME
    assert coll.drop_calls == []


# ---------------------------------------------------------------------------
# 3. up-to-date index → no-op.
# ---------------------------------------------------------------------------


def test_apply_idempotent_when_up_to_date() -> None:
    expected = list(CHUNKS_VECTOR_FILTER_FIELDS) + list(CHUNKS_VECTOR_LEGACY_FILTER_FIELDS)
    coll = _FakeCollection(indexes=[_live_def_with_filters(VECTOR_INDEX_NAME, expected)])
    status = apply_index(
        coll,
        definition=build_vector_index_definition(),
        rebuild=False,
    )
    assert "up-to-date" in status
    assert coll.create_calls == []
    assert coll.drop_calls == []


# ---------------------------------------------------------------------------
# 4. drift requires --rebuild.
# ---------------------------------------------------------------------------


def test_apply_rejects_drift_without_rebuild() -> None:
    # Live index has only session_id — missing all the new S20 fields.
    coll = _FakeCollection(indexes=[_live_def_with_filters(VECTOR_INDEX_NAME, ["session_id"])])
    with pytest.raises(RuntimeError, match="drift detected"):
        apply_index(
            coll,
            definition=build_vector_index_definition(),
            rebuild=False,
        )
    # No mutating calls fired.
    assert coll.create_calls == []
    assert coll.drop_calls == []


# ---------------------------------------------------------------------------
# 5. --rebuild drops + re-creates.
# ---------------------------------------------------------------------------


def test_apply_with_rebuild_drops_and_recreates() -> None:
    coll = _FakeCollection(indexes=[_live_def_with_filters(VECTOR_INDEX_NAME, ["session_id"])])
    status = apply_index(
        coll,
        definition=build_vector_index_definition(),
        rebuild=True,
    )
    assert "rebuilt" in status
    assert coll.drop_calls == [VECTOR_INDEX_NAME]
    assert len(coll.create_calls) == 1


# ---------------------------------------------------------------------------
# Helper-level coverage: diff_index reports drift correctly.
# ---------------------------------------------------------------------------


def test_diff_index_detects_missing_filters() -> None:
    coll = _FakeCollection(indexes=[_live_def_with_filters(VECTOR_INDEX_NAME, ["vertical"])])
    diff = diff_index(
        coll,
        name=VECTOR_INDEX_NAME,
        desired_filters=set(CHUNKS_VECTOR_FILTER_FIELDS),
    )
    assert diff.exists is True
    assert diff.up_to_date is False
    assert diff.needs_rebuild is True
    txt = diff.render()
    assert "drift" in txt
    assert "category" in txt  # one of the missing


def test_text_index_definition_has_token_filters() -> None:
    defn = build_text_index_definition()
    fields = defn["definition"]["mappings"]["fields"]
    for path in CHUNKS_VECTOR_FILTER_FIELDS:
        assert path in fields
        assert fields[path]["type"] == "token"


# ---------------------------------------------------------------------------
# End-to-end run() in apply mode against the fake collection.
# ---------------------------------------------------------------------------


def test_run_apply_creates_both_indexes(capsys: pytest.CaptureFixture[str]) -> None:
    coll = _FakeCollection()
    rc = run(["--apply"], collection=coll)
    out = capsys.readouterr().out
    assert rc == 0
    names = {c["name"] for c in coll.create_calls}
    assert VECTOR_INDEX_NAME in names
    assert SEARCH_INDEX_NAME in names
    assert "created" in out


def test_run_apply_drift_without_rebuild_returns_nonzero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    coll = _FakeCollection(
        indexes=[
            _live_def_with_filters(VECTOR_INDEX_NAME, ["session_id"]),
            _live_def_with_filters(SEARCH_INDEX_NAME, []),
        ]
    )
    rc = run(["--apply"], collection=coll)
    err = capsys.readouterr().err
    assert rc != 0
    assert "REJECTED" in err
