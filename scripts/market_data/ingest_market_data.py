"""S24 WS-A — fetch + chunk + embed Pyth historical + DefiLlama TVL.

Mirrors the shape of ``scripts/canon/ingest_marks.py``. Wires market
data into the corpus under ``provider_kind="market_data"``,
``freshness_tier="hot"`` so the trade-panel's chart-reading voices have
real numbers to cite.

Idempotent: ``source_id = UUID5(NAMESPACE_URL, "<endpoint>#<as_of_hour>")``.
Re-running within the same hour produces 0 net new chunks via the
unique index on ``(source_id, chunk_index)``. The hour-boundary key
matches the intended 60-minute refresh cadence — finer-grained
re-ingests within the hour are deduped.

Run:
    set -a; source .env; set +a   # MONGODB_URI + embedder keys
    uv run python scripts/market_data/ingest_market_data.py --protocols kamino,drift,jupiter,sanctum,marinade

Day-1 smoke (5 sample protocols, fresh ingest):
    uv run python scripts/market_data/ingest_market_data.py --sample

The ``--dry-run`` flag skips embed + Mongo writes — useful to verify the
upstream Pyth/DefiLlama endpoints respond before spending tokens.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import click
import httpx

_REPO = Path(__file__).resolve().parents[2]
_GC_SRC = _REPO / "packages" / "gecko-core" / "src"
if str(_GC_SRC) not in sys.path:
    sys.path.insert(0, str(_GC_SRC))

from gecko_core.db.mongo_chunks import insert_chunks_mongo  # noqa: E402
from gecko_core.ingestion.chunker import chunk  # noqa: E402
from gecko_core.ingestion.embedder import embed  # noqa: E402
from gecko_core.sources.market_data import (  # noqa: E402
    SAMPLE_PROTOCOLS_S24,
    MarketDataPlan,
    build_market_data_plan,
    render_candle_chunk,
    render_tvl_chunk,
)

log = logging.getLogger("market_data.ingest")

# Stable session namespace — every market_data row across runs belongs to
# this session_id. Per Pattern F, retrieval is scoped by provider_kind +
# protocol, not session_id, so this is purely provenance.
_MARKET_DATA_SESSION = uuid5(NAMESPACE_URL, "gecko.market_data.session.v1")


def _hour_bucket(now: datetime) -> str:
    return now.strftime("%Y-%m-%dT%H:00:00Z")


async def _fetch_pyth_candles(
    client: httpx.AsyncClient, feed_id: str
) -> str:
    """Fetch latest price update for a feed and render as a CSV-in-prose stub.

    NOTE: Pyth Hermes does not currently expose a true historical-candle
    endpoint — production WS-A will replace this with a Birdeye OHLCV
    pull keyed off the same feed metadata. For the Day-1 smoke we render
    the latest price as a single-row CSV so the embed + insert path is
    exercised end-to-end; full 30-day window lands Day 2-3.
    """
    url = (
        "https://hermes.pyth.network/api/latest_price_feeds"
        f"?ids[]={feed_id}&verbose=false&binary=false"
    )
    resp = await client.get(url, timeout=20.0)
    resp.raise_for_status()
    data = resp.json()
    if not data:
        return "date,open,high,low,close,volume_proxy\n(no data returned)"
    point = data[0].get("price", {})
    price_raw = int(point.get("price", "0"))
    expo = int(point.get("expo", 0))
    publish_time = int(point.get("publish_time", 0))
    px = price_raw * (10**expo)
    iso = datetime.fromtimestamp(publish_time, tz=UTC).strftime("%Y-%m-%d")
    return (
        "date,open,high,low,close,volume_proxy\n"
        f"{iso},{px:.6f},{px:.6f},{px:.6f},{px:.6f},n/a"
    )


async def _fetch_defillama_tvl(
    client: httpx.AsyncClient, defillama_slug: str
) -> dict[str, float]:
    url = f"https://api.llama.fi/protocol/{defillama_slug}"
    resp = await client.get(url, timeout=30.0)
    resp.raise_for_status()
    payload = resp.json()
    tvl_series = payload.get("tvl") or []
    if not tvl_series:
        return {"tvl_usd": 0.0, "tvl_7d_delta_pct": 0.0, "tvl_30d_delta_pct": 0.0}
    latest = float(tvl_series[-1].get("totalLiquidityUSD") or 0.0)

    def _delta(days_back: int) -> float:
        if len(tvl_series) <= days_back:
            return 0.0
        prior = float(tvl_series[-1 - days_back].get("totalLiquidityUSD") or 0.0)
        if prior == 0:
            return 0.0
        return ((latest - prior) / prior) * 100.0

    return {
        "tvl_usd": latest,
        "tvl_7d_delta_pct": _delta(7),
        "tvl_30d_delta_pct": _delta(30),
    }


async def ingest_protocol(
    plan: MarketDataPlan, *, dry_run: bool
) -> dict[str, int]:
    """Fetch + render + insert market_data chunks for one protocol."""
    now = datetime.now(UTC)
    hour_iso = _hour_bucket(now)
    chunk_texts: list[str] = []
    sources_in_play: list[str] = []

    async with httpx.AsyncClient() as http:
        for cp in plan.candles:
            try:
                csv = await _fetch_pyth_candles(http, cp.feed_id)
            except Exception as exc:
                log.warning("FAIL pyth feed=%s err=%s", cp.asset, exc)
                continue
            chunk_texts.append(render_candle_chunk(cp.asset, csv, hour_iso))
            sources_in_play.append(f"pyth:{cp.asset}")
        try:
            tvl = await _fetch_defillama_tvl(http, plan.tvl.defillama_slug)
            chunk_texts.append(
                render_tvl_chunk(
                    plan.protocol,
                    tvl["tvl_usd"],
                    tvl["tvl_7d_delta_pct"],
                    tvl["tvl_30d_delta_pct"],
                    hour_iso,
                )
            )
            sources_in_play.append(f"defillama:{plan.tvl.defillama_slug}")
        except Exception as exc:
            log.warning("FAIL defillama protocol=%s err=%s", plan.protocol, exc)

    log.info(
        "RENDER protocol=%s chunks=%d sources=%s",
        plan.protocol,
        len(chunk_texts),
        ",".join(sources_in_play),
    )

    if dry_run or not chunk_texts:
        return {"chunks": len(chunk_texts), "skipped": 0 if chunk_texts else 1}

    # All market_data chunks for one protocol+hour live under a single
    # synthetic source_id so the (source_id, chunk_index) dedup works
    # cleanly across re-runs within the hour.
    source_key = f"market_data:{plan.protocol}#{hour_iso}"
    source_id = uuid5(NAMESPACE_URL, source_key)

    # Re-chunk for safety; market_data render is already small but the
    # chunker is what owns the embed-budget contract.
    rechunked: list[str] = []
    for text in chunk_texts:
        rechunked.extend(chunk(text) or [text])

    vectors, _tokens = await embed(rechunked)
    rows: list[tuple[int, str, list[float]]] = [
        (i, rechunked[i], list(vectors[i])) for i in range(len(rechunked))
    ]

    inserted = await insert_chunks_mongo(
        session_id=_MARKET_DATA_SESSION,
        source_id=source_id,
        chunks=rows,
        category="market_intelligence",
        vertical="dex",
        source="market_data",
        provider_kind="market_data",
        source_url=f"https://gecko.local/market_data/{plan.protocol}/{hour_iso}",
        freshness_tier="hot",
        protocol=(plan.protocol,),
        content_kind="quote",
    )
    log.info(
        "INSERT protocol=%s new_chunks=%d (hour_bucket=%s)",
        plan.protocol,
        inserted,
        hour_iso,
    )
    return {"chunks": inserted, "skipped": 0}


async def amain(
    *, protocols: list[str], dry_run: bool, sleep_seconds: float
) -> int:
    log.info(
        "=== market_data ingest: %d protocols (dry_run=%s) ===",
        len(protocols),
        dry_run,
    )
    total_chunks = 0
    total_skipped = 0
    for i, proto in enumerate(protocols, start=1):
        log.info("[%d/%d] %s", i, len(protocols), proto)
        try:
            plan = build_market_data_plan(proto)
        except KeyError as exc:
            log.warning("SKIP %s reason=%s", proto, exc)
            total_skipped += 1
            continue
        stats = await ingest_protocol(plan, dry_run=dry_run)
        total_chunks += stats["chunks"]
        total_skipped += stats["skipped"]
        if i < len(protocols):
            await asyncio.sleep(sleep_seconds)
    log.info(
        "=== DONE: protocols=%d chunks=%d skipped=%d ===",
        len(protocols),
        total_chunks,
        total_skipped,
    )
    return 0


@click.command()
@click.option(
    "--protocols",
    type=str,
    default=None,
    help="Comma-separated list of protocol slugs. Mutually exclusive with --sample.",
)
@click.option(
    "--sample",
    is_flag=True,
    default=False,
    help="Ingest the 5 SAMPLE_PROTOCOLS_S24 (kamino, drift, jupiter, sanctum, marinade).",
)
@click.option(
    "--dry-run", is_flag=True, default=False,
    help="Fetch + render only; no embed, no Mongo writes.",
)
@click.option(
    "--sleep-seconds", type=float, default=0.5, show_default=True,
)
@click.option("--verbose", "-v", is_flag=True, default=False)
def main(
    protocols: str | None,
    sample: bool,
    dry_run: bool,
    sleep_seconds: float,
    verbose: bool,
) -> None:
    """S24 WS-A — ingest market_data chunks for the trade-panel."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s %(message)s",
    )
    if sample and protocols:
        raise click.UsageError("--sample and --protocols are mutually exclusive")
    chosen: list[str]
    if sample:
        chosen = list(SAMPLE_PROTOCOLS_S24)
    elif protocols:
        chosen = [p.strip() for p in protocols.split(",") if p.strip()]
    else:
        raise click.UsageError("provide --sample or --protocols")
    rc = asyncio.run(
        amain(protocols=chosen, dry_run=dry_run, sleep_seconds=sleep_seconds)
    )
    sys.exit(rc)


if __name__ == "__main__":
    main()
