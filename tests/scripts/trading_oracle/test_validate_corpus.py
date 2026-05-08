"""Tests for ``scripts/trading_oracle/validate_corpus.py``.

Per ``feedback_lighter_tests``: light fakes, no Mongo, no orchestration
fired. We drive ``run_with_collection`` directly with a tiny in-memory
fake that implements the four async surfaces the script touches:
``count_documents``, ``aggregate``, ``find``.
"""

from __future__ import annotations

import importlib.util
import io
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from rich.console import Console

# Sibling-import pattern.
_SCRIPT_PATH = (
    Path(__file__).resolve().parents[3] / "scripts" / "trading_oracle" / "validate_corpus.py"
)
_spec = importlib.util.spec_from_file_location("trading_oracle_validate_corpus", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
vc = importlib.util.module_from_spec(_spec)
sys.modules["trading_oracle_validate_corpus"] = vc
_spec.loader.exec_module(vc)


# ---------------------------------------------------------------------------
# Tiny fake collection. Async iteration / coroutines wrapped minimally.
# ---------------------------------------------------------------------------


class _AsyncIter:
    def __init__(self, items: list[dict[str, Any]]) -> None:
        self._items = list(items)

    def __aiter__(self) -> _AsyncIter:
        return self

    async def __anext__(self) -> dict[str, Any]:
        if not self._items:
            raise StopAsyncIteration
        return self._items.pop(0)


class _FakeCursor:
    def __init__(self, items: list[dict[str, Any]]) -> None:
        self._items = items

    def limit(self, n: int) -> _FakeCursor:
        self._items = self._items[:n]
        return self

    def __aiter__(self) -> _AsyncIter:
        return _AsyncIter(self._items)


def _matches(doc: dict[str, Any], flt: dict[str, Any]) -> bool:
    for k, v in flt.items():
        if k == "$or":
            if not any(_matches(doc, sub) for sub in v):
                return False
            continue
        if isinstance(v, dict) and "$exists" in v:
            present = k in doc
            if v["$exists"] is False and present:
                return False
            if v["$exists"] is True and not present:
                return False
            continue
        if doc.get(k) != v:
            return False
    return True


class FakeCollection:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self.docs = docs

    async def count_documents(self, flt: dict[str, Any]) -> int:
        return sum(1 for d in self.docs if _matches(d, flt))

    def find(self, flt: dict[str, Any], _proj: dict[str, Any] | None = None) -> _FakeCursor:
        return _FakeCursor([d for d in self.docs if _matches(d, flt)])

    def aggregate(self, pipeline: list[dict[str, Any]]) -> _AsyncIter:
        # Inspect pipeline shape to dispatch. Tiny, hardcoded — not a
        # general $group engine.
        flt: dict[str, Any] = {}
        unwind_path: str | None = None
        group: dict[str, Any] = {}
        for stage in pipeline:
            if "$match" in stage:
                flt = stage["$match"]
            elif "$unwind" in stage:
                u = stage["$unwind"]
                unwind_path = u["path"] if isinstance(u, dict) else u
            elif "$group" in stage:
                group = stage["$group"]

        matched = [d for d in self.docs if _matches(d, flt)]

        if unwind_path:
            unwound: list[dict[str, Any]] = []
            field = unwind_path.lstrip("$")
            for d in matched:
                vals = d.get(field)
                if not vals:
                    unwound.append({**d, field: None})
                elif isinstance(vals, list):
                    for v in vals:
                        unwound.append({**d, field: v})
                else:
                    unwound.append({**d, field: vals})
            matched = unwound

        gid = group.get("_id")
        counts: Counter[Any] = Counter()
        for d in matched:
            if isinstance(gid, str):
                key = d.get(gid.lstrip("$"))
            elif isinstance(gid, dict):
                # Either {"$ifNull": ["$field", default]} or a compound dict.
                if "$ifNull" in gid:
                    field, default = gid["$ifNull"]
                    key = d.get(field.lstrip("$")) or default
                else:
                    parts: dict[str, Any] = {}
                    for sub_k, sub_v in gid.items():
                        if isinstance(sub_v, dict) and "$ifNull" in sub_v:
                            field, default = sub_v["$ifNull"]
                            parts[sub_k] = d.get(field.lstrip("$")) or default
                        elif isinstance(sub_v, str):
                            parts[sub_k] = d.get(sub_v.lstrip("$"))
                        else:
                            parts[sub_k] = sub_v
                    key = tuple(sorted(parts.items()))
                    counts[key] = counts.get(key, 0) + 1
                    continue
            else:
                key = None
            counts[key] = counts.get(key, 0) + 1

        out: list[dict[str, Any]] = []
        for key, n in counts.items():
            if isinstance(key, tuple):
                out.append({"_id": dict(key), "n": n})
            else:
                out.append({"_id": key, "n": n})
        return _AsyncIter(out)


def _capture_console() -> tuple[Console, io.StringIO]:
    buf = io.StringIO()
    return Console(file=buf, force_terminal=False, width=200), buf


# ---------------------------------------------------------------------------
# Tests.
# ---------------------------------------------------------------------------


def _good_chunk(**overrides: Any) -> dict[str, Any]:
    base = {
        "_id": "c1",
        "vertical": "dex",
        "provider_kind": "bazaar_live",
        "freshness_tier": "daily",
        "content_kind": "unknown",
        "protocol": ["jupiter"],
        "embedding": [0.0] * 1024,
        "text": "hello world",
        "is_stale": False,
        "source_url": "https://example.com/jupiter",
    }
    base.update(overrides)
    return base


def test_dim_drift_marked_red() -> None:
    docs = [
        _good_chunk(_id="ok", embedding=[0.0] * 1024),
        _good_chunk(_id="bad", embedding=[0.0] * 768),
    ]
    coll = FakeCollection(docs)
    console, buf = _capture_console()
    code = vc.run_with_collection(
        collection=coll,
        vertical="dex",
        provider_kind=None,
        protocol=None,
        sample=0,
        quiet=False,
        console=console,
    )
    out = buf.getvalue()
    assert code == 1, out
    assert "EMBEDDING DIM DRIFT" in out
    assert "bad" in out


def test_missing_protocol_warning() -> None:
    docs = [_good_chunk(_id=f"j{i}", protocol=["jupiter"]) for i in range(2)] + [
        _good_chunk(_id="k1", protocol=["kamino"]),
        _good_chunk(_id="p1", protocol=["pyth"]),
    ]
    coll = FakeCollection(docs)
    console, buf = _capture_console()
    code = vc.run_with_collection(
        collection=coll,
        vertical="dex",
        provider_kind=None,
        protocol=None,
        sample=0,
        quiet=False,
        console=console,
    )
    out = buf.getvalue()
    assert code == 0, out
    assert "MISSING PROTOCOL" in out
    assert "drift" in out
    assert "jito" in out


def test_empty_corpus_fails() -> None:
    coll = FakeCollection([])
    console, buf = _capture_console()
    code = vc.run_with_collection(
        collection=coll,
        vertical="dex",
        provider_kind=None,
        protocol=None,
        sample=0,
        quiet=False,
        console=console,
    )
    out = buf.getvalue()
    assert code == 1, out
    assert "NO CHUNKS" in out
