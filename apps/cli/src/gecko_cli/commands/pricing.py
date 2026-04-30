"""`bb pricing` -- render the per-tier x endpoint pricing ladder (S6-TIER-03).

Reads `GET /pricing` and prints a Rich table. When the API is unreachable
(or the user is offline), falls back to building the table locally from
the curated catalog using the default x402 prices baked into
`gecko_api.settings`. The fallback is informational only — the user sees
the same shape, just without the live `price_usd` of any custom-priced
deployment.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import click
import httpx
from rich.console import Console
from rich.table import Table

console = Console()

_DEFAULT_API_URL = os.environ.get("GECKO_API_URL", "https://api.geckovision.tech")


async def _fetch_pricing(api_url: str) -> dict[str, Any]:
    async with httpx.AsyncClient(base_url=api_url, timeout=5.0) as client:
        response = await client.get("/pricing")
        response.raise_for_status()
        return dict(response.json())


def _local_pricing() -> dict[str, Any]:
    """Build the pricing table locally without hitting the API.

    Useful for `bb pricing --local` and as a fallback when the API call
    fails. Mirrors `gecko_api.main.pricing` but uses default prices.
    """
    from gecko_core.routing.pricing import build_pricing_table

    # Defaults match `gecko_api.settings.Settings.from_env` — keep in sync.
    return {
        "endpoints": build_pricing_table(
            {
                "research": 20.00,
                "plan": 0.25,
                "route": 0.01,
            }
        ),
        "tiers": ["quality", "balanced", "budget", "free"],
    }


def _render(payload: dict[str, Any]) -> None:
    tiers = list(payload.get("tiers") or ["quality", "balanced", "budget", "free"])
    endpoints = dict(payload.get("endpoints") or {})

    if not endpoints:
        console.print("[dim]No pricing data returned.[/dim]")
        return

    for endpoint, per_tier in endpoints.items():
        table = Table(title=f"/{endpoint}")
        table.add_column("Tier", style="bold")
        table.add_column("Price (USD)", justify="right")
        table.add_column("Est. latency", justify="right")
        table.add_column("Models", overflow="fold")
        for tier in tiers:
            row = per_tier.get(tier) or {}
            price = row.get("price_usd")
            latency = row.get("est_latency_ms")
            summary = row.get("model_summary") or "-"
            price_str = f"${float(price):.2f}" if price is not None else "-"
            latency_str = f"{int(latency) // 1000}s" if latency else "-"
            table.add_row(tier, price_str, latency_str, summary)
        console.print(table)


@click.command("pricing")
@click.option(
    "--api-url",
    default=_DEFAULT_API_URL,
    show_default=True,
    help="Base URL for gecko-api. Override for local dev.",
)
@click.option(
    "--local",
    is_flag=True,
    default=False,
    help="Skip the HTTP call; render the catalog-driven table locally.",
)
def pricing_cmd(api_url: str, local: bool) -> None:
    """Print the per-tier x endpoint pricing ladder."""
    if local:
        payload = _local_pricing()
        _render(payload)
        return
    try:
        payload = asyncio.run(_fetch_pricing(api_url))
    except (httpx.HTTPError, httpx.HTTPStatusError) as exc:
        console.print(
            f"[yellow]could not reach {api_url}/pricing ({exc}); rendering local fallback[/yellow]"
        )
        payload = _local_pricing()
    _render(payload)
