"""S20-COHERENCE-VERDICT-LABEL-01 — Pattern A consistency test for Verdict.

Mirrors ``test_payment_mode_consistency.py`` and
``test_v1_source_ids_consistency.py``: every site that enumerates the
Verdict vocabulary must agree with the canonical ``Verdict`` enum in
``gecko_core.models``. Adding a new verdict label requires touching:

  1. ``Verdict`` enum (canonical Python definition)
  2. ``derive_verdict`` mapping in ``gecko_core.models``
  3. The legacy bucket map in ``workflows._detect_research_verdict``
  4. The Pro judge prompt's "Final verdict:" emit line (v5.4 today)
  5. PRD acceptance-criteria docstring (when one exists)

This test fails loudly when a new enum value lacks a downstream entry —
the failure mode S17 hit when the rename landed in Python but not all
consumers (the eval rubric still graded against ship/kill/pivot). The
S20 KILL re-introduction is the first canonical addition to live under
this test.

Note on scope: The new ``models.Verdict`` (PIVOT / REFINE / GO / KILL,
S11+S17+S20) does NOT yet have a SQL CHECK mirror — it's persisted as
free text inside ``sessions.result_json``. The legacy
``sessions.store.Verdict`` (ship/kill/pivot, used by the
``gecko_precedent`` table) is independently SQL-mirrored and is
covered by ``packages/gecko-core/tests/test_literal_consistency.py``.
When S15+ adds a SQL column for the new verdict shape, extend this
test with a CHECK-constraint scan.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest
from gecko_core.models import Verdict
from gecko_core.workflows import _detect_research_verdict

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CORE_PKG = _REPO_ROOT / "packages" / "gecko-core" / "src" / "gecko_core"
_PROMPTS_DIR = _CORE_PKG / "orchestration" / "pro"


# ---------------------------------------------------------------------------
# Canonical-enum sanity
# ---------------------------------------------------------------------------


def test_canonical_verdict_values() -> None:
    """Lock the canonical set. Adding a label changes this assertion and
    forces the developer to walk the rest of this file before merging."""
    values = {v.value for v in Verdict}
    assert values == {"PIVOT", "REFINE", "GO", "KILL"}


def test_verdict_string_values_are_canonical_uppercase() -> None:
    """Every verdict surfaces as a single uppercase token. The CLI splash
    and rubric scripts assume this — break it and downstream regexes break."""
    for v in Verdict:
        assert v.value.isupper(), f"{v!r} value must be uppercase"
        assert v.value == v.name, f"{v!r} value must match name"


# ---------------------------------------------------------------------------
# Synthesizer mapping coverage
# ---------------------------------------------------------------------------


def test_legacy_bucket_map_covers_every_verdict() -> None:
    """``_detect_research_verdict`` maps each canonical Verdict to a legacy
    journal bucket (ship / kill / pivot). The map must cover every label
    or the journal entry silently falls through to "unknown" for the
    missing one — the exact bug pattern S17 introduced."""
    src = inspect.getsource(_detect_research_verdict)
    # Surface check: every Verdict.X is referenced in the legacy map.
    for v in Verdict:
        assert f"Verdict.{v.name}" in src, (
            f"workflows._detect_research_verdict is missing an entry for "
            f"Verdict.{v.name}; add it to the _LEGACY map or this verdict "
            f"falls through to 'unknown' in the journal."
        )


def test_derive_verdict_can_emit_every_canonical_value() -> None:
    """For every canonical Verdict label, there exists at least one input
    set that produces it. Catches dead-code labels and one-way enums."""
    from gecko_core.models import derive_verdict

    # GO — Partial:pricing + advisor consensus ≥ 0.8
    assert derive_verdict("Partial:pricing", advisor_consensus=1.0) is Verdict.GO
    # REFINE — Partial:UX (any consensus)
    assert derive_verdict("Partial:UX") is Verdict.REFINE
    # PIVOT — Full
    assert derive_verdict("Full") is Verdict.PIVOT
    # KILL — ≥2 incoherence flags (S20)
    assert derive_verdict("Partial:UX", incoherence_flag_count=2) is Verdict.KILL


# ---------------------------------------------------------------------------
# Prompt-template scan: the active Pro judge prompt must enumerate every
# verdict label it is allowed to emit. If a label is added to the enum
# but the judge never knows it exists, the synthesizer can produce a
# value that the judge can't justify in transcript prose.
# ---------------------------------------------------------------------------


_VERDICTS_THE_JUDGE_OWNS: tuple[str, ...] = ("PIVOT", "REFINE", "GO")
"""Labels that the JUDGE PROMPT emits via 'Final verdict: X'.

Carved out as a separate tuple because KILL (S20) is structurally
different — it is NEVER emitted by the judge as a standalone token; it
is computed by the synthesizer from the count of ``INCOHERENT_PREMISE:
yes`` sentinels across voices. The judge's prose may still SAY 'KILL'
for legacy reasons (v5.4 STEP 4 still uses the word for hard-kill
triggers) but the structured ``Final verdict:`` line stays in
{PIVOT, REFINE, GO}. If a future prompt version teaches the judge to
emit ``Final verdict: KILL`` directly, move KILL into the tuple above
and adjust ``_run_pro_debate`` to honour that path."""


def test_judge_prompt_enumerates_judge_owned_labels() -> None:
    """Active Pro judge prompt (v5.4) must mention every label in
    ``_VERDICTS_THE_JUDGE_OWNS`` in the 'Final verdict:' enumeration."""
    active = _PROMPTS_DIR / "_default_prompts_v5_4.json"
    text = active.read_text(encoding="utf-8")
    for label in _VERDICTS_THE_JUDGE_OWNS:
        # Word-boundary match to avoid catching the label inside another word.
        assert re.search(rf"\b{label}\b", text), (
            f"active judge prompt {active.name!r} does not mention {label!r} — "
            f"either remove the label from _VERDICTS_THE_JUDGE_OWNS or update "
            f"the judge prompt's Final-verdict enumeration."
        )


def test_kill_label_is_synthesizer_only() -> None:
    """Defensive: KILL must NOT be promoted into the judge-owned tuple
    without a paired update to ``_run_pro_debate``. If a developer adds
    'KILL' to ``_VERDICTS_THE_JUDGE_OWNS`` without also teaching the
    workflow to honour a judge-emitted KILL, the structured signal
    (incoherence flag count) and the prose signal (judge's word) can
    disagree silently."""
    assert "KILL" not in _VERDICTS_THE_JUDGE_OWNS, (
        "KILL was promoted to judge-owned. Update _run_pro_debate in "
        "workflows.py to also parse 'Final verdict: KILL' before relaxing "
        "this guard."
    )


# ---------------------------------------------------------------------------
# Deliberate-divergence test — proves the comparator above is not a no-op.
# ---------------------------------------------------------------------------


def test_divergence_is_detected_for_legacy_map(monkeypatch: pytest.MonkeyPatch) -> None:
    """If a future contributor drops a Verdict label from the legacy map
    in ``_detect_research_verdict``, the test above must fail. Simulate by
    inspecting a fake source and asserting the same comparator catches
    the missing entry."""
    fake_src = (
        "def fake():\n"
        "    return {Verdict.GO: 'ship', Verdict.PIVOT: 'kill'}\n"  # missing REFINE + KILL
    )
    missing = [v for v in Verdict if f"Verdict.{v.name}" not in fake_src]
    assert set(m.name for m in missing) == {"REFINE", "KILL"}, (
        "the comparator is no-op — it should have flagged REFINE and KILL "
        "as missing from the fake source."
    )
