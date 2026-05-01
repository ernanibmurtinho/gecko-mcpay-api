"""Public Pydantic models for the Advisor Panel (S4-ADVISOR-02).

Voices are independent (not GroupChat). Each emits a markdown response
with a fixed closing-line shape per voice — extracted into
``AdvisorVoice.closing_line`` so callers can render a one-line summary
without re-parsing prose.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from gecko_core.models import Citation, GapClassification, Verdict
from gecko_core.routing.catalog import AgentRole

# Stable rendering order — CEO first (sets strategy), staff_manager last
# (synthesizes the others into a sprint plan). The orchestration shell
# guarantees this order even though voices run in parallel.
PANEL_VOICE_ORDER: tuple[AgentRole, ...] = (
    AgentRole.ceo,
    AgentRole.cto,
    AgentRole.business_manager,
    AgentRole.product_manager,
    AgentRole.staff_manager,
)


class AdvisorVoice(BaseModel):
    """One voice's full response + extracted accounting."""

    role: AgentRole
    model_used: str
    output_md: str
    closing_line: str
    tokens_in: int
    tokens_out: int
    cost_usd: float | None = None
    # S9-ADVISOR-01: structured failure mode for quality monitoring. ``None``
    # means the voice produced a compliant closing line (possibly on retry).
    # ``"no_closing_line"`` means both attempts failed regex extraction.
    error_kind: str | None = None


class AdvisorPanel(BaseModel):
    """The 5-voice panel. Voices are always present in PANEL_VOICE_ORDER."""

    session_id: str
    voices: list[AdvisorVoice]
    total_cost_usd: float
    generated_at: datetime = Field(default_factory=lambda: datetime.now().astimezone())
    # S9-ADVISOR-01: count of voices that failed closing-line extraction
    # after retry. Surface for dogfood / monitoring drift detection.
    voices_no_closing_line: int = 0


class PulseDelta(BaseModel):
    """Per-voice delta between two panel runs (S4-ADVISOR-05)."""

    role: AgentRole
    previous_closing_line: str | None
    current_closing_line: str
    changed: bool
    reason: str | None = None  # free-text explanation if we can attribute the shift


class PulsePanel(BaseModel):
    """A pulse run: a fresh AdvisorPanel + per-voice deltas vs the prior run."""

    panel: AdvisorPanel
    deltas: list[PulseDelta]
    previous_panel_at: datetime | None = None


class PulseResult(BaseModel):
    """S14-PULSE-01 — user-facing result of `gecko_pulse <session_id>`.

    Wraps the existing PulsePanel and adds the founder-facing headline:
    a single-token verdict + structured gap_classification + 3-bullet
    "what changed since last pulse" summary + fresh citations.

    A pulse run creates a NEW session row with ``phase="during_build"``
    and ``parent_session_id`` set to the input session_id (the original
    research). ``pulse_session_id`` is that new row's UUID; callers can
    walk the pulse-history chain via the FK.

    The 3-bullet ``summary_bullets`` is a TEXT-ONLY summary in v14 — no
    delta computation. The full delta render lands in S15-PULSE-DELTA-01.
    """

    parent_session_id: str
    pulse_session_id: str
    idea: str
    verdict: Verdict
    gap_classification: GapClassification
    panel: AdvisorPanel
    citations: list[Citation] = Field(default_factory=list)
    summary_bullets: list[str] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=lambda: datetime.now().astimezone())
    # 12-pack credit accounting (S14-PULSE-02). When non-None, the call
    # decremented a prepaid credit instead of settling per-call.
    credits_remaining_after: int | None = None


class AdvisorError(Exception):
    """Raised when the advisor panel can't produce a result."""


class AdvisorSessionNotFoundError(AdvisorError):
    """The session_id doesn't exist or has been soft-deleted.

    NOTE: unlike the scaffold pipeline, the advisor panel WILL run on
    sessions whose verdict is ``kill`` — pivot advice is still informative
    after a kill. This error is reserved for genuinely missing sessions.
    """


__all__ = [
    "PANEL_VOICE_ORDER",
    "AdvisorError",
    "AdvisorPanel",
    "AdvisorSessionNotFoundError",
    "AdvisorVoice",
    "PulseDelta",
    "PulsePanel",
    "PulseResult",
]
