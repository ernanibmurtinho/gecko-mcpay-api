"""S6-MINE-03 — capture priors-driven verdict inversions.

This test does NOT call any LLM. Instead, it verifies the *eval surface*:
given a deterministic mock that returns one verdict when the priors block
is present in its prompt context and another when it's absent, the test
confirms that the priors-on run inverts the verdict and that we can
isolate those cases for investigation.

The point is to keep a shape-stable place to plug the live eval into
once the holdout-live runner gets a `priors=on/off` knob (see
``docs/build-plan-sprint-6.md`` Track A). Until then, this test is
documentation: it pins the contract that "priors injection can flip
verdicts" and gives us a regression net for the rendering helper.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from gecko_core.memory.models import MemoryEntry, MemoryEntryType, MemoryScope
from gecko_core.memory.priors import render_priors_block


def _verdict_with_priors_aware_oracle(prompt: str) -> str:
    """Toy oracle: kills if it sees a prior KILL on the same project,
    otherwise ships. Stand-in for the LLM during this shape test."""
    if "VERDICT=KILL" in prompt:
        return "kill"
    return "ship"


def _entry(verdict: str, age_days: int = 1) -> MemoryEntry:
    return MemoryEntry(
        id=uuid4(),
        scope=MemoryScope(type="project", id="proj-1"),
        entry_type=MemoryEntryType.verdict_received,
        key=None,
        value={"verdict": verdict, "idea": "previous attempt"},
        embedding=None,
        tx_signature=None,
        created_at=datetime.now(UTC) - timedelta(days=age_days),
    )


def test_priors_on_inverts_verdict_when_prior_kill_exists() -> None:
    """The interesting case: a prior KILL on this project flips ship→kill
    when priors are injected. Captures the inversion the eval gate is
    supposed to surface to humans."""
    base_prompt = "# Idea\nThing the user wants to build"
    priors_block = render_priors_block([_entry("kill")])
    assert priors_block, "priors block must render for this fixture"

    baseline_verdict = _verdict_with_priors_aware_oracle(base_prompt)
    primed_verdict = _verdict_with_priors_aware_oracle(f"{priors_block}\n\n{base_prompt}")

    assert baseline_verdict == "ship"
    assert primed_verdict == "kill"
    assert baseline_verdict != primed_verdict


def test_priors_on_no_inversion_when_priors_agree() -> None:
    """Priors that align with the baseline must not introduce noise — the
    inversion-detector should not flag this case."""
    base_prompt = "# Idea\nGood idea"
    priors_block = render_priors_block([_entry("ship")])

    baseline_verdict = _verdict_with_priors_aware_oracle(base_prompt)
    primed_verdict = _verdict_with_priors_aware_oracle(f"{priors_block}\n\n{base_prompt}")

    assert baseline_verdict == primed_verdict == "ship"


def test_priors_off_returns_baseline_unchanged() -> None:
    """Empty priors block must be a no-op on the prompt."""
    base_prompt = "# Idea\nAnything"
    block = render_priors_block([])
    assert block == ""
    assert _verdict_with_priors_aware_oracle(base_prompt) == _verdict_with_priors_aware_oracle(
        f"{block}\n\n{base_prompt}"
    )


@pytest.mark.parametrize(
    "prior_verdict,expected_inversion",
    [
        ("kill", True),
        ("ship", False),
        ("pivot", False),  # toy oracle only flips on KILL
    ],
)
def test_inversion_detection_matrix(prior_verdict: str, expected_inversion: bool) -> None:
    base_prompt = "# Idea\nNew product"
    block = render_priors_block([_entry(prior_verdict)])
    baseline = _verdict_with_priors_aware_oracle(base_prompt)
    primed = _verdict_with_priors_aware_oracle(f"{block}\n\n{base_prompt}")
    inverted = baseline != primed
    assert inverted is expected_inversion
