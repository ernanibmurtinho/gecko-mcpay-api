"""S26 #14 Gap 2 — validate existing bazaar_live chunks are Solana-only.

Per the rubric eval (2026-05-12), bazaar_live Kamino chunks were Zerion
**Base chain** portfolio metadata that happened to mention 'kamino' in
the payload. The retrieval picked them up via vector similarity, but
they're useless for Solana-Kamino questions — wrong chain entirely.

This script is the one-off diagnostic + remediation Pattern B step
(CLAUDE.md). It does NOT add runtime validation — the ingest-time
chain filter (in :mod:`gecko_core.sources.bazaar_live`) is the
enforcement point for future writes. This script:

  1. Lists every bazaar_live chunk in Mongo and inspects payload for
     cross-chain markers ('"chain":"base"', '0x'-prefixed wallet
     addresses, 'positions_distribution_by_chain":{"base"').
  2. Counts the offenders per protocol.
  3. With --remediate, stamps offending chunks with
     metadata.deprecated=True so retrieval drops them via the
     {"metadata.deprecated": {"$ne": True}} filter already in
     ``retrieve_trade_corpus_chunks``.

Run:
    set -a; source .env; set +a
    uv run python scripts/bazaar/validate_solana_only.py            # dry diagnose
    uv run python scripts/bazaar/validate_solana_only.py --remediate # stamp deprecated=True
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
from pathlib import Path

import click

_REPO = Path(__file__).resolve().parents[2]
_GC_SRC = _REPO / "packages" / "gecko-core" / "src"
if str(_GC_SRC) not in sys.path:
    sys.path.insert(0, str(_GC_SRC))

from gecko_core.db.mongo import chunks_collection  # noqa: E402

log = logging.getLogger("bazaar.validate_solana_only")


# Cross-chain markers. A bazaar_live chunk that matches ANY of these is
# considered off-chain (i.e. not Solana). The markers are intentionally
# strict — false-positives here mean a Solana-tagged chunk gets
# deprecated. False-negatives mean cross-chain noise stays in the corpus.
# We err toward strict (more deprecations) because the corpus already
# carries paysh_live + protocol_native for the genuine Solana content.
_BASE_CHAIN_MARKERS: tuple[re.Pattern[str], ...] = (
    re.compile(r'"chain"\s*:\s*"base"', re.IGNORECASE),
    re.compile(r'positions_distribution_by_chain"\s*:\s*\{\s*"base"', re.IGNORECASE),
    re.compile(r'\bzerion\.io/v1/wallets/0x', re.IGNORECASE),
    # 0x-prefixed Ethereum-style wallet addresses (40 hex chars).
    re.compile(r'\b0x[a-fA-F0-9]{40}\b'),
)

# Solana positive markers — if present, the chunk is genuinely Solana
# even if it co-mentions "base" (e.g. "base APY"). These are checked
# AFTER the base-chain markers as a salvage step.
_SOLANA_MARKERS: tuple[re.Pattern[str], ...] = (
    re.compile(r'\bSo11111111111111111111111111111111111111112\b'),  # SOL mint
    re.compile(r'\bEPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v\b'),  # USDC mint Sol
    re.compile(r'\b[1-9A-HJ-NP-Za-km-z]{32,44}\b'),  # base58 Solana pubkey shape
)


def _classify(text: str) -> str:
    """Return one of: solana | base_chain | ambiguous.

    Strategy:
      * Check base-chain markers first; if any present, classify as
        base_chain unless a Solana mint hash is also present (then
        ambiguous — likely a mixed-chain catalog response).
      * If no base markers but Solana base58 pubkey shape present →
        solana.
      * Otherwise ambiguous (likely free-form text with no chain
        signal — we leave these alone).
    """
    base_hit = any(p.search(text) for p in _BASE_CHAIN_MARKERS)
    solana_mint = any(_SOLANA_MARKERS[i].search(text) for i in (0, 1))
    if base_hit and solana_mint:
        return "ambiguous"
    if base_hit:
        return "base_chain"
    if _SOLANA_MARKERS[2].search(text):
        return "solana"
    return "ambiguous"


async def amain(*, remediate: bool) -> int:
    coll = chunks_collection()
    if coll is None:
        log.error("chunks_collection unavailable — set MONGODB_URI + MONGODB_DB")
        return 2

    total = await coll.count_documents({"provider_kind": "bazaar_live"})
    log.info("scanning bazaar_live chunks (n=%d)", total)

    counts: dict[str, int] = {"solana": 0, "base_chain": 0, "ambiguous": 0}
    by_protocol_base: dict[str, int] = {}
    base_chunk_ids: list[object] = []

    async for doc in coll.find({"provider_kind": "bazaar_live"}):
        text = doc.get("text") or ""
        cls = _classify(text)
        counts[cls] += 1
        if cls == "base_chain":
            proto_field = doc.get("protocol") or []
            proto_key = proto_field[0] if isinstance(proto_field, list) and proto_field else (
                proto_field if isinstance(proto_field, str) else "none"
            )
            by_protocol_base[proto_key] = by_protocol_base.get(proto_key, 0) + 1
            base_chunk_ids.append(doc["_id"])

    log.info("classification: %s", counts)
    log.info("base_chain by protocol: %s", by_protocol_base)

    if not remediate:
        log.info(
            "(dry-run) would stamp metadata.deprecated=True on %d chunks; "
            "rerun with --remediate to apply",
            len(base_chunk_ids),
        )
        return 0

    if not base_chunk_ids:
        log.info("no base_chain chunks to remediate — exiting clean")
        return 0

    result = await coll.update_many(
        {"_id": {"$in": base_chunk_ids}},
        {"$set": {"metadata.deprecated": True, "metadata.deprecated_reason": "cross_chain_base_noise_s26_14_gap_2"}},
    )
    log.info(
        "REMEDIATE matched=%d modified=%d — chunks now excluded from retrieval "
        "via {metadata.deprecated: {$ne: True}} filter",
        result.matched_count,
        result.modified_count,
    )
    return 0


@click.command()
@click.option(
    "--remediate",
    is_flag=True,
    default=False,
    help="Stamp metadata.deprecated=True on base-chain bazaar_live chunks.",
)
@click.option("--verbose", "-v", is_flag=True, default=False)
def main(remediate: bool, verbose: bool) -> None:
    """S26 #14 Gap 2 — diagnose + optionally remediate cross-chain bazaar noise."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s %(message)s",
    )
    rc = asyncio.run(amain(remediate=remediate))
    sys.exit(rc)


if __name__ == "__main__":
    main()
