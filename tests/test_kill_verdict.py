"""S20-COHERENCE-VERDICT-LABEL-01 — KILL on premise incoherence.

Stress-matrix #4 from ``docs/sprint-reviews/2026-05-02-s18-s19-review.md``
§5: "AI-powered blockchain for emotional intelligence in dogs" must
produce ``Verdict.KILL`` once ≥2 voices in the 5-agent pro debate emit
the structured ``INCOHERENT_PREMISE: yes`` sentinel. Today's pipeline
emits a polite REFINE because no critic voice is willing to call premise
incoherence — that's the gap this test pins.

Mocked-only: the test exercises ``derive_verdict`` and the transcript
flag-counter (``count_incoherent_premise_flags``). No live LLM calls.
A live-LLM regression for the prompt edit lives in the eval harness,
not here.
"""

from __future__ import annotations

from gecko_core.models import (
    COHERENCE_KILL_MIN_FLAGS,
    Verdict,
    derive_verdict,
)
from gecko_core.orchestration.pro.coherence import (
    count_incoherent_premise_flags,
    turn_flags_incoherent_premise,
)
from gecko_core.orchestration.pro.transcript import AgentTurn

# Stress-matrix #4 — the canonical incoherent-premise idea. Kept here as
# a string constant so future fixture authors can grep for it across
# test_kill_verdict + the eval harness rubric.
_INCOHERENT_IDEA = "AI-powered blockchain for emotional intelligence in dogs"


def _turn(agent: str, content: str, seq: int = 0) -> AgentTurn:
    """Minimal AgentTurn factory — defaults match the Pro transcript shape."""
    return AgentTurn(
        seq=seq,
        agent=agent,  # type: ignore[arg-type]
        content=content,
        ts=0.0,
        tokens_in=0,
        tokens_out=0,
    )


# ---------------------------------------------------------------------------
# Sentinel parser unit tests
# ---------------------------------------------------------------------------


def test_sentinel_yes_matches() -> None:
    assert turn_flags_incoherent_premise("INCOHERENT_PREMISE: yes")
    assert turn_flags_incoherent_premise("incoherent_premise: yes")
    assert turn_flags_incoherent_premise("foo\nINCOHERENT_PREMISE: yes\nbar")
    # Whitespace flexibility around the colon.
    assert turn_flags_incoherent_premise("INCOHERENT_PREMISE:yes")
    assert turn_flags_incoherent_premise("INCOHERENT_PREMISE :  yes")


def test_sentinel_no_does_not_match() -> None:
    assert not turn_flags_incoherent_premise("INCOHERENT_PREMISE: no")
    assert not turn_flags_incoherent_premise("incoherent_premise: no")
    assert not turn_flags_incoherent_premise("")
    assert not turn_flags_incoherent_premise("the idea is coherent, INCOHERENT_PREMISE: no")


def test_distinct_voice_count() -> None:
    turns = [
        _turn("critic", "INCOHERENT_PREMISE: yes", seq=1),
        _turn("critic", "INCOHERENT_PREMISE: yes", seq=2),  # same voice → 1
        _turn("judge", "INCOHERENT_PREMISE: yes", seq=3),
        _turn("analyst", "INCOHERENT_PREMISE: no", seq=4),
    ]
    assert count_incoherent_premise_flags(turns) == 2


# ---------------------------------------------------------------------------
# Synthesizer behaviour — the actual S20 contract
# ---------------------------------------------------------------------------


def test_kill_fires_at_minimum_flag_count() -> None:
    """≥COHERENCE_KILL_MIN_FLAGS distinct voices flagging incoherence →
    Verdict.KILL, regardless of gap classification."""
    assert COHERENCE_KILL_MIN_FLAGS == 2
    # gap that would otherwise be GO-eligible
    assert (
        derive_verdict(
            "Partial:pricing",
            advisor_consensus=1.0,
            incoherence_flag_count=2,
        )
        is Verdict.KILL
    )
    # gap that would otherwise be PIVOT
    assert derive_verdict("Full", incoherence_flag_count=2) is Verdict.KILL
    # 3 flags (more than threshold) — still KILL
    assert derive_verdict("Partial:UX", incoherence_flag_count=3) is Verdict.KILL


def test_single_flag_does_not_fire_kill() -> None:
    """Single dissent could be noise — must NOT trip KILL."""
    assert derive_verdict("Partial:UX", incoherence_flag_count=1) is Verdict.REFINE
    assert derive_verdict("Full", incoherence_flag_count=1) is Verdict.PIVOT


