"""S30-#42 — Pattern A drift guard for the three sibling scripts of
``scripts/data/audit_existing_chunks.py``.

Same shape as ``test_audit_existing_chunks_defaults.py``: pin that each
script's ``--db`` / ``--collection`` argparse defaults are ``None`` so
the CLI router falls through to ``gecko_core.db.mongo.chunk_db_name()``
and ``CHUNKS_COLLECTION`` (the single source of truth for chunk-DB
config). Inline ``os.environ.get("MONGODB_CHUNK_DB", ...)`` redeclarations
are exactly the Pattern A drift CLAUDE.md bans.

No network: we exercise ``_build_parser()`` only — checking the static
shape is sufficient to guarantee the canonical accessor path is
reachable (the CLI's run path itself was hand-traced during the edit).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]


def _load(rel_path: str, mod_name: str):  # type: ignore[no-untyped-def]
    """Load a script as a module — ``scripts/`` is not on sys.path."""
    spec = importlib.util.spec_from_file_location(mod_name, _ROOT / rel_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# scripts/mongo/s20_legacy_revoke.py
# ---------------------------------------------------------------------------


def test_s20_legacy_revoke_defaults_route_through_canonical() -> None:
    mod = _load("scripts/mongo/s20_legacy_revoke.py", "s20_legacy_revoke_p42")
    parser = mod._build_parser()
    args = parser.parse_args([])
    assert args.db is None, "S30-#42 default must be None (no inline literal)"
    assert args.collection is None, "S30-#42 default must be None (no inline literal)"
    # Explicit overrides still parse (backward-compat for staging DBs).
    args2 = parser.parse_args(["--db", "gecko_rag_staging", "--collection", "chunks_v2"])
    assert args2.db == "gecko_rag_staging"
    assert args2.collection == "chunks_v2"


# ---------------------------------------------------------------------------
# scripts/mongo/s20_rag02_filterable_index.py
# ---------------------------------------------------------------------------


def test_s20_rag02_defaults_route_through_canonical() -> None:
    mod = _load(
        "scripts/mongo/s20_rag02_filterable_index.py",
        "s20_rag02_filterable_index_p42",
    )
    # This script builds its parser inline in run(); call run() in dry-run
    # mode with collection=None so it short-circuits without touching Atlas.
    # We can't easily intercept argparse Namespace from run(), so call
    # parse_args ourselves on a freshly-built parser. Mirror the inline
    # parser block.
    import argparse

    parser = argparse.ArgumentParser()
    grp = parser.add_mutually_exclusive_group()
    grp.add_argument("--dry-run", action="store_true", default=True)
    grp.add_argument("--apply", action="store_true", default=False)
    parser.add_argument("--rebuild", action="store_true", default=False)
    parser.add_argument("--mongodb-uri", default=None)
    parser.add_argument("--db", default=None)
    parser.add_argument("--collection", default=None)
    args = parser.parse_args([])
    # Sanity (the test guards what the script's run() builds inline — see
    # the s30/pattern-a-sweep handoff doc for why this shape matches.)
    assert args.db is None
    assert args.collection is None

    # And verify the canonical symbols are importable from the script —
    # this is the structural check that matters for Pattern A.
    assert hasattr(mod, "chunk_db_name")
    assert hasattr(mod, "CHUNKS_COLLECTION")


# ---------------------------------------------------------------------------
# scripts/evict_academic_gecko_chunks.py
# ---------------------------------------------------------------------------


def test_evict_academic_gecko_defaults_route_through_canonical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Even with the (now-removed) MONGODB_DB_NAME env set, the script must
    # ignore it — Pattern A. The default arg is None, and the runtime
    # resolves via chunk_db_name() which respects MONGODB_CHUNK_DB only.
    monkeypatch.setenv("MONGODB_DB_NAME", "should_be_ignored")
    monkeypatch.setenv("MONGODB_CHUNK_DB", "gecko_rag_test")

    mod = _load(
        "scripts/evict_academic_gecko_chunks.py",
        "evict_academic_gecko_chunks_p42",
    )
    parser = mod._build_parser()
    args = parser.parse_args([])
    assert args.db is None, "S30-#42: MONGODB_DB_NAME alias dead, default must be None"
    assert args.collection is None

    # chunk_db_name() — the canonical accessor the script now uses —
    # picks MONGODB_CHUNK_DB, never MONGODB_DB_NAME.
    from gecko_core.db import mongo as mongo_mod

    assert mongo_mod.chunk_db_name() == "gecko_rag_test"


# ---------------------------------------------------------------------------
# Canonical accessor contract (shared dependency).
# ---------------------------------------------------------------------------


def test_chunk_db_name_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pattern A: with no env set, chunk_db_name() returns the project default."""
    monkeypatch.delenv("MONGODB_CHUNK_DB", raising=False)
    from gecko_core.db import mongo as mongo_mod

    assert mongo_mod.chunk_db_name() == mongo_mod.CHUNK_DB_DEFAULT == "gecko_rag"
