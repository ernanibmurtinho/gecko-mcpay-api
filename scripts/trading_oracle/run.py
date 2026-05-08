"""Trading-oracle live ingest CLI.

This is the **scaffolding** for the one-shot live ingest run. The live
invocation step is operator-gated (founder must fund a buyer wallet on
Solana for paysh and on Base for bazaar — see
``memory/project_buyer_wallet_blocker_2026_05_08.md``). This script is
"ready to fire once funds land": you can dry-run today, and flip to live
the moment the buyer wallet is funded and a concrete ``PaidRequester``
implementation is wired into ``_build_paid_requester`` below.

Usage:

    # Plan-only against a recorded listings fixture (zero network I/O,
    # zero Mongo writes):
    uv run python scripts/trading_oracle/run.py \\
        --dry-run --listings-json path/to/listings.json

    # Live (after operator funds the buyer wallets — DO NOT run blindly):
    GECKO_X402_MODE=live \\
    uv run python scripts/trading_oracle/run.py \\
        --cap-usd 5.00 --source paysh

Flags:
    --cap-usd FLOAT   Hard $ cap per run. Default $5.00 (founder funds
                      $5 per network — paysh on Solana, bazaar on Base).
    --dry-run         Plan only. No catalog fetch, no charges, no writes.
                      Pair with --listings-json to plan against fixtures.
    --source CHOICE   ``paysh`` (Solana) | ``bazaar`` (Base) | ``both``.
                      Default ``both``. Use this to run one network at a
                      time since each is funded independently.
    --listings-json PATH  Optional JSON file containing pre-loaded
                      listings (planner shape — see ``_to_planner_listing``).
                      When set, the catalog fetch is skipped entirely;
                      the planner runs against the file. Required for
                      dry-run if you want non-empty output.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

import click
from gecko_core.ingestion.trading_oracle.run_live_ingest import (
    PlannedCall,
    execute_plan,
    plan_ingest,
)

log = logging.getLogger("trading_oracle.run")


# ---------------------------------------------------------------------------
# Listing-shape adapters.
#
# paysh_manifest / bazaar_manifest emit catalog rows whose shapes don't match
# the planner's expected ``{name, tags, price_usd, provider_kind, fqn, ...}``
# directly — so map at the CLI layer (per the task scope: do NOT modify the
# source modules).
# ---------------------------------------------------------------------------


def _to_planner_listing_paysh(provider: Any) -> dict[str, Any]:
    """Map a ``PayshCatalogProvider`` to the planner's listing dict.

    Pulls min_price, fqn, category, and synthesizes a tag list so the
    Solana-DeFi filter can match. paysh providers are Solana-native by
    construction; we tag them as such.
    """
    name = getattr(provider, "title", None) or getattr(provider, "fqn", "<unknown>")
    description = getattr(provider, "description", "") or ""
    category = getattr(provider, "category", "") or ""
    tags = ["solana", category] if category else ["solana"]
    min_price = float(getattr(provider, "min_price_usd", 0.0))
    return {
        "name": name,
        "description": description,
        "tags": tags,
        "price_usd": Decimal(str(min_price)),
        "provider_kind": "paysh_live",
        "fqn": getattr(provider, "fqn", ""),
        "service_url": getattr(provider, "service_url", ""),
    }


def _to_planner_listing_bazaar(service: Any) -> dict[str, Any]:
    """Map a ``BazaarService`` to the planner's listing dict.

    Pulls the first endpoint's price, networks, and category. Bazaar is
    Base-primary but multi-chain; the network list flows through as tags
    so the filter can decide.
    """
    name = getattr(service, "name", None) or getattr(service, "id", "<unknown>")
    description = getattr(service, "description", "") or ""
    category = getattr(service, "category", "") or ""
    networks = list(getattr(service, "networks", []) or [])
    tags = list(networks)
    if category:
        tags.append(category)
    # First endpoint with a usable url + price.
    endpoints = list(getattr(service, "endpoints", []) or [])
    min_price = 0.0
    endpoint_url = ""
    if endpoints:
        ep = endpoints[0]
        endpoint_url = getattr(ep, "url", "") or ""
        pricing = getattr(ep, "pricing", None)
        if pricing is not None:
            candidate = getattr(pricing, "minAmount", None) or getattr(pricing, "amount", None)
            if candidate:
                try:
                    min_price = float(candidate)
                except (TypeError, ValueError):
                    min_price = 0.0
    return {
        "name": name,
        "description": description,
        "tags": tags,
        "price_usd": Decimal(str(min_price)),
        "provider_kind": "bazaar_live",
        "fqn": getattr(service, "id", ""),
        "service_url": endpoint_url,
    }


# ---------------------------------------------------------------------------
# Catalog fetch (live-only; dry-run never touches the network).
# ---------------------------------------------------------------------------


async def _load_listings_from_catalogs(source: str) -> list[dict[str, Any]]:
    """Fetch live catalogs and adapt to planner shape.

    NOTE: this is the only function that touches the network in live mode.
    Dry-run skips it entirely (--listings-json is the dry-run input path).
    """
    listings: list[dict[str, Any]] = []
    if source in ("paysh", "both"):
        from gecko_core.sources.paysh_manifest import fetch_catalog as fetch_paysh_catalog

        paysh_result = await fetch_paysh_catalog()
        providers = (paysh_result.payload or {}).get("providers", []) if paysh_result else []
        listings.extend(_to_planner_listing_paysh(p) for p in providers)
    if source in ("bazaar", "both"):
        from gecko_core.sources.bazaar_manifest import fetch_catalog as fetch_bazaar_catalog

        bazaar_result = await fetch_bazaar_catalog()
        services = (bazaar_result.payload or {}).get("services", []) if bazaar_result else []
        listings.extend(_to_planner_listing_bazaar(s) for s in services)
    return listings


def _load_listings_from_file(path: Path) -> list[dict[str, Any]]:
    """Load planner-shaped listings from a JSON fixture (no network I/O)."""
    raw = json.loads(path.read_text())
    if not isinstance(raw, list):
        raise click.ClickException(f"--listings-json {path} must contain a JSON array")
    out: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        d = dict(entry)
        # Coerce price_usd to Decimal — JSON has no Decimal type.
        if "price_usd" in d and not isinstance(d["price_usd"], Decimal):
            d["price_usd"] = Decimal(str(d["price_usd"]))
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# Live charge + write seams.
#
# These are the I/O legs. Both are constructed lazily so importing this
# module (and the dry-run code path) never loads them.
# ---------------------------------------------------------------------------


def _build_paid_requester() -> Any:
    """Construct the live ``PaidRequester`` (Solana + Base buyer wallets).

    GAP: a concrete ``PaidRequester`` implementation is not yet shipped in
    ``gecko-core``. The buyer-wallet plumbing (SSM SecureString → ECS task
    env → x402 consumer client) is itself the operator-gated piece per
    ``project_buyer_wallet_blocker_2026_05_08.md``. Once that lands, swap
    the ``NotImplementedError`` for a concrete construction (likely a thin
    adapter over ``gecko_core.payments.x402_consumer``).

    Until then, the live path raises here — the dry-run path never calls
    this function.
    """
    raise NotImplementedError(
        "Live PaidRequester not wired. Operator gate: fund the buyer wallet "
        "(see memory/project_buyer_wallet_blocker_2026_05_08.md) and ship a "
        "concrete PaidRequester before flipping --dry-run off."
    )


async def _charge_and_fetch(call: PlannedCall) -> dict[str, Any]:
    """Issue one paid x402 call against the listing's source.

    Routes to ``paysh_live.fetch_paid`` or ``bazaar_live.fetch_paid`` based
    on ``provider_kind``. Both shipped in S22 (per the live_paysh /
    live_bazaar markers in the source modules) and accept a ``PaidRequester``
    for the wire seam.
    """
    pk = call.listing.get("provider_kind", "paysh_live")
    requester = _build_paid_requester()  # raises until live wallet is wired
    if pk == "paysh_live":
        from gecko_core.sources.paysh_live import fetch_paid as paysh_fetch_paid

        result = await paysh_fetch_paid(
            call.listing.get("fqn", ""),
            "Solana DeFi trading oracle ingest",
            x402_client=requester,
            catalog_providers=[],  # plumbed by the operator at flip-time
        )
    elif pk == "bazaar_live":
        from gecko_core.sources.bazaar_live import fetch_paid as bazaar_fetch_paid

        result = await bazaar_fetch_paid(
            call.listing.get("fqn", ""),
            "Solana DeFi trading oracle ingest",
            x402_client=requester,
            catalog_services=[],
        )
    else:
        raise ValueError(f"unknown provider_kind {pk!r}")
    chunks = (result.payload or {}).get("chunks", []) if result else []
    body = chunks[0]["text"] if chunks else ""
    return {"body": body, "fqn": call.listing.get("fqn", "")}


async def _write_chunk(
    *,
    text: str,
    provider_kind: str,
    vertical: str,
    freshness_tier: str,
    source_url: str,
) -> None:
    """Embed and persist one chunk to MongoDB with the trading-oracle axes.

    Wraps ``insert_chunks_mongo``. Embedding is computed via the standard
    ingestion embedder so the dim matches the Atlas Vector Search index.
    """
    # Lazy imports keep this leg out of the dry-run code path entirely.
    from uuid import uuid4

    from gecko_core.db.mongo_chunks import insert_chunks_mongo
    from gecko_core.ingestion.embedder import embed

    vectors = await embed([text])
    embedding = vectors[0] if vectors else []
    await insert_chunks_mongo(
        session_id=uuid4(),
        source_id=uuid4(),
        chunks=[(0, text, list(embedding))],
        category="market_intelligence",
        vertical=vertical,  # type: ignore[arg-type]
        source="paysh" if provider_kind == "paysh_live" else "bazaar",  # type: ignore[arg-type]
        provider_kind=provider_kind,  # type: ignore[arg-type]
        source_url=source_url,
        freshness_tier=freshness_tier,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# CLI entry.
# ---------------------------------------------------------------------------


def _log_plan(plan_calls: Sequence[PlannedCall], skipped: Sequence[Any]) -> None:
    for s in skipped:
        log.info("  SKIP %-50s reason=%s", s.name, s.reason)
    for c in plan_calls:
        log.info("  CALL %-50s price=$%s", c.name, c.price_usd)


@click.command()
@click.option("--cap-usd", default="5.00", show_default=True, help="Hard spend cap in USD.")
@click.option("--dry-run", is_flag=True, help="Plan only — no network I/O, no Mongo writes.")
@click.option(
    "--source",
    type=click.Choice(["paysh", "bazaar", "both"]),
    default="both",
    show_default=True,
    help="Which marketplace to ingest from.",
)
@click.option(
    "--listings-json",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Pre-loaded planner-shaped listings (skips catalog fetch).",
)
def main(cap_usd: str, dry_run: bool, source: str, listings_json: Path | None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    async def _run() -> int:
        # Listings: file-loaded fixture (no network) or live catalog fetch.
        if listings_json is not None:
            listings = _load_listings_from_file(listings_json)
            log.info(
                "loaded %d listings from fixture %s (no network I/O)",
                len(listings),
                listings_json,
            )
        elif dry_run:
            log.info(
                "dry-run with no --listings-json: skipping catalog fetch "
                "(zero network I/O contract). Pass --listings-json to plan "
                "against a fixture."
            )
            listings = []
        else:
            log.info("fetching live catalogs (source=%s)", source)
            listings = await _load_listings_from_catalogs(source)
            log.info("fetched %d listings", len(listings))

        # Filter by source AFTER loading so --source narrows file fixtures
        # too.
        if source != "both":
            wanted_pk = "paysh_live" if source == "paysh" else "bazaar_live"
            listings = [le for le in listings if le.get("provider_kind") == wanted_pk]

        plan = plan_ingest(listings, cap_usd=Decimal(cap_usd))
        log.info(
            "planned %d calls (skip %d, projected $%s, cap $%s)",
            len(plan.calls),
            len(plan.skipped),
            plan.projected_total_usd,
            cap_usd,
        )
        _log_plan(plan.calls, plan.skipped)

        if dry_run:
            log.info("DRY RUN — no charges, no writes.")
            return 0

        report = await execute_plan(
            plan,
            charge_and_fetch=_charge_and_fetch,
            write_chunk=_write_chunk,
            vertical="defi-trading",
        )
        log.info(
            "DONE: spent $%s, wrote %d chunks, %d failures",
            report.spent_usd,
            report.chunks_written,
            len(report.failures),
        )
        for f in report.failures:
            log.warning("  FAIL %s", f)
        return 1 if report.failures else 0

    sys.exit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