def test_zero_flag_default_unchanged() -> None:
    """Default ``incoherence_flag_count=0`` keeps existing GO/REFINE/PIVOT
    mapping unchanged — basic tier and legacy callers."""
    assert derive_verdict("Partial:UX") is Verdict.REFINE
    assert derive_verdict("Full") is Verdict.PIVOT
    assert derive_verdict("Partial:pricing", advisor_consensus=1.0) is Verdict.GO


def test_kill_overrides_low_grounding_floor() -> None:
    """Premise incoherence overrides the citation evidence-strength floor.
    A premise that doesn't compose is still incoherent even with sparse
    citations — flooring to REFINE would mislead the founder."""
    # Empty citations → would normally floor to REFINE
    assert derive_verdict("Partial:UX", citations=[], incoherence_flag_count=2) is Verdict.KILL


# ---------------------------------------------------------------------------
# Stress-matrix #4 end-to-end: incoherent_idea + mocked debate → KILL
# ---------------------------------------------------------------------------


def test_stress_matrix_idea_routes_to_kill() -> None:
    """Stress-matrix #4: the dog-emotional-AI-blockchain idea, with two
    voices flagging premise incoherence, must land on Verdict.KILL.

    This is the regression that pins S20-COHERENCE-VERDICT-LABEL-01: the
    same input set produced REFINE before this ticket landed because no
    critic voice was willing to call incoherence."""
    # Mocked transcript — what we expect the v5.4+ critic + judge to emit
    # when they apply the PREMISE COHERENCE CHECK to the stress-matrix idea.
    turns = [
        _turn(
            "analyst",
            "TAM is unmeasurable; no comparable shipped in last 24 months.",
            seq=1,
        ),
        _turn(
            "critic",
            "There is no measurable substrate for 'emotional intelligence in dogs', "
            "and blockchain adds nothing to the loop.\n"
            "INCOHERENT_PREMISE: yes\n"
            "Change my mind by: a peer-reviewed canine-emotion benchmark that "
            "blockchain settlement is on the critical path for.",
            seq=2,
        ),
        _turn("architect", "Default Next.js + Supabase. Skeptical of any V1.", seq=3),
        _turn("scoper", "V1_FEASIBLE_IN_4_DAYS: no", seq=4),
        _turn(
            "judge",
            "Verdict: KILL — premise components do not compose.\n"
            "INCOHERENT_PREMISE: yes\n"
            "gap_classification: False\n"
            "Final verdict: KILL",
            seq=5,
        ),
    ]
    flags = count_incoherent_premise_flags(turns)
    assert flags == 2, "expected critic + judge to flag incoherence"

    # The synthesizer takes the gap classification from the validation
    # report (here we mirror what the judge emitted: 'False'). Even with
    # gap='False' (which would normally → PIVOT), KILL must win.
    verdict = derive_verdict(
        "False",
        advisor_consensus=None,
        citations=None,
        incoherence_flag_count=flags,
    )
    assert verdict is Verdict.KILL


def test_negative_case_concerns_without_incoherence_does_not_fire_kill() -> None:
    """Two voices flagging concerns BUT NOT premise incoherence → KILL must
    NOT fire. PIVOT/REFINE branch wins."""
    turns = [
        _turn(
            "critic",
            "Crowded market, no named first user.\nINCOHERENT_PREMISE: no\n"
            "Change my mind by: a named pilot user with a payment moment.",
            seq=1,
        ),
        _turn(
            "judge",
            "Verdict: KILL — saturated commodity vertical (STEP 4c).\n"
            "INCOHERENT_PREMISE: no\n"
            "gap_classification: Full\n"
            "Final verdict: PIVOT",
            seq=2,
        ),
    ]
    flags = count_incoherent_premise_flags(turns)
    assert flags == 0, "neither voice flagged premise incoherence"

    verdict = derive_verdict(
        "Full",  # judge said gap=Full (saturated commodity)
        advisor_consensus=None,
        citations=None,
        incoherence_flag_count=flags,
    )
    # gap=Full → PIVOT, not KILL — even though the judge's prose used
    # the legacy "KILL" word. The structured signal (sentinel count) is
    # the source of truth, prose is diagnostic only.
    assert verdict is Verdict.PIVOT


def test_idea_text_is_pinned() -> None:
    """Defensive: keep the canonical stress-matrix #4 string addressable so
    the eval rubric author can reuse it without drift."""
    assert "blockchain" in _INCOHERENT_IDEA
    assert "emotional intelligence" in _INCOHERENT_IDEA
