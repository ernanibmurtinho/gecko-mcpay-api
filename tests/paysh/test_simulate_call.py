"""Tests for scripts/paysh/simulate_call.py — Pattern B local x402 sim.

Per feedback_lighter_tests: prefer pure-function unit tests; the single
CliRunner smoke is the only end-to-end path.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "paysh" / "simulate_call.py"
_SPEC = importlib.util.spec_from_file_location("paysh_simulate_call", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
simulate_call = importlib.util.module_from_spec(_SPEC)
# dataclass introspection requires the module to be registered in sys.modules
# before exec_module — otherwise frozen=True dataclasses fail to construct.
sys.modules["paysh_simulate_call"] = simulate_call
_SPEC.loader.exec_module(simulate_call)


def test_load_scenario_returns_canned_fixture() -> None:
    """Loader resolves a known skill fixture and exposes the paid response shape."""
    scenario = simulate_call.load_scenario("kyc-provider-stub")
    assert scenario["skill_id"] == "kyc-provider-stub"
    assert scenario["vertical"] == "neobank"
    assert "paid_response" in scenario
    assert "challenge" in scenario


def test_check_budget_aborts_when_cost_exceeds_remaining() -> None:
    """Budget gate flips to abort when cost strictly exceeds remaining envelope."""
    over = simulate_call.check_budget(cost_usd=1.0, budget_remaining_usd=0.5)
    assert over.would_abort is True

    within = simulate_call.check_budget(cost_usd=0.05, budget_remaining_usd=5.0)
    assert within.would_abort is False

    no_cap = simulate_call.check_budget(cost_usd=999.0, budget_remaining_usd=None)
    assert no_cap.would_abort is False


def test_build_simulated_output_shape_is_n4_consumable() -> None:
    """Output JSON carries the keys N4's ingestion path expects."""
    scenario = simulate_call.load_scenario("kyc-provider-stub")
    out = simulate_call.build_simulated_output(
        skill_id="kyc-provider-stub",
        query="verify customer",
        cost_usd=0.05,
        scenario=scenario,
    )
    assert set(out.keys()) >= {
        "skill",
        "cost_usd",
        "query",
        "response",
        "simulated",
        "no_real_spend",
    }
    assert out["skill"] == "kyc-provider-stub"
    assert out["simulated"] is True
    assert out["no_real_spend"] is True
    assert isinstance(out["response"], dict)


def test_cli_runner_smoke_against_fixture_skill() -> None:
    """End-to-end CLI smoke — fixture skill returns 0 with a parseable JSON line."""
    runner = CliRunner()
    result = runner.invoke(
        simulate_call.main,
        ["--skill", "kyc-provider-stub", "--query", "verify customer"],
    )
    assert result.exit_code == 0, result.output + "\n" + (result.stderr or "")
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["skill"] == "kyc-provider-stub"
    assert payload["no_real_spend"] is True

    abort = runner.invoke(
        simulate_call.main,
        [
            "--skill",
            "kyc-provider-stub",
            "--query",
            "x",
            "--cost-usd",
            "100",
            "--budget-remaining-usd",
            "5",
        ],
    )
    assert abort.exit_code == 3
    assert "WOULD ABORT" in (abort.stderr or "")


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
