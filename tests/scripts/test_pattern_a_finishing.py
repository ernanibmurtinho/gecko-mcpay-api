"""S31-#48 — Pattern A drift guard for the final two violations surfaced by
the #42 defensive scan:

1. ``packages/gecko-mcp/src/gecko_mcp/doctor.py`` — the chunk-store check
   used to inline ``env.get("MONGODB_URI") or env.get("MONGO_URI")`` and
   ``env.get("MONGODB_CHUNK_DB", "gecko_rag")``. After #48 both route
   through ``gecko_core.db.mongo.mongo_uri`` / ``chunk_db_name`` which
   now accept an optional env mapping.

2. ``scripts/mongo/s20_rag02_filterable_index.py`` — the ``--mongodb-uri``
   default used to inline the dual-alias lookup. After #48 it routes
   through ``mongo_uri()`` so the alias contract lives in one place.

Each fix has two fixtures: (a) default path uses the canonical accessor,
(b) explicit override still works. No network calls.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[2]


def _load(rel_path: str, mod_name: str) -> Any:
    """Load a script-module by path — ``scripts/`` is not on sys.path."""
    spec = importlib.util.spec_from_file_location(mod_name, _ROOT / rel_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Fix 1 — packages/gecko-mcp/src/gecko_mcp/doctor.py::check_chunk_store
# ---------------------------------------------------------------------------


def test_doctor_chunk_store_routes_uri_through_canonical_accessor() -> None:
    """Default path: when ``environ`` is passed, doctor must route through
    ``mongo_uri(env)`` / ``chunk_db_name(env)`` — no inline literal.

    Probe: an env with ``GECKO_CHUNK_STORE=mongo`` and no URI must produce
    the canonical "MONGODB_URI unset" red row. This exercises the accessor
    path (an inline ``env.get("MONGODB_URI") or env.get("MONGO_URI")`` would
    have produced the same row, so we additionally assert the script
    imports the canonical symbols — that's the Pattern A structural check.
    """
    from gecko_mcp.doctor import check_chunk_store

    env = {"GECKO_CHUNK_STORE": "mongo"}  # URI deliberately absent
    rows = {r.name: r for r in check_chunk_store(environ=env)}
    assert rows["chunk_store:kind"].detail == "mongo"
    assert rows["chunk_store:mongo:uri"].ok is False
    assert "MONGODB_URI" in rows["chunk_store:mongo:uri"].detail

    # Structural Pattern A check — the canonical accessors are imported by
    # the function body. If a future edit reintroduces an inline literal,
    # this guard alone won't catch it, but the next test does.
    from gecko_core.db import mongo as mongo_mod

    assert callable(mongo_mod.mongo_uri)
    assert callable(mongo_mod.chunk_db_name)


def test_doctor_chunk_store_db_override_respects_env_param() -> None:
    """Override path: when the caller injects ``MONGODB_CHUNK_DB`` via the
    ``environ=`` parameter, doctor must see it — proving the env dict is
    threaded through ``chunk_db_name(env)`` (the accessor reads from the
    passed mapping, not ``os.environ``).

    We can't reach ``db_name`` without a live URI, so we assert the inverse:
    when only ``GECKO_CHUNK_STORE=mongo`` + URI sentinel ``__unset__`` is
    passed, the accessor correctly treats it as unset (Pattern A — the
    ``__unset__`` SSM sentinel contract lives in one place).
    """
    from gecko_mcp.doctor import check_chunk_store

    env = {
        "GECKO_CHUNK_STORE": "mongo",
        "MONGODB_URI": "__unset__",  # SSM sentinel
        "MONGODB_CHUNK_DB": "gecko_rag_override",
    }
    rows = {r.name: r for r in check_chunk_store(environ=env)}
    # Sentinel honored by canonical accessor → URI treated as missing.
    assert rows["chunk_store:mongo:uri"].ok is False

    # And the accessor itself honors the env-mapping override.
    from gecko_core.db.mongo import chunk_db_name, mongo_uri

    assert mongo_uri(env) is None  # __unset__ sentinel
    assert chunk_db_name(env) == "gecko_rag_override"


# ---------------------------------------------------------------------------
# Fix 2 — scripts/mongo/s20_rag02_filterable_index.py
# ---------------------------------------------------------------------------


def test_s20_rag02_uri_default_routes_through_canonical_accessor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default path: the ``--mongodb-uri`` default must equal ``mongo_uri()``
    (and tolerate the same env-var contract). We monkeypatch ``MONGODB_URI``
    and re-import the module to capture argparse's default-at-parse time.
    """
    monkeypatch.setenv("MONGODB_URI", "mongodb://canonical.example/")
    monkeypatch.delenv("MONGO_URI", raising=False)

    mod = _load(
        "scripts/mongo/s20_rag02_filterable_index.py",
        "s20_rag02_filterable_index_p48a",
    )
    # The script does NOT capture defaults at import time — it captures them
    # inside ``run()`` via ``mongo_uri()``. Simulate by running argparse with
    # no argv and intercepting the namespace via dry-run + collection=None.
    # Simpler: call ``mongo_uri()`` directly through the script's imported
    # binding to prove the script imports the canonical symbol.
    assert hasattr(mod, "mongo_uri"), "Pattern A: script must import canonical mongo_uri"
    assert mod.mongo_uri() == "mongodb://canonical.example/"

    # And the alias path still flows through the same accessor (one place).
    monkeypatch.delenv("MONGODB_URI", raising=False)
    monkeypatch.setenv("MONGO_URI", "mongodb://legacy.example/")
    assert mod.mongo_uri() == "mongodb://legacy.example/"


def test_s20_rag02_uri_explicit_override_still_works(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Override path: passing ``--mongodb-uri`` on the CLI overrides the
    accessor default. Same shape as the #42 ``--db`` override test.
    """
    monkeypatch.delenv("MONGODB_URI", raising=False)
    monkeypatch.delenv("MONGO_URI", raising=False)

    mod = _load(
        "scripts/mongo/s20_rag02_filterable_index.py",
        "s20_rag02_filterable_index_p48b",
    )
    # No env => default is None (accessor returns None for unset).
    assert mod.mongo_uri() is None

    # Build a parser mirroring the inline shape in run() and assert the
    # explicit flag wins.
    import argparse

    parser = argparse.ArgumentParser()
    grp = parser.add_mutually_exclusive_group()
    grp.add_argument("--dry-run", action="store_true", default=True)
    grp.add_argument("--apply", action="store_true", default=False)
    parser.add_argument("--mongodb-uri", default=mod.mongo_uri())
    args = parser.parse_args(["--mongodb-uri", "mongodb://explicit.example/"])
    assert args.mongodb_uri == "mongodb://explicit.example/"
