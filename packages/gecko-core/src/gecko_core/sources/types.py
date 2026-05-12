"""Canonical ``ProviderKind`` Literal — the single source of truth.

S17-WEDGE-DATA-01. Mirrors the SQL ``CHECK`` constraint on
``chunks.provider_kind`` and ``sources.provider_kind`` (migration
``20260502000000_provider_kind.sql``). Drift between this Literal and the
SQL CHECK is caught by ``tests/test_provider_kind_consistency.py`` —
exactly the Pattern A enforcement shape we use for ``PaymentMode``.

Why one Literal here instead of per-provider strings:

  Sprint 16 wired Bazaar / Arxiv / twit.sh dispatchers but their chunks
  never reached the LLM because the ``chunks`` table had no
  ``provider_kind`` column. Path B (S17-WEDGE-WIRE-01) makes the kind a
  first-class column. Per Pattern A, every consumer routes through this
  module — never re-declares.

Note on legacy provider-internal ``provider_kind`` strings:

  ``BazaarChunk.provider_kind`` (``"bazaar:<resource_type>"``) and
  ``arxiv.provider._entry_to_chunk``'s ``"free:arxiv"`` are a *different*
  concept — adapter-internal billing/source tags, not the chunks-table
  kind. Software-engineer's S17-WEDGE-WIRE-02 will translate those to
  the canonical ``ProviderKind`` in the per-provider ``embed_adapter.py``
  before calling ``insert_chunks``. Don't conflate the two; this module
  owns only the chunks-table column values.

Adding a new provider kind:

  1. Add the value to ``ProviderKind`` below.
  2. Ship a migration that extends both
     ``chunks_provider_kind_check`` and ``sources_provider_kind_check``.
  3. The drift test will fail loudly until both sides agree.
"""

from __future__ import annotations

from typing import Final, Literal, get_args

ProviderKind = Literal[
    "web",
    "youtube",
    "bazaar",
    "arxiv",
    "twitsh",
    "hn",
    "reddit",
    "gecko_precedent",
    "judge_corpus",
    # S23-FIX-12 (T1) — marketplace providers. Manifest = catalog/discovery
    # chunks (free), live = paid x402 endpoint responses. Mirrors the four
    # KnowledgeSource literals in ``gecko_core.knowledge.taxonomy``; they
    # are intentionally distinct concepts (KnowledgeSource = where the
    # bytes came from; ProviderKind = the chunks-table column the
    # retrieval router filters on) but for the marketplace family they
    # share the same name to avoid an artificial translation layer.
    "paysh_manifest",
    "paysh_live",
    "bazaar_manifest",
    "bazaar_live",
    # Investor-canon corpus (free + public-domain). Trade-vertical wedge:
    # ground every coach decision in attributed investor-canon citations
    # blended with live on-chain freshness data. See
    # docs/strategy/2026-05-11-trade-vertical-expansion.md §6.
    "canon_marks",       # Howard Marks / Oaktree client memos
    "canon_damodaran",   # Aswath Damodaran / NYU Stern PDFs + posts
    "canon_mauboussin",  # Michael Mauboussin / Morgan Stanley papers
    "canon_youtube",     # Patrick Boyle, Ben Felix, Mauboussin transcripts
    "canon_berkshire",   # Berkshire Hathaway shareholder letters 1965-now
    "canon_macro",       # Fed, BIS, IMF working papers
    # S24 WS-A — live market-data grounding. Pyth historical daily candles
    # + DefiLlama TVL snapshots per protocol. ai-ml-engineer review
    # (2026-05-12): 3/7 panel voices structurally hallucinate without
    # market data; technical_analyst cannot ground on Marks memos. Chunks
    # surface alongside canon (cross-cutting) but tag the protocol so
    # protocol-equality $match also catches them. See
    # docs/strategy/2026-05-12-s24-plan.md §4 WS-A and
    # packages/gecko-core/src/gecko_core/sources/market_data.py.
    "market_data",
]
"""Static type alias for the ``chunks.provider_kind`` /
``sources.provider_kind`` column. Every consumer imports from here."""

PROVIDER_KINDS: Final[tuple[str, ...]] = get_args(ProviderKind)
"""Runtime tuple — used by env validation and schema-drift assertions."""

# --- Freshness tier (Pattern A: SQL CHECK in 20260508130000 mirrors this) ---
# Distinguishes static (protocol docs that age slowly) from daily (paid
# paysh/bazaar snapshots refreshed on a cadence) from live_only (single-call
# results never persisted to the vector store). Drift-guarded by
# ``tests/test_provider_kind_consistency.py::test_freshness_tier_values_match_sql_check``.
# 'hot' (S24 WS-A) = sub-hour cadence market data (Pyth, DefiLlama); embedded
#   into the vector store so the panel can cite it. Distinct from
#   'live_only' which is "fetch on request, never persist".
FreshnessTier = Literal["static", "daily", "live_only", "hot"]
FRESHNESS_TIER_VALUES: tuple[FreshnessTier, ...] = ("static", "daily", "live_only", "hot")

# --- Content kind (Pattern A: SQL CHECK in 20260508140000 mirrors this) ---
# 'quote'      = price/TVL/APY snapshots; 24h TTL.
# 'mechanism'  = protocol architecture, audit reports; 30d TTL.
# 'governance' = proposals, parameter changes; 7d TTL.
ContentKind = Literal["quote", "mechanism", "governance", "unknown"]
CONTENT_KIND_VALUES: tuple[ContentKind, ...] = ("quote", "mechanism", "governance", "unknown")

__all__ = [
    "CONTENT_KIND_VALUES",
    "FRESHNESS_TIER_VALUES",
    "PROVIDER_KINDS",
    "ContentKind",
    "FreshnessTier",
    "ProviderKind",
]
