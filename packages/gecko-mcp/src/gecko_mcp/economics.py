"""`gecko-mcp economics <session_id>` — print per-session unit economics.

Hits gecko-api's free `GET /sessions/{id}/economics` endpoint and renders the
breakdown: what we charged (`price_usd`) vs what we actually spent (LLM,
embeddings, Tavily, Deepgram), with `margin_usd` as the bottom line.

Useful on devnet where prices are symbolic but costs are real — the margin
column tells you whether mainnet pricing will hold up.

S6-RECON-01: pass `--verify` to additionally fetch the on-chain receipt
via Helius/public RPC and assert (status, amount, recipient) match what we
charged. Stub signatures (prefix `stub_`) are skipped — they are synthetic.
"""

from __future__ import annotations

import asyncio
import os
import sys

import click
import httpx
from gecko_core.payments.verifier import (
    VerifyResult,
    VerifyTarget,
    is_stub_signature,
    make_httpx_rpc,
    resolve_rpc_url,
    verify_target,
)
from rich.console import Console
from rich.table import Table

DEFAULT_API_URL = "https://api.geckovision.tech"


def _api_url() -> str:
    return os.environ.get("GECKO_API_URL", DEFAULT_API_URL).rstrip("/")


def _fmt_usd(v: float | None) -> str:
    if v is None:
        return "—"
    return f"${v:>10.6f}"


def _render_verify(result: VerifyResult) -> None:
    """Print a Rich diff table for one verification result."""
    console = Console()
    color = {
        "ok": "green",
        "skipped_stub": "yellow",
        "mismatch": "red",
        "not_found": "red",
        "rpc_error": "red",
    }.get(result.status, "white")
    console.print(f"[bold]verify[/bold]   [{color}]{result.status}[/{color}]")
    if result.status == "skipped_stub":
        console.print("  stub signature — no on-chain check")
        return
    if result.status == "rpc_error":
        console.print(f"  rpc error: {result.error}")
        return
    if result.status == "not_found":
        console.print(f"  signature not found: {result.target.tx_signature}")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("field")
    table.add_column("expected")
    table.add_column("actual")
    table.add_row(
        "amount_usd",
        f"{result.target.expected_amount_usd:.6f}",
        f"{result.actual_amount_usd:.6f}" if result.actual_amount_usd is not None else "—",
    )
    table.add_row(
        "recipient",
        str(result.target.expected_recipient or "—"),
        str(result.actual_recipient or "—"),
    )
    table.add_row("confirmation", "confirmed|finalized", str(result.confirmation or "—"))
    console.print(table)
    for m in result.mismatches:
        console.print(f"  [red]mismatch[/red]: {m}")


async def _verify_one(tx: str, expected_usd: float, network: str) -> VerifyResult:
    treasury = os.environ.get("GECKO_TREASURY_ADDRESS")
    target = VerifyTarget(
        source="session",
        row_id="<cli>",
        tx_signature=tx,
        expected_amount_usd=expected_usd,
        expected_recipient=treasury,
        network=network,
    )
    rpc_url = resolve_rpc_url(network)
    rpc = make_httpx_rpc(rpc_url)
    return await verify_target(target, rpc)


@click.command()
@click.argument("session_id")
@click.option(
    "--verify",
    is_flag=True,
    default=False,
    help="Fetch the x402 tx_signature on-chain and diff against expected price/recipient.",
)
def economics(session_id: str, verify: bool) -> None:
    """Print the economics row for SESSION_ID (price, costs, margin)."""
    url = f"{_api_url()}/sessions/{session_id}/economics"
    try:
        r = httpx.get(url, timeout=10.0)
        r.raise_for_status()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            click.secho(f"session {session_id} not found", fg="red", err=True)
            sys.exit(2)
        click.secho(f"gecko-api returned {exc.response.status_code}", fg="red", err=True)
        sys.exit(1)
    except httpx.HTTPError as exc:
        click.secho(f"gecko-api unreachable at {url}: {exc}", fg="red", err=True)
        sys.exit(1)

    body = r.json()
    price = body.get("price_usd")
    cost_total = body.get("cost_total_usd")
    margin = body.get("margin_usd")
    tx = body.get("x402_tx_signature") or "—"

    click.echo(f"session    {session_id}")
    click.echo(f"price      {_fmt_usd(price)}")
    click.echo("costs:")
    click.echo(f"  llm      {_fmt_usd(body.get('cost_llm_usd'))}")
    click.echo(f"  embed    {_fmt_usd(body.get('cost_embed_usd'))}")
    click.echo(f"  tavily   {_fmt_usd(body.get('cost_tavily_usd'))}")
    click.echo(f"  deepgram {_fmt_usd(body.get('cost_deepgram_usd'))}")
    # V1 sources (S4-TWITSH-02): twit.sh is the only paid V1 source today;
    # we surface both the per-source line (twitsh) and the cross-source
    # rollup so the dashboard reflects the workflow-level $0.10 cap.
    click.echo(f"  twitsh   {_fmt_usd(body.get('cost_twitsh_usd'))}")
    click.echo(f"  v1 srcs  {_fmt_usd(body.get('cost_v1_sources_usd'))}")
    click.echo("  ───────────────────")
    click.echo(f"  total    {_fmt_usd(cost_total)}")

    margin_color = "green" if (margin or 0) >= 0 else "red"
    click.secho(f"margin     {_fmt_usd(margin)}", fg=margin_color, bold=True)
    click.echo(f"x402 tx    {tx}")

    if verify:
        if tx == "—" or tx is None:
            click.secho("verify: no tx_signature on this session", fg="yellow")
            return
        if is_stub_signature(tx):
            click.secho("verify: stub signature — skipping on-chain check", fg="yellow")
            return
        network = body.get("network") or "solana-devnet"
        try:
            result = asyncio.run(_verify_one(tx, float(price or 0.0), network))
        except Exception as exc:
            click.secho(f"verify: error {exc}", fg="red", err=True)
            sys.exit(1)
        _render_verify(result)
        if result.status not in {"ok", "skipped_stub"}:
            sys.exit(3)
