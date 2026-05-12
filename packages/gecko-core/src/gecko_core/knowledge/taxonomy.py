"""Single source of truth for the categorized-knowledge taxonomy (S20-A1).

The Mongo chunk store partitions on TWO orthogonal axes: a knowledge
``Category`` (7 dimensions) and an app ``Vertical`` (~11 seeded). Every
chunk written carries both, plus a ``KnowledgeSource`` indicating where
the raw text came from.

Per Pattern A in CLAUDE.md, every consumer imports from this module —
no string-typed verticals or categories anywhere downstream. The
schema-drift test in ``tests/knowledge/test_taxonomy_consistency.py``
mirrors ``tests/test_payment_mode_consistency.py`` and asserts the
constant tuples and Literal definitions cannot drift.

Subcategories are NOT a Literal — they are free-form strings validated
at write time against ``SUBCATEGORIES``. Per the brainstorm decision,
``regulated`` lives as ``business_financial.regulatory`` (subcategory),
not a top-level Category.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final, Literal, TypedDict

# ---------------------------------------------------------------------------
# Knowledge category — the 7-dimension partition.
# ---------------------------------------------------------------------------

Category = Literal[
    "market_intelligence",
    "business_financial",
    "investment_signals",
    "product",
    "technical_engineering",
    "ai_ml",
    "design_ux",
    "legacy_uncategorized",
]
"""Static type alias. Keep in sync with CATEGORIES manually — the
schema-drift test verifies they match (typing.get_args(Category)).

``legacy_uncategorized`` (S20-A3) is a RESERVED bucket — chunks that
predate the A2 categorized-knowledge fields are stamped with this
category by the ``scripts/mongo/s20_legacy_revoke.py`` one-shot. New
ingestions MUST NOT use it; the Mongo write path raises
``ChunkValidationError(kind='invalid_category')`` if they do, unless
``metadata.deprecated=True`` is set explicitly (the revoke script's
own write path)."""

CATEGORIES: Final[tuple[Category, ...]] = (
    "market_intelligence",
    "business_financial",
    "investment_signals",
    "product",
    "technical_engineering",
    "ai_ml",
    "design_ux",
    "legacy_uncategorized",
)
"""Runtime tuple — used by Mongo validators and runtime enum checks.

The 7 canonical knowledge dimensions plus the reserved S20-A3
``legacy_uncategorized`` bucket (see Category docstring)."""

LEGACY_CATEGORY: Final[Category] = "legacy_uncategorized"
"""Single source of truth for the legacy bucket name. Use this constant
instead of the bare string anywhere downstream (Pattern A)."""


# ---------------------------------------------------------------------------
# App vertical — the ~11-cell seed taxonomy.
# ---------------------------------------------------------------------------

Vertical = Literal[
    "neobank",
    "dex",
    "marketplace",
    "prediction_market",
    "indexer",
    "b2b_saas",
    "consumer_social",
    "ai_agent_platform",
    "gaming",
    "infra_devtool",
    "unknown",
]
"""Static type alias. Keep in sync with VERTICALS manually."""

VERTICALS: Final[tuple[Vertical, ...]] = (
    "neobank",
    "dex",
    "marketplace",
    "prediction_market",
    "indexer",
    "b2b_saas",
    "consumer_social",
    "ai_agent_platform",
    "gaming",
    "infra_devtool",
    "unknown",
)
"""Runtime tuple — ``unknown`` is the catch-all when classification fails."""


# ---------------------------------------------------------------------------
# Knowledge source — where the chunk's raw text originated.
# ---------------------------------------------------------------------------

KnowledgeSource = Literal[
    "web",
    "tavily",
    "twit_sh",
    "bazaar",
    "bazaar_manifest",
    "bazaar_live",
    "paysh_manifest",
    "paysh_live",
    "user_query",
    "enriched_output",
    "market_data",
]
"""Static type alias. Keep in sync with KNOWLEDGE_SOURCES manually.

S22-N1 — ``pay_sh`` was split into two distinct sources:
``paysh_manifest`` (discovery from pay.sh's
``/.well-known/agent-skills/index.json``) and ``paysh_live`` (live
x402-paid endpoint responses). Existing chunks persisted with the old
``pay_sh`` literal are coerced to ``paysh_live`` on read by
:func:`_coerce_legacy_source`. Removed in S23 — see ``# S23 cleanup``.

