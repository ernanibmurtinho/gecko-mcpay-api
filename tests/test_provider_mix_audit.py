"""S20-PROVIDER-MIX-FLOOR-01 — provider-mix audit on the citation set.

Verifies the post-hoc audit classifies citation sets into:
  * ``balanced``         — ≥3 distinct kinds, no kind > 50%
  * ``single_provider_dominates`` — one kind > 50% (severe)
  * ``thin_diversity``   — < 3 distinct kinds

Includes the stress-matrix #1 regression: a citation set 80% twitsh +
20% bazaar must fire ``single_provider_dominates`` so the CLI footer
surfaces a soft warning. The audit is informational — verdict identity
and verdict_hash inputs are unchanged.
"""

from __future__ import annotations

from gecko_core.models import Citation, Provenance
from gecko_core.orchestration.provider_mix_audit import audit_provider_mix


def _cite(url: str, kind: str | None = None, similarity: float = 0.7) -> Citation:
    """Build a test citation. If ``kind`` is None we let the URI scheme
    infer it via the audit's fallback path (so we can exercise both
    code paths)."""
    prov = Provenance(provider_kind=kind) if kind else Provenance()
    return Citation(
        source_url=url,
        chunk_index=0,
        similarity=similarity,
        provenance=prov,
    )


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_empty_citations_is_thin_diversity() -> None:
    assert audit_provider_mix([]) == "thin_diversity"


def test_single_citation_is_dominated() -> None:
    # 1/1 = 100% > 50% → single_provider_dominates wins over thin_diversity.
    citations = [_cite("https://a.com", kind="web")]
    assert audit_provider_mix(citations) == "single_provider_dominates"


def test_all_same_kind_is_dominated() -> None:
    citations = [_cite(f"https://a{i}.com", kind="web") for i in range(5)]
    assert audit_provider_mix(citations) == "single_provider_dominates"


# ---------------------------------------------------------------------------
# Two-kind splits
# ---------------------------------------------------------------------------


def test_50_50_two_kind_split_is_thin_diversity() -> None:
    # Neither kind > 50%, but only 2 distinct kinds → thin_diversity.
    citations = [
        _cite("https://a.com", kind="web"),
        _cite("https://b.com", kind="web"),
        _cite("bazaar://x/1", kind="bazaar"),
        _cite("bazaar://x/2", kind="bazaar"),
    ]
    assert audit_provider_mix(citations) == "thin_diversity"


def test_51_49_split_tips_into_dominates() -> None:
    # 51 web / 49 bazaar — one kind exceeds 50%.
    citations = [_cite(f"https://a{i}.com", kind="web") for i in range(51)]
    citations += [_cite(f"bazaar://x/{i}", kind="bazaar") for i in range(49)]
    assert audit_provider_mix(citations) == "single_provider_dominates"


# ---------------------------------------------------------------------------
# Three-kind cases
# ---------------------------------------------------------------------------


def test_exactly_three_kinds_balanced() -> None:
    citations = [
        _cite("https://a.com", kind="web"),
        _cite("bazaar://x/1", kind="bazaar"),
        _cite("twitsh://abc", kind="twitsh"),
    ]
    assert audit_provider_mix(citations) == "balanced"


def test_three_kinds_one_dominates() -> None:
    # 4 web, 1 bazaar, 1 twitsh — web is 4/6 = 66% > 50%.
    citations = [_cite(f"https://a{i}.com", kind="web") for i in range(4)]
    citations.append(_cite("bazaar://x/1", kind="bazaar"))
    citations.append(_cite("twitsh://abc", kind="twitsh"))
    assert audit_provider_mix(citations) == "single_provider_dominates"


def test_three_kinds_at_thirds_balanced() -> None:
    citations = []
    for i in range(3):
        citations.append(_cite(f"https://a{i}.com", kind="web"))
    for i in range(3):
        citations.append(_cite(f"bazaar://x/{i}", kind="bazaar"))
    for i in range(3):
        citations.append(_cite(f"twitsh://t{i}", kind="twitsh"))
    assert audit_provider_mix(citations) == "balanced"


# ---------------------------------------------------------------------------
# URI scheme fallback (no provenance.provider_kind set)
# ---------------------------------------------------------------------------


def test_uri_scheme_fallback_resolves_kinds() -> None:
    # No explicit provider_kind — synthetic schemes win over the legacy
    # "free" default so the audit still classifies.
    citations = [
        _cite("https://a.com"),
        _cite("https://b.com"),
        _cite("bazaar://x/1"),
        _cite("twitsh://abc"),
    ]
    # web=2, bazaar=1, twitsh=1 — 2/4=50% (NOT > 50%) and 3 distinct kinds.
    assert audit_provider_mix(citations) == "balanced"


# ---------------------------------------------------------------------------
# Stress-matrix #1 regression
# ---------------------------------------------------------------------------


def test_stress_matrix_1_twitsh_dominated_set() -> None:
    """x402-onramp-for-DAOs prediction: 80% twitsh + 20% bazaar.

    The audit MUST flag this as single_provider_dominates so the CLI
    footer renders a soft warning. Without the floor, the verdict would
    cite the same provider repeatedly without surfacing the over-anchor.
    """
    citations = [_cite(f"twitsh://t{i}", kind="twitsh") for i in range(8)]
    citations += [_cite(f"bazaar://x/{i}", kind="bazaar") for i in range(2)]
    assert audit_provider_mix(citations) == "single_provider_dominates"


# ---------------------------------------------------------------------------
# Verdict-hash invariance
# ---------------------------------------------------------------------------


def test_provider_mix_flag_excluded_from_verdict_hash() -> None:
    """S18-D4 invariant: verdict_hash is structural. Adding/changing
    ``provider_mix_flag`` must NOT perturb the digest — it's an
    informational signal, not part of verdict identity."""
    from gecko_core.models import (
        PRD,
        BusinessPlan,
        ResearchResult,
        ValidationReport,
        Verdict,
    )
    from gecko_core.verdict_hash import verdict_hash

    citations = [
        _cite("https://a.com", kind="web"),
        _cite("https://b.com", kind="web"),
        _cite("https://c.com", kind="web"),
    ]
    bp = BusinessPlan(
        problem="p",
        icp="i",
        solution="s",
        market="m",
        business_model="b",
        channels="c",
        risks=[],
        citations=citations,
    )
    vr = ValidationReport(
        market_size_signal="m",
        competitor_analysis="c",
        demand_evidence="d",
        risk_flags=[],
        citations=citations,
        gap_classification="Partial:segment",
    )
    prd = PRD(
        v1_scope=["a"],
        v2_scope=[],
        v3_scope=[],
        acceptance_criteria=[],
        non_functional=[],
        success_metrics=[],
        citations=citations,
    )
    base = ResearchResult(
        session_id="00000000-0000-0000-0000-000000000000",
        tier="basic",
        business_plan=bp,
        validation_report=vr,
        prd=prd,
        sources=[],
        verdict=Verdict.REFINE,
        low_grounding=False,
    )
    h_no_flag = verdict_hash("idea", base)
    h_with_flag = verdict_hash(
        "idea",
        base.model_copy(update={"provider_mix_flag": "single_provider_dominates"}),
    )
    assert h_no_flag == h_with_flag
