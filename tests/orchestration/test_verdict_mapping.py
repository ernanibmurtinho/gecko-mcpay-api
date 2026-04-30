"""S11-VERDICT-01 — verdict unification renderer.

Table-driven mapping from (gap_classification, advisor_consensus) → Verdict.
Covers all 7 gap_classification values x 3 consensus tiers (0.0, 0.5, 1.0).

Mapping rule (see ``gecko_core.models.derive_verdict``):
  - ``Full`` or ``False``                    → ``KILL``  (consensus ignored)
  - ``Partial:pricing`` / ``Partial:integration``
      AND consensus ≥ 0.8                   → ``BUILD``
      else                                  → ``REFINE``
  - ``Partial:segment | UX | geo``           → ``REFINE``  (consensus ignored)
"""

from __future__ import annotations

import pytest
from gecko_core.models import (
    GapClassification,
    Verdict,
    derive_verdict,
)

# fmt: off
_CASES: list[tuple[GapClassification, float, Verdict]] = [
    # gap → KILL regardless of consensus
    ("Full",                0.0, Verdict.KILL),
    ("Full",                0.5, Verdict.KILL),
    ("Full",                1.0, Verdict.KILL),
    ("False",               0.0, Verdict.KILL),
    ("False",               0.5, Verdict.KILL),
    ("False",               1.0, Verdict.KILL),
    # Partial:pricing — consensus tips REFINE → BUILD at 0.8
    ("Partial:pricing",     0.0, Verdict.REFINE),
    ("Partial:pricing",     0.5, Verdict.REFINE),
    ("Partial:pricing",     1.0, Verdict.BUILD),
    # Partial:integration — same gate
    ("Partial:integration", 0.0, Verdict.REFINE),
    ("Partial:integration", 0.5, Verdict.REFINE),
    ("Partial:integration", 1.0, Verdict.BUILD),
    # Partial:segment / UX / geo — always REFINE (consensus ignored)
    ("Partial:segment",     0.0, Verdict.REFINE),
    ("Partial:segment",     0.5, Verdict.REFINE),
    ("Partial:segment",     1.0, Verdict.REFINE),
    ("Partial:UX",          0.0, Verdict.REFINE),
    ("Partial:UX",          0.5, Verdict.REFINE),
    ("Partial:UX",          1.0, Verdict.REFINE),
    ("Partial:geo",         0.0, Verdict.REFINE),
    ("Partial:geo",         0.5, Verdict.REFINE),
    ("Partial:geo",         1.0, Verdict.REFINE),
]
# fmt: on


@pytest.mark.parametrize("gap,consensus,expected", _CASES)
def test_derive_verdict_table(gap: GapClassification, consensus: float, expected: Verdict) -> None:
    assert derive_verdict(gap, consensus) is expected


def test_derive_verdict_consensus_threshold_exact() -> None:
    """0.8 is the inclusive boundary — exactly 0.8 must promote to BUILD."""
    assert derive_verdict("Partial:pricing", 0.8) is Verdict.BUILD
    assert derive_verdict("Partial:pricing", 0.79) is Verdict.REFINE


def test_derive_verdict_none_consensus_means_lean_refine() -> None:
    """When advisor signal is absent (basic tier), pricing/integration
    partials default to REFINE rather than BUILD on silence."""
    assert derive_verdict("Partial:pricing") is Verdict.REFINE
    assert derive_verdict("Partial:integration") is Verdict.REFINE
    # Full / False stay KILL even without consensus.
    assert derive_verdict("Full") is Verdict.KILL
    assert derive_verdict("False") is Verdict.KILL


def test_verdict_enum_string_values() -> None:
    """Verdict serializes as the exact tokens the landing copy promises."""
    assert Verdict.KILL.value == "KILL"
    assert Verdict.REFINE.value == "REFINE"
    assert Verdict.BUILD.value == "BUILD"
