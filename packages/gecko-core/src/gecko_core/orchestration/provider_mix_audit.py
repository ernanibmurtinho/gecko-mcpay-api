"""Provider-mix audit on the verdict's citation set (S20-PROVIDER-MIX-FLOOR-01).

Stress-matrix #1 (docs/sprint-reviews/2026-05-02-s18-s19-review.md §5)
predicted that hot-topic ideas like "x402-onramp-for-DAOs" skew >70%
twit.sh + bazaar, missing actual prior art. The hybrid retrieval path
already enforces a per-kind quota at fetch time (see
``match_chunks_hybrid_mongo`` and ``_rerank_by_provider``); this module
adds a *post-hoc* audit on the citation set the LLM actually cited.

The audit is informational. It does NOT change the verdict (verdict
identity / verdict_hash inputs stay unchanged). It surfaces a soft flag
on ``ResearchResult.provider_mix_flag`` so downstream consumers (CLI
footer, S20-VERDICT-URL-IMPL phase 1) can render a warning when
provenance is too narrow.

Rules (see plan §2a #5):
  * ``balanced`` — ≥3 distinct provider_kinds AND no kind > 50%
  * ``single_provider_dominates`` — one kind > 50% (severe)
  * ``thin_diversity`` — < 3 distinct kinds (severe-ish)

If both severe conditions apply, ``single_provider_dominates`` wins
because the provenance signal is worse: a corpus that's 100% one kind
is more misleading than a corpus split 60/40 across two kinds.
"""

from __future__ import annotations

from collections import Counter

from gecko_core.models import Citation, ProviderMixFlag

_DOMINANCE_THRESHOLD = 0.5  # > 50% of citations from one kind = dominated
_MIN_DISTINCT_KINDS = 3


def _kind_for(citation: Citation) -> str:
    """Resolve the provider_kind for a citation.

    Prefers the typed ``provenance.provider_kind`` field (set by the
    ingestion path); falls back to URI-scheme inference for legacy
    citations that predate the provenance stamp or for hand-built test
    fixtures that didn't override the default.
    """
    pk = citation.provenance.provider_kind if citation.provenance else None
    # Treat the legacy default ("free") as "unknown intent" only when the
    # URI scheme tells us nothing more specific. URI schemes for synthetic
    # provider URIs (bazaar://, twitsh://) are authoritative.
    url = (citation.source_url or "").strip().lower()
    if url.startswith("bazaar://"):
        return "bazaar"
    if url.startswith("twitsh://"):
        return "twitsh"
    if pk:
        return pk
    if url.startswith(("https://", "http://")):
        return "web"
    return "unknown"


def audit_provider_mix(citations: list[Citation]) -> ProviderMixFlag:
    """Classify the provider-mix shape of a citation set.

    Empty / single-citation sets are ``thin_diversity`` by construction
    (< 3 distinct kinds).
    """
    if not citations:
        return "thin_diversity"

    counts: Counter[str] = Counter(_kind_for(c) for c in citations)
    total = sum(counts.values())
    distinct = len(counts)

    # Single-provider dominance is the worse signal — it means the
    # verdict leans on one source of evidence even if other kinds are
    # technically present in the citation set.
    top_share = max(counts.values()) / total
    if top_share > _DOMINANCE_THRESHOLD:
        return "single_provider_dominates"

    if distinct < _MIN_DISTINCT_KINDS:
        return "thin_diversity"

    return "balanced"


__all__ = ["audit_provider_mix"]
