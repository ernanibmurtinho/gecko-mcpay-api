"""Free local x402 simulation. NO real wallet, NO real spend, NO network calls. See CLAUDE.md Pattern B.

S22-PAYSH-LOCAL-SIM-01 — gate for the live pay.sh caller (S22-PAYSH-LIVE-CALLER-01).

What this script does
---------------------
Simulates the x402 challenge/response cycle for a pay.sh skill against a
canned scenario fixture under ``scripts/paysh/scenarios/<skill_id>.json``.
The output is a structured JSON line shaped exactly like what the live
caller (N4) will produce on success::

    {"skill": <id>, "cost_usd": <float>, "response": <provider body>}

What this script does NOT do
----------------------------
* Does not read any wallet env var (``X402_PRIVATE_KEY``,
  ``SOLANA_PRIVATE_KEY``, ``MNEMONIC``, ``WALLET_*``).
* Does not import the signing helpers from ``gecko_core.payments``.
* Does not make any outbound HTTP request.
* Does not load ``.env`` — fully offline by construction.

Usage
-----
::

    python scripts/paysh/simulate_call.py --skill kyc-provider-stub --query "verify customer"
    python scripts/paysh/simulate_call.py --skill card-issuing-stub --query "issue virtual card" --cost-usd 0.10
    python scripts/paysh/simulate_call.py --skill baas-rails-stub --query "rtp quote" --budget-remaining-usd 0.01
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click

SCENARIOS_DIR = Path(__file__).parent / "scenarios"


class ScenarioNotFoundError(RuntimeError):
    """Raised when no canned scenario fixture exists for the requested skill."""


@dataclass(frozen=True)
class BudgetCheck:
    """Outcome of the local budget gate.

    ``would_abort=True`` means the simulated charge exceeds the remaining
    budget envelope; the CLI exits non-zero and never prints the canned
    response. Mirrors the hard cap N4 will enforce against
    ``PAYSH_S22_BUDGET_USD``.
    """

    would_abort: bool
    cost_usd: float
    budget_remaining_usd: float | None
    reason: str


def load_scenario(skill_id: str, scenarios_dir: Path = SCENARIOS_DIR) -> dict[str, Any]:
    """Load a canned scenario fixture by skill_id.

    Raises ``ScenarioNotFoundError`` if no fixture exists. The simulation is
    fixture-only by design — there is no fallback to a real network fetch.
    """
    path = scenarios_dir / f"{skill_id}.json"
    if not path.exists():
        available = sorted(p.stem for p in scenarios_dir.glob("*.json"))
        raise ScenarioNotFoundError(
            f"no scenario fixture for skill_id={skill_id!r} at {path}. Available: {available}"
        )
    return json.loads(path.read_text())  # type: ignore[no-any-return]


def check_budget(cost_usd: float, budget_remaining_usd: float | None) -> BudgetCheck:
    """Return a ``BudgetCheck`` describing whether the charge would proceed.

    ``budget_remaining_usd=None`` disables the gate (matches N4's behavior
    when the operator hasn't set ``PAYSH_S22_BUDGET_USD``). When set, a
    cost strictly greater than the remaining budget aborts.
    """
    if budget_remaining_usd is None:
        return BudgetCheck(
            would_abort=False,
            cost_usd=cost_usd,
            budget_remaining_usd=None,
            reason="no budget cap configured",
        )
    if cost_usd > budget_remaining_usd:
        return BudgetCheck(
            would_abort=True,
            cost_usd=cost_usd,
            budget_remaining_usd=budget_remaining_usd,
            reason=(f"cost {cost_usd} USD exceeds remaining budget {budget_remaining_usd} USD"),
        )
    return BudgetCheck(
        would_abort=False,
        cost_usd=cost_usd,
        budget_remaining_usd=budget_remaining_usd,
        reason="within budget",
    )


def build_simulated_output(
    *,
    skill_id: str,
    query: str,
    cost_usd: float,
    scenario: dict[str, Any],
) -> dict[str, Any]:
    """Build the structured success output N4's ingestion path will consume.

    Wire shape:
      ``{"skill": str, "cost_usd": float, "query": str, "response": dict,
         "simulated": True, "no_real_spend": True}``
    """
    paid = scenario.get("paid_response", {})
    response_body = paid.get("body", {})
    return {
        "skill": skill_id,
        "cost_usd": cost_usd,
        "query": query,
        "response": response_body,
        "simulated": True,
        "no_real_spend": True,
    }


def _render_challenge_log(scenario: dict[str, Any], cost_usd: float) -> list[str]:
    """Build the human-readable challenge log lines printed to stderr."""
    challenge = scenario.get("challenge", {})
    headers = challenge.get("headers", {})
    payment_required = headers.get(
        "X-Payment-Required",
        f"x402; amount={cost_usd}; asset=usdc",
    )
    return [
        f"[sim] 402 Payment Required from skill={scenario.get('skill_id')!r}",
        f"[sim] X-Payment-Required: {payment_required}",
        "[sim] would sign with wallet at $X402_MODE=live (NO real signing performed)",
        f"[sim] simulated cost: {cost_usd} USD",
    ]


@click.command()
@click.option(
    "--skill",
    "skill_id",
    required=True,
    help="pay.sh skill id (matches scenario fixture filename).",
)
@click.option(
    "--query", "query", required=True, help="Free-form query string forwarded to the provider."
)
@click.option(
    "--cost-usd",
    "cost_usd",
    type=float,
    default=None,
    help="Override the per-call cost. Defaults to the scenario's default_cost_usd.",
)
@click.option(
    "--budget-remaining-usd",
    "budget_remaining_usd",
    type=float,
    default=None,
    help="If set, abort when cost > budget_remaining (mirrors PAYSH_S22_BUDGET_USD behavior).",
)
@click.option(
    "--response-fixture",
    "response_fixture",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Override the scenario fixture path. Useful for ad-hoc fixtures.",
)
def main(
    skill_id: str,
    query: str,
    cost_usd: float | None,
    budget_remaining_usd: float | None,
    response_fixture: Path | None,
) -> None:
    """Simulate an x402 paid call for a pay.sh skill — fully offline.

    Exits 0 on simulated success, non-zero on budget-exceeded or
    fixture-not-found.
    """
    try:
        if response_fixture is not None:
            scenario = json.loads(response_fixture.read_text())
        else:
            scenario = load_scenario(skill_id)
    except ScenarioNotFoundError as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(2)

    effective_cost = (
        cost_usd if cost_usd is not None else float(scenario.get("default_cost_usd", 0.05))
    )

    for line in _render_challenge_log(scenario, effective_cost):
        click.echo(line, err=True)

    gate = check_budget(effective_cost, budget_remaining_usd)
    if gate.would_abort:
        click.echo(f"WOULD ABORT: {gate.reason}", err=True)
        click.echo(
            json.dumps(
                {
                    "skill": skill_id,
                    "cost_usd": effective_cost,
                    "would_abort": True,
                    "reason": gate.reason,
                }
            )
        )
        sys.exit(3)

    output = build_simulated_output(
        skill_id=skill_id,
        query=query,
        cost_usd=effective_cost,
        scenario=scenario,
    )
    click.echo(json.dumps(output))

    response_body = output["response"]
    response_len = len(json.dumps(response_body))
    click.echo(
        f"[sim] OK — skill={skill_id} cost={effective_cost} USD "
        f"response_bytes={response_len} (no real spend occurred)",
        err=True,
    )


if __name__ == "__main__":
    main()
