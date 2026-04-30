"""Public types for the gecko-core SDK.

These models cross every boundary (CLI ↔ core, MCP ↔ core, API ↔ core).
Keep them stable — breaking changes here ripple to every consumer (CLI, MCP, API, web).
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl, field_validator

Tier = Literal["basic", "pro"]
SourceType = Literal["youtube", "web"]
SessionStatus = Literal["pending", "indexing", "generating", "complete", "failed"]


class SourceInfo(BaseModel):
    """A source indexed into a session's knowledge base."""

    url: HttpUrl
    type: SourceType
    chunk_count: int = Field(ge=0)
    indexed_at: datetime


class SourceCandidate(BaseModel):
    """A candidate source surfaced by discovery, before user approval / ingestion.

    Crosses CLI/MCP/API boundaries (the approval flow shows these to the user),
    so it lives in `models.py` alongside the other public types.
    """

    url: HttpUrl
    title: str = ""
    type: SourceType
    score: float = Field(default=0.0, ge=0.0, le=1.0)


class Citation(BaseModel):
    """A citation pointing back to a source chunk."""

    source_url: HttpUrl
    chunk_index: int
    similarity: float = Field(ge=0.0, le=1.0)


class BusinessPlan(BaseModel):
    """One-page business plan for the idea."""

    problem: str
    icp: str
    solution: str
    market: str
    business_model: str
    channels: str
    risks: list[str]
    citations: list[Citation]

    @field_validator(
        "problem", "icp", "solution", "market", "business_model", "channels", mode="before"
    )
    @classmethod
    def _coerce_to_string(cls, v: object) -> object:
        # LLMs often return prose fields as bullet lists. Join into a sentence.
        if isinstance(v, list):
            return "; ".join(str(item) for item in v)
        return v


GapClassification = Literal[
    "Full",
    "Partial:segment",
    "Partial:UX",
    "Partial:geo",
    "Partial:pricing",
    "Partial:integration",
    "False",
]
"""Structured taxonomy for the judge's gap assessment (S9-VERDICT-01).

Replaces prose hand-waving in the validation report with a single discrete
label so downstream consumers (CLI, PRD, future precedent labeler) can branch
on it without parsing free text.

Values:
  - ``Full``: an existing competitor fully covers the proposed wedge.
  - ``Partial:<facet>``: a competitor partially covers it; the named facet is
    where they fall short (segment, UX, geo, pricing, integration).
  - ``False``: there is no real gap — the demand the analyst surfaced doesn't
    actually go unmet (this is a kill signal, not a partial-cover signal).
"""


class Verdict(str, Enum):
    """S11-VERDICT-01 — single-token go/no-go signal surfaced to founders.

    Derived from the structured ``gap_classification`` plus advisor
    consensus (when available). The typed gap stays as evidence — Verdict
    is the headline.

    Mapping rule (see ``derive_verdict``):
      - ``Full`` or ``False``                      → ``KILL``
      - ``Partial:pricing`` / ``Partial:integration``
          AND advisor_consensus ≥ 0.8             → ``BUILD``
          else                                    → ``REFINE``
      - ``Partial:segment | UX | geo``             → ``REFINE``
    """

    KILL = "KILL"
    REFINE = "REFINE"
    BUILD = "BUILD"


# Threshold above which advisor consensus on the panel's "ship" sentiment
# flips a Partial:pricing / Partial:integration gap from REFINE to BUILD.
# 0.8 = 4-of-5 voices agree on the ship-shaped lever — leaves room for one
# legitimate dissent without softening the headline.
ADVISOR_CONSENSUS_BUILD_THRESHOLD: float = 0.8


