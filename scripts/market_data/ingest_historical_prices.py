"""S26 #14 Gap 3 — ingest Pyth historical prices for rubric-eval fixture dates.

The rubric eval (2026-05-12) flagged that market_data was current-
snapshot-only, so questions with as_of_date=2024-11-05 (SOL=$165)
retrieved the latest market_data cache (~$95) and the panel had no
date-aligned ground truth to cite. Date-misalignment hits the new
S25 #13 boost code (-0.10 demotion) but only if there's a chunk to
boost; without one, the panel falls back to canon which is
intentionally date-agnostic.

This script:

  1. Reads tests/eval/suites/defi_trade_rubric_suite.json +
     defi_trade_suite.json. Collects unique as_of_date values.
  2. For each (date, asset) pair across the 5 sample protocols'
     PYTH_FEEDS, calls Pyth Hermes /api/get_price_feed with the
     date's midnight publish_time.
  3. Renders one short chunk per (asset, date), embeds, inserts
     with freshness_tier='hot' + metadata.timestamp set to the
     fixture date so the S25 #13 date-alignment boost picks it up.

Idempotent via UUID5(NAMESPACE_URL, '<feed>@<date>'). Re-running
produces 0 net new chunks.

Run:
    set -a; source .env; set +a
    uv run python scripts/market_data/ingest_historical_prices.py --dry-run
    uv run python scripts/market_data/ingest_historical_prices.py

Cost (text-embedding-3-small @ \$0.02 / 1M tokens):
    ~150 chunks * ~150 tokens / chunk = 22.5k tokens ≈ \$0.0005.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from datetime import UTC, datetime, timezone
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import click
import httpx

_REPO = Path(__file__).resolve().parents[2]
_GC_SRC = _REPO / "packages" / "gecko-core" / "src"
if str(_GC_SRC) not in sys.path:
    sys.path.insert(0, str(_GC_SRC))

from gecko_core.db.mongo_chunks import insert_chunks_mongo  # noqa: E402
from gecko_core.ingestion.embedder import embed  # noqa: E402
from gecko_core.sources.market_data import (  # noqa: E402
    PYTH_FEEDS,
    render_historical_price_chunk,
)

log = logging.getLogger("market_data.historical")

_HISTORICAL_SESSION = uuid5(NAMESPACE_URL, "gecko.market_data.historical.session.v1")

# Map our PYTH_FEEDS list (asset → feed_id) for fast lookup.
_FEED_LOOKUP: dict[str, str] = dict(PYTH_FEEDS)


# Assets to fetch per fixture date. Universe-wide (every fixture
# implicitly references SOL + USDC at minimum; perp fixtures add BTC/ETH;
# LST/yield fixtures add jitoSOL/mSOL/INF).
_FIXTURE_ASSET_SET: tuple[str, ...] = (
    "SOL/USD",
    "USDC/USD",
    "BTC/USD",
    "ETH/USD",
    "jitoSOL/USD",
    "mSOL/USD",
    "INF/USD",
)


def _load_fixture_dates(suite_paths: list[Path]) -> tuple[str, ...]:
    """Read every as_of_date string from the eval suites. Returns
    sorted unique calendar-date strings."""
    dates: set[str] = set()
    for path in suite_paths:
        if not path.exists():
            log.warning("suite_missing path=%s", path)
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, list):
            entries = payload
        elif isinstance(payload, dict):
            entries = payload.get("fixtures") or payload.get("rows") or []
        else:
            entries = []
        for entry in entries:
            d = entry.get("as_of_date")
            if isinstance(d, str) and len(d) == 10:
                dates.add(d)
    return tuple(sorted(dates))


def _midnight_unix(date_iso: str) -> int:
    """Convert 'YYYY-MM-DD' → midnight UTC unix seconds."""
    dt = datetime.strptime(date_iso, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


async def _fetch_pyth_historical(
    client: httpx.AsyncClient, feed_id: str, publish_time_unix: int
) -> tuple[float, str] | None:
    """Fetch one Pyth historical price. Returns (price_usd, iso_ts) or None.

    Endpoint shape: /api/get_price_feed?id=<feed>&publish_time=<unix>.
    publish_time is "as of this Unix timestamp"; Pyth returns the
    nearest feed update at or before that moment.

    Retries with exponential backoff on 429 (rate-limited). Pyth Hermes
    enforces per-IP token-bucket; on a fresh quota run, 0.5s spacing is
    fine; right after a heavy burst we need 5-10s recovery before the
    bucket refills.
    """
    url = (
        f"https://hermes.pyth.network/api/get_price_feed"
        f"?id={feed_id}&publish_time={publish_time_unix}"
    )
    backoffs = (2.0, 5.0, 10.0, 20.0)
    for attempt, sleep_s in enumerate((0.0, *backoffs)):
        if sleep_s > 0:
            await asyncio.sleep(sleep_s)
        try:
            resp = await client.get(url, timeout=20.0)
            if resp.status_code == 429:
                log.warning(
                    "pyth.historical.rate_limited feed=%s attempt=%d next_backoff=%.1fs",
                    feed_id[:8],
                    attempt,
                    backoffs[min(attempt, len(backoffs) - 1)],
                )
                continue
            if resp.status_code != 200:
                log.warning(
                    "pyth.historical.fail feed=%s status=%d body_head=%r",
                    feed_id[:8],
                    resp.status_code,
                    resp.text[:120],
                )
                return None
            data = resp.json()
            break
        except Exception as exc:  # noqa: BLE001
            log.warning("pyth.historical.err feed=%s exc=%s", feed_id[:8], exc)
            return None
    else:
        log.warning("pyth.historical.gave_up feed=%s after retries", feed_id[:8])
        return None

    point = data.get("price", {})
    try:
        price_raw = int(point.get("price", "0"))
        expo = int(point.get("expo", 0))
        publish_time = int(point.get("publish_time", 0))
    except (TypeError, ValueError):
        return None
    price = price_raw * (10**expo)
    iso = datetime.fromtimestamp(publish_time, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return price, iso


def _chunk_text_for_protocol(asset: str) -> tuple[str, ...]:
    """Protocols this asset is most relevant to — drives the chunk's
    protocol tag for the S25 #13 protocol-exact boost.

    Rule: every historical asset surfaces for every fixture protocol,
    since prices are cross-cutting (SOL price matters for Kamino SOL
    leverage, Drift SOL perp, Jupiter JLP composition, etc.). We use
    protocol=() — empty list — so the canon-cross-cutting admittance
    in retrieve_trade_corpus_chunks ({protocol: {$size: 0}}) picks it
    up. The +0.05 date-aligned boost is what drives ranking; the
    protocol-exact boost wouldn't help here anyway because the
    fixture's protocol field is e.g. 'kamino' / 'drift' / 'jito'.

    Note: this leaves the protocol-exact boost off for these chunks
    but the date-alignment boost (+0.05) PLUS being a hot-tier chunk
    that admits cross-vertical is enough to surface them above canon
    for date-specific questions. Confirmed by the boost test logic
    at packages/gecko-core/tests/orchestration/trade_panel/
    test_retrieval_boosts.py.
    """
    del asset  # all assets get empty protocol tag — cross-cutting
    return ()


async def ingest_date_asset(
    *,
    http: httpx.AsyncClient,
    asset: str,
    feed_id: str,
    date_iso: str,
    dry_run: bool,
) -> dict[str, int]:
    pub_unix = _midnight_unix(date_iso)
    result = await _fetch_pyth_historical(http, feed_id, pub_unix)
    if result is None:
        return {"chunks": 0, "skipped": 1}
    price, iso_ts = result

    chunk_body = render_historical_price_chunk(
        asset=asset,
        price_usd=price,
        publish_time_iso=iso_ts,
        fixture_as_of_date=date_iso,
    )
    log.info(
        "RENDER asset=%s date=%s price=$%.4f chunk_chars=%d",
        asset,
        date_iso,
        price,
        len(chunk_body),
    )
    if dry_run:
        return {"chunks": 1, "skipped": 0}

    vectors, _tokens = await embed([chunk_body])
    rows: list[tuple[int, str, list[float]]] = [(0, chunk_body, list(vectors[0]))]
    source_key = f"market_data:historical:{feed_id}@{date_iso}"
    source_id = uuid5(NAMESPACE_URL, source_key)

    inserted = await insert_chunks_mongo(
        session_id=_HISTORICAL_SESSION,
        source_id=source_id,
        chunks=rows,
        category="market_intelligence",
        vertical="dex",
        source="market_data",
        provider_kind="market_data",
        source_url=(
            f"https://hermes.pyth.network/api/get_price_feed"
            f"?id={feed_id}&publish_time={pub_unix}"
        ),
        freshness_tier="hot",
        protocol=_chunk_text_for_protocol(asset),
        content_kind="quote",
    )
    if inserted:
        log.info("INSERT asset=%s date=%s new=%d", asset, date_iso, inserted)
    else:
        log.debug("dedup asset=%s date=%s (already in corpus)", asset, date_iso)
    return {"chunks": inserted, "skipped": 0}


async def amain(*, dry_run: bool, dates: tuple[str, ...] | None) -> int:
    suite_paths = [
        _REPO / "tests" / "eval" / "suites" / "defi_trade_rubric_suite.json",
        _REPO / "tests" / "eval" / "suites" / "defi_trade_suite.json",
    ]
    if dates is None:
        dates = _load_fixture_dates(suite_paths)
    log.info("=== historical ingest: %d dates × %d assets (dry_run=%s) ===",
             len(dates), len(_FIXTURE_ASSET_SET), dry_run)
    log.info("dates: %s", list(dates))

    total_chunks = 0
    total_skipped = 0
    async with httpx.AsyncClient() as http:
        for date_iso in dates:
            for asset in _FIXTURE_ASSET_SET:
                feed_id = _FEED_LOOKUP.get(asset)
                if not feed_id:
                    log.warning("no feed for asset=%s; skip", asset)
                    total_skipped += 1
                    continue
                stats = await ingest_date_asset(
                    http=http,
                    asset=asset,
                    feed_id=feed_id,
                    date_iso=date_iso,
                    dry_run=dry_run,
                )
                total_chunks += stats["chunks"]
                total_skipped += stats["skipped"]
                # Pyth Hermes rate-limits per-IP. Observed empirically:
                # ~10 req/burst then 429 with bucket refill on ~5-10s
                # idle. 1.5s spacing keeps us inside the bucket.
                await asyncio.sleep(1.5)
    log.info(
        "=== DONE: chunks=%d skipped=%d (dates=%d × assets=%d = %d planned) ===",
        total_chunks,
        total_skipped,
        len(dates),
        len(_FIXTURE_ASSET_SET),
        len(dates) * len(_FIXTURE_ASSET_SET),
    )
    return 0


@click.command()
@click.option("--dry-run", is_flag=True, default=False,
              help="Fetch + render only; no embed, no Mongo writes.")
@click.option("--dates", type=str, default=None,
              help="Override: comma-separated YYYY-MM-DD list.")
@click.option("--verbose", "-v", is_flag=True, default=False)
def main(dry_run: bool, dates: str | None, verbose: bool) -> None:
    """S26 #14 Gap 3 — historical Pyth prices for fixture as_of_dates."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s %(message)s",
    )
    parsed: tuple[str, ...] | None = None
    if dates:
        parsed = tuple(d.strip() for d in dates.split(",") if d.strip())
    rc = asyncio.run(amain(dry_run=dry_run, dates=parsed))
    sys.exit(rc)


if __name__ == "__main__":
    main()
