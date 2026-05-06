"""S20-OBS-01 — degraded-section tracker for the synth/post-processor pipeline.

Today, when a post-processor (e.g. ``market_landscape``) silently fails or
returns partial output, the resulting ``ResearchResult`` carries
``market_landscape: None`` with no signal that something dropped. A reader
cannot distinguish "no competitors found in the chunks" from "the section
failed to render" — and the second case should be loud.

This module wires a small, explicit tracker through the orchestration call
chain. Sections register themselves as degraded with a short reason string;
at the end of the workflow, the tracker mutates ``ResearchResult.degraded_sections``
and ``ResearchResult.degraded_reasons`` so the renderer (and ops dashboards)
can branch on the failure mode.

NOT thread-local on purpose — pass the tracker explicitly through the call
chain. Future work can layer a contextvar-based ambient tracker on top of
this surface; the explicit pass is the safer default for now.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gecko_core.models import ResearchResult

__all__ = ["DegradedSectionTracker"]


class DegradedSectionTracker:
    """Tracks which post-processor sections degraded during a run.

    Thin wrapper over a ``dict[str, str]`` mapping section name → short
    reason (e.g. ``"truncated_partial_recovery"``, ``"validation_dropped_all"``,
    ``"model_timeout"``). Empty state = clean run.
    """

    def __init__(self) -> None:
        self._reasons: dict[str, str] = {}

    def mark(self, section_name: str, reason: str) -> None:
        """Record ``section_name`` as degraded with ``reason``.

        Re-marking the same section overwrites the previous reason — last
        write wins. Reasons should be short, machine-greppable strings; the
        renderer can map them to user-facing copy separately.
        """
        self._reasons[section_name] = reason

    def clear(self, section_name: str) -> None:
        """Undo a prior ``mark`` for ``section_name``. Idempotent — clearing
        a section that was never marked is a no-op (no error).
        """
        self._reasons.pop(section_name, None)

    def to_dict(self) -> dict[str, str]:
        """Serializable snapshot of the tracker state.

        Returns a *copy*; mutating the returned dict does not affect the
        tracker. Suitable for INFO-log payloads / telemetry.
        """
        return dict(self._reasons)

    def apply_to(self, result: ResearchResult) -> ResearchResult:
        """Stamp the tracker state onto ``result.degraded_sections`` and
        ``result.degraded_reasons`` in place. Returns the same result for
        chaining at the call site.

        Mutation (rather than ``model_copy``) is intentional: the call sites
        (basic.py, workflows.py) build the result and immediately apply the
        tracker before returning, so mutating in place keeps the call shape
        flat. Pydantic v2 still validates field assignments via the model
        config at construction; ``list``/``dict`` field types accept the
        plain Python values written here.
        """
        result.degraded_sections = sorted(self._reasons.keys())
        result.degraded_reasons = dict(self._reasons)
        return result
