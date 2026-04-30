"""Check OpenRouter pricing drift vs. local model_catalog.json.

S6-CATALOG-01: pulls live OpenRouter prices for every model in the catalog,
diffs vs. the committed pricing, and exits non-zero if any price changed
> DRIFT_THRESHOLD or a model is delisted.

Usage:
    uv run python scripts/check_catalog_drift.py
    uv run python scripts/check_catalog_drift.py --write   # rewrite JSON in place

Exit codes:
    0  no drift
    1  drift detected (price delta > threshold or delisted model)
    2  unexpected error (network, parse, etc.)
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from rich.console import Console
from rich.table import Table

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"

# Catalog stores prices in USD per 1M tokens; OpenRouter returns USD per token.
OPENROUTER_PRICE_SCALE = 1_000_000

# Trigger drift if any per-token price (input or output) shifts by more than this fraction.
DRIFT_THRESHOLD = 0.10

CATALOG_PATH = (
    Path(__file__).resolve().parent.parent
    / "packages"
    / "gecko-core"
    / "src"
    / "gecko_core"
    / "routing"
    / "model_catalog.json"
)


@dataclass
class Drift:
    """One row describing how a catalog entry differs from OpenRouter."""

    key: str
    model_id: str
    kind: str  # "price" | "delisted"
    field: str  # "input" | "output" | "-"
    old: float | None
    new: float | None
    pct: float | None  # signed delta as fraction; None for delisted


def load_catalog(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text())
    return data


def fetch_openrouter_models(
    url: str = OPENROUTER_MODELS_URL,
    *,
    client: httpx.Client | None = None,
) -> dict[str, dict[str, Any]]:
    """Return mapping of OpenRouter model id -> raw model dict."""
    own_client = client is None
    c = client or httpx.Client(timeout=30.0)
    try:
        resp = c.get(url)
        resp.raise_for_status()
        payload = resp.json()
    finally:
        if own_client:
            c.close()
    data = payload.get("data", []) if isinstance(payload, dict) else []
    return {m["id"]: m for m in data if isinstance(m, dict) and "id" in m}


def _to_per_million(raw: Any) -> float | None:
    """OpenRouter price strings are USD per token. Convert to USD per 1M."""
    if raw is None:
        return None
    try:
        return float(raw) * OPENROUTER_PRICE_SCALE
    except (TypeError, ValueError):
        return None


def detect_drift(
    catalog: dict[str, Any],
    live_models: dict[str, dict[str, Any]],
    *,
    threshold: float = DRIFT_THRESHOLD,
) -> tuple[list[Drift], dict[str, dict[str, float]]]:
    """Compare catalog vs. live, return (drift rows, suggested new prices)."""
    drifts: list[Drift] = []
    new_prices: dict[str, dict[str, float]] = {}

    for key, entry in catalog.get("models", {}).items():
        model_id = entry.get("id")
        if not model_id:
            continue

        live = live_models.get(model_id)
        if live is None:
            drifts.append(
                Drift(
                    key=key,
                    model_id=model_id,
                    kind="delisted",
                    field="-",
                    old=None,
                    new=None,
                    pct=None,
                )
            )
            continue

        live_pricing = live.get("pricing") or {}
        cat_pricing = entry.get("pricing") or {}

        for field in ("input", "output"):
            old_val = cat_pricing.get(field)
            new_val = _to_per_million(live_pricing.get(field))
            if old_val is None or new_val is None:
                continue
            # Free models stay free; treat any move off zero as drift.
            if old_val == 0 and new_val == 0:
                continue
            if old_val == 0:
                pct = float("inf") if new_val > 0 else 0.0
            else:
                pct = (new_val - old_val) / old_val
            if abs(pct) > threshold:
                drifts.append(
                    Drift(
                        key=key,
                        model_id=model_id,
                        kind="price",
                        field=field,
                        old=old_val,
                        new=new_val,
                        pct=pct,
                    )
                )
                new_prices.setdefault(key, dict(cat_pricing))[field] = new_val

    return drifts, new_prices


def render_drift_table(drifts: list[Drift]) -> Table:
    table = Table(title="Catalog drift vs. OpenRouter")
    table.add_column("model", style="cyan", no_wrap=True)
    table.add_column("id", style="dim")
    table.add_column("kind", style="magenta")
    table.add_column("field")
    table.add_column("old", justify="right")
    table.add_column("new", justify="right")
    table.add_column("delta %", justify="right")

    for d in drifts:
        if d.kind == "delisted":
            table.add_row(d.key, d.model_id, "DELISTED", "-", "-", "-", "-", style="red")
            continue
        old_s = f"${d.old:.4f}" if d.old is not None else "-"
        new_s = f"${d.new:.4f}" if d.new is not None else "-"
        if d.pct is None:
            pct_s = "-"
        elif d.pct == float("inf"):
            pct_s = "+inf"
        else:
            pct_s = f"{d.pct * 100:+.1f}%"
        style = "yellow" if d.pct is not None and abs(d.pct) < 0.5 else "red"
        table.add_row(d.key, d.model_id, "price", d.field, old_s, new_s, pct_s, style=style)

    return table


def apply_price_updates(
    catalog: dict[str, Any], new_prices: dict[str, dict[str, float]]
) -> dict[str, Any]:
    """Return a new catalog dict with updated pricing for drifted models."""
    out: dict[str, Any] = json.loads(json.dumps(catalog))  # deep copy via JSON
    for key, pricing in new_prices.items():
        if key in out["models"]:
            out["models"][key]["pricing"] = pricing
    return out


def apply_delisted_removals(
    catalog: dict[str, Any], drifts: list[Drift]
) -> tuple[dict[str, Any], list[str]]:
    """Drop catalog entries flagged as delisted.

    S8-CATALOG-01: when an OpenRouter listing disappears, leaving the entry
    in the local catalog produces a runtime ``CatalogError`` on lookup. The
    ``--write`` flow now strips delisted keys so a single drift run keeps
    the catalog internally consistent. Returns the new catalog plus the
    list of removed keys (for log output).
    """
    out: dict[str, Any] = json.loads(json.dumps(catalog))
    removed: list[str] = []
    for d in drifts:
        if d.kind == "delisted" and d.key in out.get("models", {}):
            del out["models"][d.key]
            removed.append(d.key)
    return out, removed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--catalog",
        type=Path,
        default=CATALOG_PATH,
        help="Path to model_catalog.json",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=DRIFT_THRESHOLD,
        help="Drift threshold as a fraction (default 0.10 == 10%%)",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Rewrite catalog file with updated prices when drift is found",
    )
    args = parser.parse_args(argv)

    console = Console()
    catalog = load_catalog(args.catalog)

    try:
        live = fetch_openrouter_models()
    except httpx.HTTPError as exc:
        console.print(f"[red]Failed to fetch OpenRouter models: {exc}[/red]")
        return 2

    drifts, new_prices = detect_drift(catalog, live, threshold=args.threshold)

    if not drifts:
        console.print("[green]No drift detected.[/green]")
        return 0

    console.print(render_drift_table(drifts))

    if args.write:
        updated = catalog
        if new_prices:
            updated = apply_price_updates(updated, new_prices)
        updated, removed = apply_delisted_removals(updated, drifts)
        if new_prices or removed:
            args.catalog.write_text(json.dumps(updated, indent=2) + "\n")
            if new_prices:
                console.print(
                    f"[yellow]Wrote {len(new_prices)} pricing update(s) to {args.catalog}.[/yellow]"
                )
            if removed:
                console.print(
                    f"[yellow]Removed {len(removed)} delisted model(s): "
                    f"{', '.join(removed)}[/yellow]"
                )

    return 1


if __name__ == "__main__":
    sys.exit(main())
