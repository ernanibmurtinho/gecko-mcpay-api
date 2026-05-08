"""Curated prompt + listing filter for the trading-oracle ingest run.

Scope: Solana DeFi only. Multi-chain and CEX-news scope deliberately
excluded — see docs/superpowers/specs/2026-05-08-trading-oracle-reference-skill-design.md §4.
"""

from __future__ import annotations

from collections.abc import Mapping

SOLANA_DEFI_PROTOCOLS: tuple[str, ...] = (
    "Jupiter",
    "Kamino",
    "Jito",
    "Pyth",
    "Drift",
    "Orca",
    "Raydium",
    "Meteora",
    "MarginFi",
    "Sanctum",
)

TRADING_ORACLE_PROMPT: str = (
    "Acting as a Solana DeFi trading research oracle: for the protocols "
    + ", ".join(SOLANA_DEFI_PROTOCOLS)
    + ", retrieve and summarize current operational facts that affect a trader's "
    "decision-making — pool TVL trends, fee tiers, oracle staleness windows, "
    "recent governance / parameter changes, audit status, known incident history "
    "within the last 90 days, and integration partners. Cite source per fact. "
    "Do not produce trade recommendations; produce parameters a trader's agent "
    "needs to reason."
)

_SOLANA_TOKENS = ("solana", "spl", "anchor")
_DEFI_TOKENS = (
    "defi",
    "dex",
    "lending",
    "lst",
    "perp",
    "perps",
    "oracle",
    "liquidity",
    "amm",
    "staking",
    "yield",
)
_EVM_REJECT_TOKENS = (
    "ethereum",
    "evm",
    "arbitrum",
    "base",
    "polygon",
    "bsc",
    "optimism",
    "avalanche",
)


def _haystack(listing: Mapping[str, object]) -> str:
    parts: list[str] = []
    for key in ("name", "description"):
        v = listing.get(key)
        if isinstance(v, str):
            parts.append(v)
    tags = listing.get("tags")
    if isinstance(tags, (list, tuple)):
        parts.extend(str(t) for t in tags)
    proto_match = any(p.lower() in " ".join(parts).lower() for p in SOLANA_DEFI_PROTOCOLS)
    if proto_match:
        parts.append("__protocol_match__")
    return " ".join(parts).lower()


def is_solana_defi_relevant(listing: Mapping[str, object]) -> bool:
    h = _haystack(listing)
    if any(t in h for t in _EVM_REJECT_TOKENS) and not any(s in h for s in _SOLANA_TOKENS):
        return False
    if "__protocol_match__" in h:
        return True
    has_solana = any(t in h for t in _SOLANA_TOKENS)
    has_defi = any(t in h for t in _DEFI_TOKENS)
    return has_solana and has_defi
