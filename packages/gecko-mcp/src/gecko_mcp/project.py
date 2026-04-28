"""`gecko-mcp project <project_id>` — print per-project economics (S2-09).

Companion to `gecko-mcp economics <session_id>` (which stays session-scoped).
This command surfaces the project's Privy wallet, live USDC balance, budget
cap + spend, and the 5 most recent paid sessions.

Hits `GET /projects/{project_id}/economics` on gecko-api with the user's
frames.ag bearer. The endpoint itself is wired by web3-engineer's
S2-05/S2-06 dispatch — until that lands we degrade gracefully with a clear
"endpoint not yet deployed" message instead of a 500.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

import click

from gecko_mcp.api_client import GeckoAPIClient, GeckoAPIError


def _short_addr(addr: str | None) -> str:
    if not addr:
        return "—"
    if len(addr) <= 12:
        return addr
    return f"{addr[:6]}...{addr[-4:]}"


def _short_tx(sig: str | None) -> str:
    if not sig:
        return "—"
    if len(sig) <= 10:
        return sig
    return f"{sig[:4]}...{sig[-4:]}"


def _network_label(network: str | None) -> str:
    if network == "solana-mainnet":
        return "mainnet"
    if network == "solana-devnet":
        return "devnet"
    return network or "—"


def _fmt_balance(balance: str | None) -> str:
    if balance is None:
        return "unavailable"
    try:
        return f"${float(balance):.2f} USDC"
    except (TypeError, ValueError):
        return f"${balance} USDC"


def _fmt_money(v: str | None) -> str:
    if v is None:
        return "$0.00"
    try:
        return f"${float(v):.2f}"
    except (TypeError, ValueError):
        return f"${v}"


def _render(payload: dict[str, Any]) -> None:
    name = payload.get("name") or "(unnamed)"
    wallet = payload.get("wallet") or {}
    budget = payload.get("budget") or {}
    sessions = payload.get("recent_sessions") or []

    addr = wallet.get("privy_wallet_address")
    privy_id = wallet.get("privy_wallet_id")
    balance = wallet.get("balance_usdc")

    click.echo(f"PROJECT  {name}")
    if not addr and not privy_id:
        click.echo("  wallet:  no wallet provisioned yet")
    else:
        click.echo(f"  wallet:  {_short_addr(addr)} (privy)")
        click.echo(f"  balance: {_fmt_balance(balance)}")
    cap = _fmt_money(budget.get("cap_usd"))
    spent = _fmt_money(budget.get("spent_usd"))
    remaining = _fmt_money(budget.get("remaining_usd"))
    click.echo(f"  budget:  {cap} cap, {spent} spent ({remaining} remaining)")

    click.echo("")
    click.echo("RECENT SESSIONS")
    if not sessions:
        click.echo("  no sessions yet")
        return
    for s in sessions:
        tier = (s.get("tier") or "—").ljust(5)
        created = (s.get("created_at") or "").replace("T", " ")[:16]
        paid = _fmt_money(s.get("paid_usd"))
        sig = _short_tx(s.get("x402_tx_signature"))
        net = _network_label(s.get("network"))
        click.echo(f"  {tier}  {created}  {paid}  ->  {sig} ({net})")


@click.command(name="project")
@click.argument("project_id")
def project(project_id: str) -> None:
    """Print wallet balance + budget + recent sessions for PROJECT_ID."""
    asyncio.run(_run(project_id))


async def _run(project_id: str) -> None:
    client = GeckoAPIClient()
    try:
        try:
            payload = await client.get_project_economics(project_id)
        except GeckoAPIError as exc:
            msg = str(exc)
            click.secho(msg, fg="red", err=True)
            # Surface a non-zero exit so scripts can detect failure. Use 2
            # for "not found" so it differs from generic transport (1).
            sys.exit(2 if "not found" in msg else 1)
        _render(payload)
    finally:
        await client.aclose()
