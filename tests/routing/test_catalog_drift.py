"""Tests for scripts/check_catalog_drift.py.

Mocks the OpenRouter listing endpoint and verifies:

- No drift -> exit 0, table not printed.
- Price delta > 10% -> exit 1, drift row in table.
- Delisted model -> exit 1, "DELISTED" row.
- HTTP failure -> exit 2.
- --write rewrites the catalog with updated pricing.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from rich.console import Console

ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = ROOT / "scripts" / "check_catalog_drift.py"


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location("check_catalog_drift", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["check_catalog_drift"] = mod
    spec.loader.exec_module(mod)
    return mod


drift_mod = _load_module()


# Per-token prices (OpenRouter format) corresponding to the catalog values
# claude-opus-4.7 input=$5/M -> 5e-6 per token, output=$25/M -> 2.5e-5.
_BASE_LIVE = {
    "data": [
        {
            "id": "anthropic/claude-opus-4.7",
            "pricing": {"input": "0.000005", "output": "0.000025"},
        },
        {
            "id": "anthropic/claude-sonnet-4.6",
            "pricing": {"input": "0.000003", "output": "0.000015"},
        },
    ]
}


def _mini_catalog() -> dict[str, Any]:
    return {
        "models": {
            "claude-opus-4.7": {
                "id": "anthropic/claude-opus-4.7",
                "pricing": {"input": 5.00, "output": 25.00},
            },
            "claude-sonnet-4.6": {
                "id": "anthropic/claude-sonnet-4.6",
                "pricing": {"input": 3.00, "output": 15.00},
            },
        }
    }


def _live_map(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {m["id"]: m for m in payload["data"]}


def test_no_drift_exits_zero() -> None:
    drifts, _ = drift_mod.detect_drift(_mini_catalog(), _live_map(_BASE_LIVE))
    assert drifts == []


def test_small_drift_under_threshold_ignored() -> None:
    live = json.loads(json.dumps(_BASE_LIVE))
    # +5% on opus output: under 10% threshold.
    live["data"][0]["pricing"]["output"] = "0.0000262"
    drifts, _ = drift_mod.detect_drift(_mini_catalog(), _live_map(live))
    assert drifts == []


def test_price_drift_over_threshold_detected() -> None:
    live = json.loads(json.dumps(_BASE_LIVE))
    # +20% on opus input: 5e-6 -> 6e-6.
    live["data"][0]["pricing"]["input"] = "0.000006"
    drifts, new_prices = drift_mod.detect_drift(_mini_catalog(), _live_map(live))
    assert len(drifts) == 1
    d = drifts[0]
    assert d.kind == "price"
    assert d.field == "input"
    assert d.key == "claude-opus-4.7"
    assert d.old == pytest.approx(5.00)
    assert d.new == pytest.approx(6.00)
    assert d.pct == pytest.approx(0.20)
    assert new_prices["claude-opus-4.7"]["input"] == pytest.approx(6.00)
    # Output price unchanged should still be carried forward.
    assert new_prices["claude-opus-4.7"]["output"] == pytest.approx(25.00)


def test_delisted_model_detected() -> None:
    live = {"data": [_BASE_LIVE["data"][0]]}  # drop sonnet
    drifts, _ = drift_mod.detect_drift(_mini_catalog(), _live_map(live))
    assert len(drifts) == 1
    assert drifts[0].kind == "delisted"
    assert drifts[0].key == "claude-sonnet-4.6"


def test_render_drift_table_contains_rows() -> None:
    drifts = [
        drift_mod.Drift(
            key="claude-opus-4.7",
            model_id="anthropic/claude-opus-4.7",
            kind="price",
            field="input",
            old=5.00,
            new=6.00,
            pct=0.20,
        ),
        drift_mod.Drift(
            key="claude-sonnet-4.6",
            model_id="anthropic/claude-sonnet-4.6",
            kind="delisted",
            field="-",
            old=None,
            new=None,
            pct=None,
        ),
    ]
    table = drift_mod.render_drift_table(drifts)
    console = Console(record=True, width=160)
    console.print(table)
    out = console.export_text()
    assert "claude-opus-4.7" in out
    assert "+20.0%" in out
    assert "DELISTED" in out
    assert "claude-sonnet-4.6" in out


def test_apply_price_updates_does_not_mutate_input() -> None:
    cat = _mini_catalog()
    updated = drift_mod.apply_price_updates(
        cat, {"claude-opus-4.7": {"input": 6.00, "output": 25.00}}
    )
    assert updated["models"]["claude-opus-4.7"]["pricing"]["input"] == 6.00
    assert cat["models"]["claude-opus-4.7"]["pricing"]["input"] == 5.00


def test_main_exit_zero_no_drift(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_text(json.dumps(_mini_catalog()))

    monkeypatch.setattr(drift_mod, "fetch_openrouter_models", lambda *a, **k: _live_map(_BASE_LIVE))
    rc = drift_mod.main(["--catalog", str(catalog_path)])
    assert rc == 0


def test_main_exit_one_on_drift_and_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_text(json.dumps(_mini_catalog()))

    live = json.loads(json.dumps(_BASE_LIVE))
    live["data"][0]["pricing"]["input"] = "0.000006"  # +20%
    monkeypatch.setattr(drift_mod, "fetch_openrouter_models", lambda *a, **k: _live_map(live))

    rc = drift_mod.main(["--catalog", str(catalog_path), "--write"])
    assert rc == 1

    rewritten = json.loads(catalog_path.read_text())
    assert rewritten["models"]["claude-opus-4.7"]["pricing"]["input"] == pytest.approx(6.00)
    # Untouched model preserved.
    assert rewritten["models"]["claude-sonnet-4.6"]["pricing"]["input"] == pytest.approx(3.00)


def test_main_exit_two_on_http_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_text(json.dumps(_mini_catalog()))

    def boom(*_a: Any, **_k: Any) -> dict[str, Any]:
        raise httpx.ConnectError("network down")

    monkeypatch.setattr(drift_mod, "fetch_openrouter_models", boom)
    rc = drift_mod.main(["--catalog", str(catalog_path)])
    assert rc == 2


def test_fetch_openrouter_models_uses_provided_client() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=_BASE_LIVE))
    with httpx.Client(transport=transport) as client:
        out = drift_mod.fetch_openrouter_models(client=client)
    assert "anthropic/claude-opus-4.7" in out
    assert "anthropic/claude-sonnet-4.6" in out
