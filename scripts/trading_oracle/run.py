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
import hashlib
import json
import logging
import os
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
# URL template substitution.
#
# Bazaar listings often have endpoint URLs like
# https://api.zerion.io/v1/wallets/{address}/portfolio. Without
# substitution they 402 with empty accepts[]. We substitute the buyer's
# own wallet address so the call shape is at least valid.
# ---------------------------------------------------------------------------

import re as _re  # noqa: E402  (kept local to module concern)


def _substitute_url_template(url: str, *, wallet_address: str) -> str:
    """Replace {address} placeholders in bazaar endpoint URLs with the buyer wallet.

    Bazaar listings often have endpoint URLs like
    https://api.zerion.io/v1/wallets/{address}/portfolio. Without
    substitution they 402 with empty accepts[]. We substitute the buyer's
    own wallet address so the call shape is at least valid.
    """
    substituted = url.replace("{address}", wallet_address)
    leftover = _re.findall(r"\{[^}]+\}", substituted)
    if leftover:
        log.warning(
            "URL %r still contains unresolved placeholders %s after substitution; issuing as-is",
            substituted,
            leftover,
        )
    return substituted


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


# Module-level memo so _build_paid_requester returns the same object every
# time within a run (the underlying httpx client is reusable, and the buyer
# nonce semantics are intent-id scoped, not requester-instance scoped).
_PAID_REQUESTER_SINGLETON: Any | None = None


def _build_paid_requester() -> Any:
    """Construct a live x402 ``PaidRequester`` from env. Raises if config missing.

    Reads (in priority order):
        TWITSH_WALLET_ADDRESS, TWITSH_WALLET_PRIVATE_KEY  (existing prod path)
        GECKO_BUYER_WALLET_ADDRESS, GECKO_BUYER_WALLET_PRIVATE_KEY  (new path)
        X402_NETWORK            (default base-mainnet)
        GECKO_X402_MODE         (must be 'live')

    Returns a ``_LiveX402PaidRequester`` — the concrete adapter that
    conforms to ``gecko_core.sources.paysh_live.PaidRequester`` /
    ``bazaar_live.PaidRequester``. The adapter performs the x402 v2 buyer
    dance (GET → 402 → sign X-PAYMENT → GET) using
    ``cdp_x402_client._build_payment_payload`` so the EIP-3009 vs Permit2
    signing path matches the seller-side fix in 19d0a83.
    """
    global _PAID_REQUESTER_SINGLETON
    if _PAID_REQUESTER_SINGLETON is not None:
        return _PAID_REQUESTER_SINGLETON

    mode = os.environ.get("GECKO_X402_MODE", "stub")
    if mode != "live":
        raise click.ClickException(
            f"GECKO_X402_MODE={mode!r}; live x402 requester requires "
            "GECKO_X402_MODE=live. Aborting before any network call."
        )

    private_key = os.environ.get("TWITSH_WALLET_PRIVATE_KEY") or os.environ.get(
        "GECKO_BUYER_WALLET_PRIVATE_KEY"
    )
    address = os.environ.get("TWITSH_WALLET_ADDRESS") or os.environ.get(
        "GECKO_BUYER_WALLET_ADDRESS"
    )
    if not private_key or not address:
        raise click.ClickException(
            "buyer wallet env not set. Export one of these pairs before "
            "running live: "
            "(TWITSH_WALLET_ADDRESS + TWITSH_WALLET_PRIVATE_KEY) or "
            "(GECKO_BUYER_WALLET_ADDRESS + GECKO_BUYER_WALLET_PRIVATE_KEY)."
        )

    network = os.environ.get("X402_NETWORK", "base-mainnet")

    _PAID_REQUESTER_SINGLETON = _LiveX402PaidRequester(
        payer_private_key=private_key,
        payer_address=address,
        network=network,
    )
    return _PAID_REQUESTER_SINGLETON


