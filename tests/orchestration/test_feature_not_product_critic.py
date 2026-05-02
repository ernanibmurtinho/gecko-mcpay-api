"""S20-FEATURE-NOT-PRODUCT-CRITIC-01 — conditional critic prompt fragment.

The 5-voice pro debate appends a "is this a feature, or a defensible product?"
fragment to the critic's system message when the basic-tier classified the
competitive landscape as crowded or mature.

Trigger mapping (see prompts.py FEATURE_NOT_PRODUCT_GAP_TRIGGERS):

    ticket label                       → canonical GapClassification
    -------------------------------------------------------------
    "crowded-but-differentiable"       → Partial:UX, Partial:pricing,
                                         Partial:integration
    "mature"                           → Full

These tests are mocked-only — no LLM calls. They assert prompt assembly is
deterministic and that the fragment is gated on the gap label.
"""

from __future__ import annotations

import pytest
from gecko_core.orchestration.pro.agents import _agent_specs
from gecko_core.orchestration.pro.prompts import (
    FEATURE_NOT_PRODUCT_FRAGMENT,
    FEATURE_NOT_PRODUCT_GAP_TRIGGERS,
    feature_not_product_fragment_for,
)

# --- pure-function trigger map -------------------------------------------------


@pytest.mark.parametrize(
    "gap",
    sorted(FEATURE_NOT_PRODUCT_GAP_TRIGGERS),
)
def test_fragment_fires_on_trigger_labels(gap: str) -> None:
    """Each canonical trigger label yields the fragment string."""
    out = feature_not_product_fragment_for(gap)
    assert out == FEATURE_NOT_PRODUCT_FRAGMENT


@pytest.mark.parametrize(
    "gap",
    [
        "Partial:segment",  # narrow ICP, NOT crowded — fragment must NOT fire
        "Partial:geo",  # geographic gap — different risk surface
        "False",  # no real gap; fragment doesn't apply
        None,  # missing classification
        "",
        "garbage-label-not-in-taxonomy",
    ],
)
def test_fragment_skipped_on_non_trigger_labels(gap: str | None) -> None:
    assert feature_not_product_fragment_for(gap) is None


# --- agent_specs assembly ------------------------------------------------------


def test_agent_specs_appends_fragment_to_critic_only() -> None:
    """When trigger fires, ONLY the critic's system message gets the suffix."""
    specs = dict(_agent_specs(gap_classification="Partial:UX"))
    assert FEATURE_NOT_PRODUCT_FRAGMENT in specs["critic"]
    # Other voices stay clean — the fragment is critic-scoped.
    for name in ("analyst", "architect", "scoper", "judge"):
        assert FEATURE_NOT_PRODUCT_FRAGMENT not in specs[name], (
            f"fragment leaked into {name}'s prompt"
        )


def test_agent_specs_skips_fragment_on_narrow_gap() -> None:
    """Partial:segment is the canonical 'narrow ICP' label — no feature risk."""
    specs = dict(_agent_specs(gap_classification="Partial:segment"))
    for name, sys_msg in specs.items():
        assert FEATURE_NOT_PRODUCT_FRAGMENT not in sys_msg, (
            f"fragment unexpectedly present in {name}"
        )


def test_agent_specs_skips_fragment_when_gap_none() -> None:
    """No gap classification → no fragment (the legacy callsite path)."""
    specs = dict(_agent_specs(gap_classification=None))
    for sys_msg in specs.values():
        assert FEATURE_NOT_PRODUCT_FRAGMENT not in sys_msg


def test_agent_specs_default_call_omits_fragment() -> None:
    """Backwards-compat: callers that don't pass the kwarg get the legacy prompts."""
    specs = dict(_agent_specs())
    for sys_msg in specs.values():
        assert FEATURE_NOT_PRODUCT_FRAGMENT not in sys_msg


def test_agent_specs_deterministic_under_repeated_calls() -> None:
    """Same inputs → same output. Replay determinism for transcript debugging."""
    a = _agent_specs(gap_classification="Full")
    b = _agent_specs(gap_classification="Full")
    assert a == b
    # And again with the no-trigger path.
    c = _agent_specs(gap_classification="Partial:segment")
    d = _agent_specs(gap_classification="Partial:segment")
    assert c == d


def test_agent_specs_full_label_triggers_fragment() -> None:
    """Full = mature space (competitor fully covers); the highest-risk case."""
    specs = dict(_agent_specs(gap_classification="Full"))
    assert FEATURE_NOT_PRODUCT_FRAGMENT in specs["critic"]


@pytest.mark.parametrize(
    "gap",
    ["Partial:UX", "Partial:pricing", "Partial:integration"],
)
def test_agent_specs_crowded_but_differentiable_labels(gap: str) -> None:
    """All three "crowded-but-differentiable" facets trigger the fragment."""
    specs = dict(_agent_specs(gap_classification=gap))
    assert FEATURE_NOT_PRODUCT_FRAGMENT in specs["critic"]


def test_fragment_text_contains_required_markers() -> None:
    """Soft check: the fragment names the moat-vs-feature framing explicitly.

    Used as a guardrail so a future prompt rewrite that drops the framing
    fails this test loud and obvious instead of silently neutering the critic.
    """
    text = FEATURE_NOT_PRODUCT_FRAGMENT.lower()
    assert "feature" in text
    assert "incumbent" in text
    assert "moat" in text


# --- dogfood smoke (skipped by default) ----------------------------------------


@pytest.mark.skip(
    reason=(
        "dogfood — opt in with a live-LLM run. Stress-matrix #2 "
        "(self-serve API gateway for B2B onboarding) should produce a "
        "verdict whose reasoning addresses 'feature' or 'incumbent'."
    )
)
def test_stress_matrix_api_gateway_addresses_feature_risk() -> None:  # pragma: no cover
    """Manual dogfood test — for human review, not CI.

    Run with the API gateway idea through the pro tier in mock mode and
    inspect transcript['turns'][1] (the critic's content) for one of:
    'feature', 'incumbent', 'moat'. Soft check by design — variance at
    N=1 is real; we want signal, not a hard gate.
    """
    raise AssertionError("dogfood test is skipped by default")
