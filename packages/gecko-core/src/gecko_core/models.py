"""Public types for the gecko-core SDK.

These models cross every boundary (CLI ↔ core, MCP ↔ core, API ↔ core).
Keep them stable — breaking changes here ripple to every consumer (CLI, MCP, API, web).
"""

from __future__ import annotations

from datetime import datetime
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


class AskResult(BaseModel):
    """Result of a follow-up `ask()` query."""

    session_id: str
    answer: str
    citations: list[Citation]
