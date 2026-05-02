"""S20-COHERENCE-VERDICT-LABEL-01 — premise-incoherence flag scanner.

Scans the 5-agent debate transcript for the structured sentinel line
each voice is instructed to emit when the idea's premise itself fails
to compose (NOT just "weak market" or "feasibility risk" — those route
through PIVOT/REFINE). When ≥``COHERENCE_KILL_MIN_FLAGS`` distinct
voices flag premise incoherence, the synthesizer flips the headline
verdict to ``Verdict.KILL`` regardless of gap classification.

The legacy KILL token (S11 / pre-S17) meant "weak idea, don't build
as-is" and was renamed to PIVOT. The S20 KILL is a structurally
different label: it fires only on premise-incoherence consensus. The
re-introduction is intentional and the trigger condition is sharper —
see ``Verdict`` docstring for the full rename history.

The scanner is intentionally regex-driven and case-insensitive:
prompts evolve (v5.4 today, v5.5+ tomorrow) but the sentinel format
``INCOHERENT_PREMISE: yes`` is the contract surface. Any voice whose
content emits the sentinel counts once, no matter how many times it
repeats. The judge's verdict prose is also scanned because the judge
sometimes synthesises critic flags into its own sentence.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from gecko_core.orchestration.pro.transcript import AgentTurn

# Sentinel: ``INCOHERENT_PREMISE: yes``. Allow whitespace flexibility,
# match case-insensitively, and bind to a word-boundary so prose like
# "the idea is not incoherent_premise: …" can't false-positive (the
# prompt instructs voices to emit the line as a standalone sentinel).
_INCOHERENT_PATTERN = re.compile(
    r"\bINCOHERENT_PREMISE\s*:\s*yes\b",
    re.IGNORECASE,
)


def turn_flags_incoherent_premise(content: str) -> bool:
    """Return True iff the turn's content contains the sentinel line.

    Pure helper so call sites and tests can probe a single string
    without building a transcript.
    """
    return bool(_INCOHERENT_PATTERN.search(content or ""))


def count_incoherent_premise_flags(turns: Iterable[AgentTurn]) -> int:
    """Return the number of DISTINCT voices that flagged incoherence.

    A voice counts at most once even if it emits the sentinel in
    multiple turns — the synthesizer cares about voice-level consensus,
    not turn count. The judge counts as a voice; if the judge alone
    flags incoherence, that's 1 (≥2 needed for KILL).
    """
    flagged: set[str] = set()
    for turn in turns:
        if turn_flags_incoherent_premise(turn.content):
            flagged.add(turn.agent)
    return len(flagged)


__all__ = [
    "count_incoherent_premise_flags",
    "turn_flags_incoherent_premise",
]