class _LiveX402PaidRequester:
    """Adapter: ``PaidRequester`` Protocol over the x402 v2 buyer dance.

    Why this lives here and not in ``gecko-core``: there is exactly one
    consumer (this CLI) and the buyer dance is a few dozen lines once you
    reuse ``cdp_x402_client._build_payment_payload``. Promoting it to core
    can wait until a second caller appears (per CLAUDE.md "split when there
    are 2 callers").

    Wire flow per ``request(url, query, max_cost_usd, timeout_seconds)``:

      1. GET url with query as ``?q=`` — expect 402.
      2. Parse ``accepts[]`` from the 402 body. Pick the first whose
         ``maxAmountRequired`` (USDC atomic) is ≤ ``max_cost_usd``.
      3. Build a signed X-PAYMENT header via ``_build_payment_payload``.
      4. Re-issue the GET with X-PAYMENT. Resource server settles via its
         own facilitator and returns 200 + body + ``X-PAYMENT-RESPONSE``.
      5. Wrap in ``PaidResponse`` for the caller.
    """

    def __init__(
        self,
        *,
        payer_private_key: str,
        payer_address: str,
        network: str,
    ) -> None:
        self._payer_private_key = payer_private_key
        self._payer_address = payer_address
        self._network = network

    async def request(
        self,
        *,
        url: str,
        query: str,
        max_cost_usd: float,
        timeout_seconds: float,
    ) -> Any:
        # Lazy imports — keep this module importable in dry-run mode
        # without dragging in the x402 SDK.
        import base64

        import httpx
        from gecko_core.payments.cdp_x402_client import (
            _build_payment_payload,
            _build_payment_requirements,
        )
        from gecko_core.sources.paysh_live import PaidResponse

        # Substitute {address} (and warn on other unresolved placeholders) so
        # bazaar wallet-portfolio listings (e.g. Zerion) get a valid URL shape
        # before the 402 probe. Without this, the seller returns 402 with an
        # empty accepts[] and the buyer dance aborts before any payment fires.
        url = _substitute_url_template(url, wallet_address=self._payer_address)

        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            # Step 1: probe for 402.
            params = {"q": query}
            probe = await client.get(url, params=params)
            if probe.status_code == 200:
                # Free endpoint — should not happen for a paid listing,
                # but degrade gracefully by returning the body unpaid.
                text = probe.text
                content_type = probe.headers.get("content-type", "text/plain")
                body: Any = probe.json() if "json" in content_type else text
                return PaidResponse(
                    status_code=200,
                    cost_usd=0.0,
                    response_body=body,
                    response_text=text,
                    content_type=content_type,
                    tx_signature=None,
                    response_sha=hashlib.sha256(text.encode()).hexdigest(),
                    headers=dict(probe.headers),
                )
            if probe.status_code != 402:
                raise RuntimeError(
                    f"expected 402 challenge from {url!r}, got {probe.status_code}: "
                    f"{probe.text[:256]!r}"
                )

            challenge = probe.json()
            accepts = challenge.get("accepts") or []
            if not accepts:
                raise RuntimeError(f"402 from {url!r} carried empty accepts[]")
            chosen = accepts[0]
            advertised_atomic = int(chosen.get("maxAmountRequired") or chosen.get("amount") or "0")
            advertised_usd = Decimal(advertised_atomic) / Decimal(10**6)
            if advertised_usd > Decimal(str(max_cost_usd)):
                raise RuntimeError(
                    f"advertised price {advertised_usd} USD exceeds caller cap {max_cost_usd} USD"
                )

            sdk_requirements = _build_payment_requirements(
                amount_usd=advertised_usd,
                pay_to=chosen["payTo"],
                network=chosen.get("network", self._network),
                asset=chosen["asset"],
                resource_url=url,
                max_timeout_seconds=int(chosen.get("maxTimeoutSeconds", 60)),
            )
            from uuid import uuid4

            intent_id = f"trading-oracle-{uuid4().hex[:16]}"
            sdk_payload = _build_payment_payload(
                intent_id=intent_id,
                requirements=sdk_requirements,
                payer_private_key=self._payer_private_key,
                resource_url=url,
            )
            payload_bytes = json.dumps(
                sdk_payload.model_dump(by_alias=True, mode="json"),
                separators=(",", ":"),
            ).encode("utf-8")
            x_payment = base64.b64encode(payload_bytes).decode("ascii")

            # Step 4: re-GET with X-PAYMENT.
            paid = await client.get(
                url,
                params=params,
                headers={"X-PAYMENT": x_payment, "Accept": "application/json"},
            )
            if paid.status_code != 200:
                raise RuntimeError(
                    f"paid GET {url!r} returned {paid.status_code}: {paid.text[:256]!r}"
                )
            tx_signature: str | None = None
            settle_header = paid.headers.get("X-PAYMENT-RESPONSE")
            if settle_header:
                try:
                    decoded = base64.b64decode(settle_header).decode("utf-8")
                    settle = json.loads(decoded)
                    tx_signature = settle.get("transaction") or settle.get("txHash")
                except Exception:
                    tx_signature = None

            text = paid.text
            content_type = paid.headers.get("content-type", "text/plain")
            try:
                body = paid.json() if "json" in content_type else text
            except Exception:
                body = text
            return PaidResponse(
                status_code=200,
                cost_usd=float(advertised_usd),
                response_body=body,
                response_text=text,
                content_type=content_type,
                tx_signature=tx_signature,
                response_sha=hashlib.sha256(text.encode()).hexdigest(),
                headers=dict(paid.headers),
            )


