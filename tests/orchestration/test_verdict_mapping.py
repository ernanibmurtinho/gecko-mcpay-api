"""S11-VERDICT-01 / S17-TONE-01 — verdict unification renderer.

Table-driven mapping from (gap_classification, advisor_consensus) → Verdict.
Covers all 7 gap_classification values x 3 consensus tiers (0.0, 0.5, 1.0).

Mapping rule (see ``gecko_core.models.derive_verdict``):
  - ``Full`` or ``False``                    → ``PIVOT``  (consensus ignored)
  - ``Partial:pricing`` / ``Partial:integration``
      AND consensus ≥ 0.8                   → ``GO``
      else                                  → ``REFINE``
  - ``Partial:segment | UX | geo``           → ``REFINE``  (consensus ignored)

S17-TONE-01: ``KILL`` was renamed to ``PIVOT`` and ``BUILD`` to ``GO`` for
softer founder-facing copy. Semantics unchanged. The legacy string tokens
``"KILL"`` and ``"BUILD"`` round-trip through ``Verdict._missing_`` for
backwards-compat with older ``result_json`` rows + on-disk transcripts —
``test_verdict_enum_legacy_strings_round_trip`` exercises that shim.
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
    # gap → PIVOT regardless of consensus
    ("Full",                0.0, Verdict.PIVOT),
    ("Full",                0.5, Verdict.PIVOT),
    ("Full",                1.0, Verdict.PIVOT),
    ("False",               0.0, Verdict.PIVOT),
    ("False",               0.5, Verdict.PIVOT),
    ("False",               1.0, Verdict.PIVOT),
    # Partial:pricing — consensus tips REFINE → GO at 0.8
    ("Partial:pricing",     0.0, Verdict.REFINE),
    ("Partial:pricing",     0.5, Verdict.REFINE),
    ("Partial:pricing",     1.0, Verdict.GO),
    # Partial:integration — same gate
    ("Partial:integration", 0.0, Verdict.REFINE),
    ("Partial:integration", 0.5, Verdict.REFINE),
    ("Partial:integration", 1.0, Verdict.GO),
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
    """0.8 is the inclusive boundary — exactly 0.8 must promote to GO."""
    assert derive_verdict("Partial:pricing", 0.8) is Verdict.GO
    assert derive_verdict("Partial:pricing", 0.79) is Verdict.REFINE


def test_derive_verdict_none_consensus_means_lean_refine() -> None:
    """When advisor signal is absent (basic tier), pricing/integration
    partials default to REFINE rather than GO on silence."""
    assert derive_verdict("Partial:pricing") is Verdict.REFINE
    assert derive_verdict("Partial:integration") is Verdict.REFINE
    # Full / False stay PIVOT even without consensus.
    assert derive_verdict("Full") is Verdict.PIVOT
    assert derive_verdict("False") is Verdict.PIVOT


def test_verdict_enum_string_values() -> None:
    """Verdict serializes as the exact tokens the landing copy promises."""
    assert Verdict.PIVOT.value == "PIVOT"
    assert Verdict.REFINE.value == "REFINE"
    assert Verdict.GO.value == "GO"


def test_verdict_enum_legacy_strings_round_trip() -> None:
    """S17-TONE-01 — backwards-read shim for legacy ``result_json`` rows
    and on-disk transcripts captured before the rename. ``Verdict("KILL")``
    must yield ``Verdict.PIVOT``; ``Verdict("BUILD")`` must yield
    ``Verdict.GO``. New tokens round-trip too."""
    assert Verdict("KILL") is Verdict.PIVOT
    assert Verdict("BUILD") is Verdict.GO
    assert Verdict("PIVOT") is Verdict.PIVOT
    assert Verdict("GO") is Verdict.GO
    assert Verdict("REFINE") is Verdict.REFINE
    # Case-insensitive on the off chance a consumer lower-cased the token.
    assert Verdict("kill") is Verdict.PIVOT
    assert Verdict("build") is Verdict.GO
