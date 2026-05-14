"""Ingest the Bazaar / Coinbase Agentic Wallet service catalog as corpus.

S30-#43. This is the first committed ingest for ``bazaar_manifest``. The
original 50 chunks shipped under this provider_kind in earlier sprints
came from an un-committed one-off and were purged in S29-#39 Phase 2 for
being too short (per-service blurbs averaging <100 chars failed the
chunk_quality gate). This script is the durable replacement.

Why this exists:
    The Bazaar catalog at https://api.agentic.market/v1/services lists
    744 x402-paid agent services (2026-05-14). For Gecko's retrieval
    corpus, "what x402 services exist for X" and "what does service Y
    cost" are real fixture questions. The catalog is the canonical
    answer — a snapshot encoded as searchable chunks.

Design choices:

    1. **Solana-only filter.** Per S26-#14 lesson (Base-chain Zerion
       payloads polluted Solana-Kamino retrievals), we keep only
       services tagged ``Solana`` in ``networks[]``. Cross-chain noise
       stays out at ingest time, not deprecate-time.

    2. **Bundle 3-5 services per chunk.** Per-service blurbs are
       typically 70-150 chars — well below the chunk_quality gate's
       ``min_chars=200``. We bundle 4 services per chunk with explicit
       ``"Service: <name>"`` separators so chunks land ≥300 chars and
       stay coherent for vector retrieval. 24 Solana services at 4/bundle
       yields ~6 substantive chunks.

    3. **Reuse :mod:`gecko_core.sources.bazaar_manifest`.** The module
       owns the schema (``BazaarCatalog`` / ``BazaarService``), the SSRF
       guard, and the chunk-shape normalization. This script is purely
       the orchestrator: fetch → filter → bundle → embed → insert.

    4. **provider_kind="bazaar_manifest", protocol=().** Cross-cutting
       discovery — the catalog spans every category and protocol. Per
       trade-vertical Pattern, general-knowledge chunks carry empty
       protocol so they surface for all protocol-scoped queries.

    5. **freshness_tier="daily".** The catalog grows; recommended
       cadence is daily re-run. Older snapshots stay durable (no
       deprecate-on-rewrite) — duplicate chunks are skipped by the
       ``(source_id, chunk_index)`` unique key, so a daily cron is
       idempotent on unchanged services.

Run:
    set -a; source .env; set +a   # MONGODB_URI + embedder keys
    uv run python scripts/bazaar/ingest_manifest.py             # full
    uv run python scripts/bazaar/ingest_manifest.py --dry-run   # no writes
    uv run python scripts/bazaar/ingest_manifest.py --limit 8   # cap services
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import click

_REPO = Path(__file__).resolve().parents[2]
_GC_SRC = _REPO / "packages" / "gecko-core" / "src"
if str(_GC_SRC) not in sys.path:
    sys.path.insert(0, str(_GC_SRC))

from gecko_core.db.mongo_chunks import insert_chunks_mongo  # noqa: E402
from gecko_core.ingestion.embedder import embed  # noqa: E402
from gecko_core.sources.bazaar_manifest import (  # noqa: E402
    BAZAAR_CATALOG_URL,
    BazaarCatalog,
    BazaarService,
)

log = logging.getLogger("bazaar.ingest_manifest")

_BAZAAR_MANIFEST_SESSION = uuid5(NAMESPACE_URL, "gecko.bazaar.manifest.session.v1")

# Bundle width. 4 services per chunk × ~80 char average per service =
# ~320 chars per chunk before the framing prose, comfortably above the
# chunk_quality gate's min_chars=200. If the catalog adds shorter blurbs
# in the future, raise this number rather than lowering the gate.
DEFAULT_BUNDLE_SIZE: int = 4

# Page through the catalog at 200/page (Bazaar's max-respected limit).
_PAGE_LIMIT: int = 200


def _is_solana(service: BazaarService) -> bool:
    """True iff the service lists ``Solana`` in ``networks[]``.

    Cross-chain services that include both Base and Solana count as
    Solana — they're callable on Solana, which is what matters for
    Gecko's Solana-first retrieval. Pure ``Base`` and ``Polygon`` are
    filtered out.
    """
    return "Solana" in (service.networks or [])


def _format_service_line(service: BazaarService) -> str:
    """Render one service as a single ``Service: ...`` line.

    Format::

        Service: <name> [<category>] (provider: <provider>; networks: <nets>).
        <description> Pricing: $<avg> USDC.

    Keeps each service self-contained inside the bundled chunk so the
    LLM can attribute citations correctly when only one service in the
    bundle is the relevant answer.
    """
    parts: list[str] = []
    head = f"Service: {service.name or service.id}"
    if service.category:
        head += f" [{service.category}]"
    meta_bits: list[str] = []
    if service.provider:
        meta_bits.append(f"provider: {service.provider}")
    if service.networks:
        meta_bits.append(f"networks: {', '.join(service.networks)}")
    if meta_bits:
        head += f" ({'; '.join(meta_bits)})"
    parts.append(head + ".")

    if service.description:
        parts.append(service.description.rstrip(". ") + ".")

    # Optional pricing tail — only when we have a non-empty avg/min.
    summary = service.priceSummary
    if summary is not None:
        avg = (summary.avgCostPerTransaction or "").strip()
        lo = (summary.minAmount or "").strip()
        hi = (summary.maxAmount or "").strip()
        cur = (summary.currency or "USDC").strip()
        if lo and hi and lo != hi:
            parts.append(f"Pricing: ${lo}–${hi} {cur} per call.")
        elif avg:
            parts.append(f"Pricing: ${avg} {cur} per call.")
        elif lo:
            parts.append(f"Pricing: ${lo} {cur} per call.")

    return " ".join(parts)


def _bundle_services(
    services: list[BazaarService], *, bundle_size: int
) -> list[str]:
    """Bundle services into ``bundle_size``-sized chunk texts.

    Each bundle is prefixed with a one-line catalog header so the LLM
    has a stable identifier to anchor the chunk to its source. Returns
    one string per chunk.
    """
    chunks: list[str] = []
    for start in range(0, len(services), bundle_size):
        group = services[start : start + bundle_size]
        idx_lo = start + 1
        idx_hi = start + len(group)
        header = (
            f"Bazaar x402 service catalog (Coinbase Agentic Wallet) — "
            f"Solana-supporting services {idx_lo}-{idx_hi}. These are "
            f"agent-callable third-party services payable via x402 USDC."
        )
        body = "\n".join(_format_service_line(s) for s in group)
        chunks.append(header + "\n\n" + body)
    return chunks


async def _fetch_all_solana_services(
    *, max_total: int, timeout_seconds: float = 15.0
) -> list[BazaarService]:
    """Page through the catalog and return Solana-tagged services.

    The shared ``fetch_catalog`` helper caps at ``DEFAULT_MAX_ENTRIES``
    (100). We page directly here so the full ~744-entry catalog can be
    surveyed for the ~24 Solana services. SSRF guard is unchanged — we
    only ever hit the canonical catalog URL.
    """
    import httpx

    out: list[BazaarService] = []
    offset = 0
    async with httpx.AsyncClient(
        timeout=timeout_seconds, follow_redirects=False
    ) as http:
        while True:
            url = f"{BAZAAR_CATALOG_URL}?limit={_PAGE_LIMIT}&offset={offset}"
            log.info("FETCH offset=%d limit=%d", offset, _PAGE_LIMIT)
            resp = await http.get(url, headers={"User-Agent": "gecko-ingest/0.1"})
            resp.raise_for_status()
            try:
                catalog = BazaarCatalog.model_validate(resp.json())
            except Exception as exc:  # noqa: BLE001
                log.error("schema validation failed at offset=%d: %s", offset, exc)
                break

            page_services = catalog.services
            solana_here = [s for s in page_services if _is_solana(s)]
            log.info(
                "PAGE offset=%d page_n=%d solana=%d total_catalog=%d",
                offset, len(page_services), len(solana_here), catalog.total,
            )
            out.extend(solana_here)
            if len(out) >= max_total:
                out = out[:max_total]
                break
            offset += _PAGE_LIMIT
            if offset >= catalog.total or not page_services:
                break

    return out


async def amain(
    *, dry_run: bool, limit: int | None, bundle_size: int
) -> int:
    effective_limit = limit if limit is not None else 10_000
    services = await _fetch_all_solana_services(max_total=effective_limit)
    log.info("=== bazaar_manifest ingest: %d Solana services ===", len(services))

    if not services:
        log.warning("no Solana services found — nothing to ingest")
        return 0

    bundled = _bundle_services(services, bundle_size=bundle_size)
    log.info(
        "BUNDLE bundle_size=%d -> %d chunks (lens=%s)",
        bundle_size,
        len(bundled),
        [len(c) for c in bundled],
    )

    if dry_run:
        log.info("DRY-RUN — skipping embed + insert")
        log.info("sample chunk[0]:\n%s", bundled[0][:600])
        return 0

    vectors, _tokens = await embed(bundled)
    rows: list[tuple[int, str, list[float]]] = [
        (i, bundled[i], list(vectors[i])) for i in range(len(bundled))
    ]
    # source_id is deterministic per ingest day so re-runs collide on the
    # unique (source_id, chunk_index) key when the bundling is unchanged.
    # The day-stamp gives us a clean "today's snapshot" granularity
    # without spawning a new source row per service.
    from datetime import UTC, datetime

    day = datetime.now(UTC).strftime("%Y-%m-%d")
    source_id = uuid5(NAMESPACE_URL, f"bazaar.manifest.snapshot.{day}")
    source_url = f"https://agentic.market/services?snapshot={day}"

    inserted = await insert_chunks_mongo(
        session_id=_BAZAAR_MANIFEST_SESSION,
        source_id=source_id,
        chunks=rows,
        category="market_intelligence",
        subcategory="competitor",
        vertical="ai_agent_platform",
        source="bazaar_manifest",
        source_url=source_url,
        freshness_tier="daily",
        protocol=(),
        content_kind="mechanism",
    )
    log.info(
        "=== DONE: %d Solana services bundled into %d chunks; %d new inserts ===",
        len(services), len(bundled), inserted,
    )
    return 0


@click.command()
@click.option("--dry-run", is_flag=True, default=False, help="Fetch + bundle only; no Mongo writes.")
@click.option("--limit", type=int, default=None,
              help="Cap Solana services processed (default: all).")
@click.option("--bundle-size", type=int, default=DEFAULT_BUNDLE_SIZE, show_default=True,
              help="Services per chunk. Raise if catalog blurbs get shorter.")
@click.option("--verbose", "-v", is_flag=True, default=False)
def main(dry_run: bool, limit: int | None, bundle_size: int, verbose: bool) -> None:
    """Ingest the Bazaar Solana-supporting service catalog."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s %(message)s",
    )
    rc = asyncio.run(amain(dry_run=dry_run, limit=limit, bundle_size=bundle_size))
    sys.exit(rc)


if __name__ == "__main__":
    main()