async def _charge_and_fetch(call: PlannedCall) -> dict[str, Any]:
    """Issue one paid x402 call against the listing's source.

    Routes to ``paysh_live.fetch_paid`` or ``bazaar_live.fetch_paid`` based
    on ``provider_kind``. Both accept a ``PaidRequester`` for the wire
    seam; we build it lazily once per run via ``_build_paid_requester``.

    Returns the dict shape ``execute_plan`` expects:
    ``{"body": <text>, "fqn": <fqn>}``. On a non-text response or
    upstream error, raises — ``execute_plan`` collects into the failure
    list rather than crashing the run.
    """
    # Import the planner-listing lookup helpers + the prompt lazily so
    # dry-run never drags them into the import path.
    from gecko_core.ingestion.trading_oracle.prompt import TRADING_ORACLE_PROMPT

    pk = call.listing.get("provider_kind", "paysh_live")
    fqn = call.listing.get("fqn", "")
    service_url = call.listing.get("service_url", "")
    requester = _build_paid_requester()

    if pk == "paysh_live":
        from gecko_core.sources.paysh_live import fetch_paid as paysh_fetch_paid
        from gecko_core.sources.paysh_manifest import PayshCatalogProvider

        # Reconstruct the minimum PayshCatalogProvider the live module
        # expects, from the planner-listing dict (we drop the catalog
        # passthrough from CLI → dispatcher).
        provider = PayshCatalogProvider(
            fqn=fqn,
            title=str(call.listing.get("name", "")),
            description=str(call.listing.get("description", "")),
            category=str(call.listing.get("category", "")),
            min_price_usd=float(call.listing.get("price_usd", 0.0)),
            service_url=service_url,
        )
        result = await paysh_fetch_paid(
            fqn,
            TRADING_ORACLE_PROMPT,
            x402_client=requester,
            catalog_providers=[provider],
        )
    elif pk == "bazaar_live":
        from gecko_core.sources.bazaar_live import fetch_paid as bazaar_fetch_paid
        from gecko_core.sources.bazaar_manifest import (
            BazaarEndpoint,
            BazaarPricing,
            BazaarService,
        )

        price = Decimal(str(call.listing.get("price_usd", 0.0)))
        endpoint = BazaarEndpoint(
            url=service_url,
            pricing=BazaarPricing(amount=str(price)),
        )
        service = BazaarService(
            id=fqn,
            name=str(call.listing.get("name", "")),
            description=str(call.listing.get("description", "")),
            category=str(call.listing.get("category", "")),
            networks=[],
            endpoints=[endpoint],
        )
        result = await bazaar_fetch_paid(
            fqn,
            TRADING_ORACLE_PROMPT,
            x402_client=requester,
            catalog_services=[service],
        )
    else:
        raise ValueError(f"unknown provider_kind {pk!r}")

    chunks = (result.payload or {}).get("chunks", []) if result else []
    if not chunks:
        raise RuntimeError(f"{pk} fetch_paid for {fqn!r} returned 0 chunks")
    body = chunks[0].get("text") if isinstance(chunks[0], dict) else None
    if not isinstance(body, str) or not body:
        raise RuntimeError(f"{pk} fetch_paid for {fqn!r} returned non-text first chunk")
    return {"body": body, "fqn": fqn}


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
            vertical="dex",
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
