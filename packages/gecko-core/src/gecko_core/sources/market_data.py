"""Market-data source — Pyth historical candles + DefiLlama TVL snapshots.

S24 WS-A. Wires live market data into the corpus under
``provider_kind="market_data"``, ``freshness_tier="hot"`` so the
trade-panel's chart-reading voices (technical_analyst, sentiment-via-news,
fundamental-via-TVL) have real numbers to cite instead of hallucinating
price levels from Howard Marks memos.

Design notes:
  - Pyth: 30-day windows aggregated to daily OHLC candles. One chunk per
    feed = one chunk per (protocol, asset) pair. Sized to keep total
    chunk count below ~5 per protocol per refresh.
  - DefiLlama: per-protocol TVL snapshot (current + 7d delta + 30d delta).
    One chunk per protocol per refresh.
  - Cadence: refresh every 60 minutes (a separate scheduled job, not
    this module's concern). Idempotent re-ingest via UUID5(NAMESPACE_URL,
    "<feed_url>#<as_of_iso>") source_id, which dedupes per (feed, hour).
  - ``protocol`` is set to the specific Solana protocol slug
    (``kamino`` / ``drift`` / ``jupiter`` / …). The trade-panel
    retrieval $match (Pattern F) admits market_data chunks via the
    provider_kind clause regardless of protocol tagging, so cross-protocol
    macro signals still surface.

Sample protocols ingested in the DE-4 hand-off run:
  - Kamino USDC reserve (mainnet)
  - Drift SOL perp (mainnet)
  - Jupiter jitoSOL LST rotation
  - Sanctum INF unstake
  - Marinade mSOL stake

This module exposes a small synchronous API — actual fetch + chunk +
embed + insert lives in :mod:`scripts.market_data.ingest_market_data`,
which composes the same ``insert_chunks_mongo`` path the canon ingesters
use. The functions here return structured plans the script consumes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final, Literal

# --- Pyth feed catalog ----------------------------------------------------
# Solana mainnet feed IDs sourced from https://pyth.network/developers/price-feed-ids
# Snapshot 2026-05-12. Re-run scrape periodically — feed-id churn is rare
# but does happen on asset relistings.

PYTH_PRICE_FEED_HOST: Final[str] = "https://hermes.pyth.network/api"

# (asset, feed_id) — Solana mainnet, USD-quoted.
PYTH_FEEDS: Final[tuple[tuple[str, str], ...]] = (
    ("SOL/USD", "0xef0d8b6fda2ceba41da15d4095d1da392a0d2f8ed0c6c7bc0f4cfac8c280b56d"),
    ("USDC/USD", "0xeaa020c61cc479712813461ce153894a96a6c00b21ed0cfc2798d1f9a9e9c94a"),
    ("BTC/USD", "0xe62df6c8b4a85fe1a67db44dc12de5db330f7ac66b72dc658afedf0f4a415b43"),
    ("ETH/USD", "0xff61491a931112ddf1bd8147cd1b641375f79f5825126d665480874634fd0ace"),
    ("jitoSOL/USD", "0x67be9f519b95cf24338801051f9a808eff0a578ccb388db73b7f6fe1de019ffb"),
    ("mSOL/USD", "0xc2289a6a43d2ce91c6f55caec370f4acc38a2ed477f58813334c6d03749ff2a4"),
    ("INF/USD", "0xf51570985c642c49c2d6e50156390fdba80bb6d5f7fa389d2f012ced4f7d208f"),
)


# --- DefiLlama protocol slugs --------------------------------------------
# https://api.llama.fi/protocol/<slug> returns the JSON snapshot. Mapping
# is a hand-curated 1:1 (DefiLlama slug ↔ Gecko protocol slug).

DEFILLAMA_API_HOST: Final[str] = "https://api.llama.fi"

DEFILLAMA_PROTOCOL_SLUGS: Final[dict[str, str]] = {
    # gecko_slug -> defillama_slug
    "kamino": "kamino-lend",
    "drift": "drift",
    "jupiter": "jupiter-aggregator",
    "sanctum": "sanctum-router",
    "marinade": "marinade-finance",
}


# --- Sample protocols for the DE-4 smoke ingest ---------------------------

SAMPLE_PROTOCOLS_S24: Final[tuple[str, ...]] = (
    "kamino",
    "drift",
    "jupiter",
    "sanctum",
    "marinade",
)
"""Five protocols ingested at S24 Day-1 to validate the market_data path.
Full sweep over the protocol catalog lands Day 2-3."""


# --- Plan dataclasses (script consumes these) ----------------------------

CandleInterval = Literal["1d", "1h"]


@dataclass(frozen=True)
class PythCandlePlan:
    """One Pyth historical candle window to fetch + chunk."""

    asset: str
    feed_id: str
    interval: CandleInterval = "1d"
    window_days: int = 30

    @property
    def url(self) -> str:
        # Pyth Hermes historical-prices endpoint shape (v2 spec).
        return (
            f"{PYTH_PRICE_FEED_HOST}/v2/updates/price/latest"
            f"?ids[]={self.feed_id}&parsed=true"
        )


@dataclass(frozen=True)
class DefiLlamaTVLPlan:
    """One DefiLlama TVL snapshot to fetch + chunk."""

    gecko_protocol_slug: str
    defillama_slug: str

    @property
    def url(self) -> str:
        return f"{DEFILLAMA_API_HOST}/protocol/{self.defillama_slug}"


@dataclass(frozen=True)
class MarketDataPlan:
    """Composite plan for one protocol — N Pyth feeds + 1 TVL snapshot."""

    protocol: str
    candles: tuple[PythCandlePlan, ...]
    tvl: DefiLlamaTVLPlan
    notes: list[str] = field(default_factory=list)


# --- Plan builders --------------------------------------------------------


def pyth_plans_for_protocol(protocol: str) -> tuple[PythCandlePlan, ...]:
    """Return the Pyth feeds relevant to a given protocol.

    Curated mapping — kept narrow (≤ 3 feeds per protocol) to bound chunk
    count. Generic feeds (SOL/USD, USDC/USD) are included for every
    protocol since they're table-stakes context for any Solana strategy.
    """
    by_protocol: dict[str, tuple[str, ...]] = {
        "kamino": ("SOL/USD", "USDC/USD"),
        "drift": ("SOL/USD", "BTC/USD", "ETH/USD"),
        "jupiter": ("SOL/USD", "jitoSOL/USD"),
        "sanctum": ("SOL/USD", "INF/USD"),
        "marinade": ("SOL/USD", "mSOL/USD"),
    }
    assets = by_protocol.get(protocol, ("SOL/USD", "USDC/USD"))
    feed_lookup = dict(PYTH_FEEDS)
    return tuple(
        PythCandlePlan(asset=a, feed_id=feed_lookup[a]) for a in assets if a in feed_lookup
    )


def build_market_data_plan(protocol: str) -> MarketDataPlan:
    """Compose Pyth + DefiLlama plan for one protocol slug."""
    defillama_slug = DEFILLAMA_PROTOCOL_SLUGS.get(protocol)
    if defillama_slug is None:
        raise KeyError(
            f"protocol={protocol!r} has no DefiLlama slug mapping; "
            f"add it to DEFILLAMA_PROTOCOL_SLUGS."
        )
    return MarketDataPlan(
        protocol=protocol,
        candles=pyth_plans_for_protocol(protocol),
        tvl=DefiLlamaTVLPlan(
            gecko_protocol_slug=protocol,
            defillama_slug=defillama_slug,
        ),
    )


def all_sample_plans() -> tuple[MarketDataPlan, ...]:
    """Build plans for the 5 sample S24 protocols."""
    return tuple(build_market_data_plan(p) for p in SAMPLE_PROTOCOLS_S24)


# --- Text rendering (chunk body) -----------------------------------------
# Free-form natural-language rendering of the structured data, embedded
# into the corpus. The retrieval surface is text, not JSON — voices need
# prose to ground a citation in.


def render_candle_chunk(asset: str, candles_csv: str, as_of_iso: str) -> str:
    """Render a daily-candle window as prose + CSV-in-prose.

    The CSV-in-prose shape keeps the chunk text-only (Atlas Vector Search
    embeddings) while giving the voice an unambiguous reference to quote.
    """
    return (
        f"Pyth daily candles for {asset} (last 30 days, as of {as_of_iso}).\n"
        f"Format: date,open,high,low,close,volume_proxy\n"
        f"{candles_csv}\n"
        f"Source: Pyth Network Hermes API. Aggregation: 1-day OHLC from "
        f"per-second updates. Provider: market_data."
    )


def render_tvl_chunk(
    protocol: str,
    tvl_usd: float,
    tvl_7d_delta_pct: float,
    tvl_30d_delta_pct: float,
    as_of_iso: str,
) -> str:
    return (
        f"DefiLlama TVL snapshot for {protocol} (as of {as_of_iso}).\n"
        f"Current TVL: ${tvl_usd:,.0f} USD.\n"
        f"7-day change: {tvl_7d_delta_pct:+.2f}%.\n"
        f"30-day change: {tvl_30d_delta_pct:+.2f}%.\n"
        f"Source: DefiLlama. Provider: market_data."
    )


__all__ = [
    "DEFILLAMA_API_HOST",
    "DEFILLAMA_PROTOCOL_SLUGS",
    "PYTH_FEEDS",
    "PYTH_PRICE_FEED_HOST",
    "SAMPLE_PROTOCOLS_S24",
    "DefiLlamaTVLPlan",
    "MarketDataPlan",
    "PythCandlePlan",
    "all_sample_plans",
    "build_market_data_plan",
    "pyth_plans_for_protocol",
    "render_candle_chunk",
    "render_tvl_chunk",
]