def derive_verdict(
    gap: GapClassification,
    advisor_consensus: float | None = None,
) -> Verdict:
    """Map (gap_classification, advisor_consensus) → single-token Verdict.

    ``advisor_consensus`` is the fraction of advisor-panel voices whose
    closing line aligns with a ship/build sentiment. ``None`` means we
    have no advisor signal (basic tier, or pro debate that didn't emit
    explicit consensus): we treat that as 0.0 — i.e. lean REFINE for the
    pricing / integration partials rather than promoting them to BUILD on
    silence.
    """
    if gap in ("Full", "False"):
        return Verdict.KILL
    consensus = advisor_consensus if advisor_consensus is not None else 0.0
    if gap in ("Partial:pricing", "Partial:integration"):
        if consensus >= ADVISOR_CONSENSUS_BUILD_THRESHOLD:
            return Verdict.BUILD
        return Verdict.REFINE
    # Partial:segment | Partial:UX | Partial:geo — always REFINE; the
    # advisor panel can promote to BUILD once it weighs in via a separate
    # downstream call (gecko_advise / gecko_plan). The research output
    # itself stays conservative.
    return Verdict.REFINE


# Default applied when the judge fails to emit a valid gap_classification
# even after the retry. We bias toward 'Partial:segment' (rather than 'Full'
# or 'False') because segment-level partial cover is the modal real-world
# state for the dev-tool / regulated-vertical wedges Gecko evaluates, and
# defaulting there minimizes downstream miscategorization until Sprint 10's
# auto-labeler can correct it. The orchestrator logs a WARNING when this
# fallback fires so quality regressions are visible.
GAP_CLASSIFICATION_FALLBACK: GapClassification = "Partial:segment"


class ValidationReport(BaseModel):
    """Quantified validation of the idea."""

    market_size_signal: str
    competitor_analysis: str
    demand_evidence: str
    risk_flags: list[str]
    citations: list[Citation]
    # S9-VERDICT-01 — structured gap taxonomy. Required field; the basic.py
    # synthesizer enforces it and falls back to GAP_CLASSIFICATION_FALLBACK
    # with a logged warning if the LLM omits or mis-types the value after one
    # retry. Default in the model itself stays as the fallback so legacy
    # callers (tests, external SDK consumers on older versions) don't break.
    gap_classification: GapClassification = GAP_CLASSIFICATION_FALLBACK
    # One-liner that backs the gap_classification with a concrete competitor
    # comparison. Surfaced in `bb research` output and the PRD. Kept short so
    # it renders on a single CLI line.
    gap_summary: str = ""

    @field_validator("market_size_signal", "competitor_analysis", "demand_evidence", mode="before")
    @classmethod
    def _coerce_to_string(cls, v: object) -> object:
        if isinstance(v, list):
            return "; ".join(str(item) for item in v)
        return v


class PRD(BaseModel):
    """V1/V2/V3 scoped product requirements document."""

    v1_scope: list[str]
    v2_scope: list[str]
    v3_scope: list[str]
    acceptance_criteria: list[str]
    non_functional: list[str]
    success_metrics: list[str]
    citations: list[Citation]


class ResearchResult(BaseModel):
    """Result of a full `research()` workflow."""

    session_id: str
    tier: Tier
    business_plan: BusinessPlan
    validation_report: ValidationReport
    prd: PRD
    sources: list[SourceInfo]
    # Pro tier only — populated when `tier == "pro"`. Shape lives in
    # `gecko_core.orchestration.pro.transcript.DebateTranscript`. We keep the
    # field weakly-typed (dict | None) here so models.py stays free of an
    # orchestration import cycle. Pro callers serialize via model_dump().
    transcript: dict[str, object] | None = None
    # Pro tier only — the judge's final paragraph, surfaced as a quick
    # readout. None for basic tier and for pro runs that halted early.
    pro_session_summary: str | None = None
    # S11-VERDICT-01 — single-token verdict (KILL | REFINE | BUILD) derived
    # from the typed gap_classification + advisor consensus. The headline
    # the landing copy promises; the typed gap stays as evidence under it.
    # Defaults to REFINE so legacy callers that build a ResearchResult by
    # hand (tests, external SDK consumers on older versions) get a safe
    # value rather than a crash. Workflow code stamps the derived value.
    verdict: Verdict = Verdict.REFINE


class AskResult(BaseModel):
    """Result of a follow-up `ask()` query."""

    session_id: str
    answer: str
    citations: list[Citation]