S22-BAZAAR-INGEST-01 — added ``bazaar_manifest`` (discovery from
Coinbase Agentic Wallet / Bazaar's public ``/v1/services`` catalog) and
``bazaar_live`` (paid x402 calls against catalog providers). The pre-
existing ``bazaar`` literal is retained as a coarse-grained source label
for legacy chunks; new ingestions MUST use the split literals."""

KNOWLEDGE_SOURCES: Final[tuple[KnowledgeSource, ...]] = (
    "web",
    "tavily",
    "twit_sh",
    "bazaar",
    "bazaar_manifest",
    "bazaar_live",
    "paysh_manifest",
    "paysh_live",
    "user_query",
    "enriched_output",
    "market_data",
)
"""Runtime tuple — used by Mongo validators."""


# ---------------------------------------------------------------------------
# Legacy source coercion — S22-N1 one-sprint deprecation alias.
# ---------------------------------------------------------------------------

# S23 cleanup — remove this map and the helper after one full sprint.
_LEGACY_SOURCE_ALIASES: Final[dict[str, KnowledgeSource]] = {
    "pay_sh": "paysh_live",
}


def _coerce_legacy_source(value: str) -> str:
    """Coerce legacy ``KnowledgeSource`` values on read.

    S22-N1 split ``pay_sh`` into ``paysh_manifest`` / ``paysh_live``.
    Chunks persisted before the split carry the old literal; this helper
    is wired into the Mongo deserialization path so reads transparently
    return the new literal. Unknown values pass through unchanged — the
    validator on the write path is the enforcement point.

    # S23 cleanup — remove after one sprint of soak time.
    """
    return _LEGACY_SOURCE_ALIASES.get(value, value)


# ---------------------------------------------------------------------------
# Subcategory map — free-form strings, validated against this map at
# write time. Seed each Category with at least one canonical subcategory.
# ---------------------------------------------------------------------------

SUBCATEGORIES: Final[dict[Category, tuple[str, ...]]] = {
    "market_intelligence": ("competitor", "trend", "tam", "segment"),
    "business_financial": ("regulatory", "unit_economics", "pricing", "gtm"),
    "investment_signals": ("funding_round", "valuation", "investor_thesis", "exit"),
    "product": ("feature", "roadmap", "user_feedback", "positioning"),
    "technical_engineering": ("architecture", "infra", "performance", "security"),
    "ai_ml": ("model", "rag", "eval", "prompt"),
    "design_ux": ("flow", "component", "research", "accessibility"),
    # S20-A3 — legacy bucket has no canonical subcategories; the revoke
    # script writes ``subcategory=""`` and writes are gated elsewhere.
    "legacy_uncategorized": (),
}
"""Canonical subcategory seeds per Category. Mongo writes validate
``subcategory`` against this map. Subcategories are open-set and grow
over time — adding one is a single-line edit here."""


# ---------------------------------------------------------------------------
# ChunkMetadata — per-chunk bookkeeping carried alongside the embedding.
# ---------------------------------------------------------------------------


class ChunkMetadata(TypedDict):
    """Per-chunk metadata stored in Mongo alongside text + embedding.

    ``pioneer`` is True when this chunk was the first to seed an empty
    ``(vertical, category)`` cell — see future ticket A7 for the
    instrumentation that flips it.
    """

    confidence: float
    usage_count: int
    timestamp: datetime
    pioneer: bool


def is_valid_subcategory(category: Category, subcategory: str) -> bool:
    """Return True iff ``subcategory`` is registered under ``category``.

    Used by the Mongo write path. Unknown categories return False rather
    than raising — callers should already have validated ``category``
    against ``CATEGORIES``.
    """
    allowed = SUBCATEGORIES.get(category)
    if allowed is None:
        return False
    return subcategory in allowed


def default_chunk_metadata() -> ChunkMetadata:
    """Return a fresh metadata dict for a brand-new chunk.

    ``timestamp`` is timezone-aware UTC. ``pioneer`` defaults False; the
    A7 instrumentation flips it after checking for an empty cell.
    """
    return ChunkMetadata(
        confidence=0.0,
        usage_count=0,
        timestamp=datetime.now(UTC),
        pioneer=False,
    )


__all__ = [
    "CATEGORIES",
    "KNOWLEDGE_SOURCES",
    "LEGACY_CATEGORY",
    "SUBCATEGORIES",
    "VERTICALS",
    "Category",
    "ChunkMetadata",
    "KnowledgeSource",
    "Vertical",
    "_coerce_legacy_source",
    "default_chunk_metadata",
    "is_valid_subcategory",
]
