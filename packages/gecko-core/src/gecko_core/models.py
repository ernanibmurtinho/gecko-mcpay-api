"""Public types for the gecko-core SDK.

These models cross every boundary (CLI ↔ core, MCP ↔ core, API ↔ core).
Keep them stable — breaking changes here ripple to every consumer (CLI, MCP, API, web).
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal, TypedDict

from pydantic import BaseModel, Field, HttpUrl, field_validator

Tier = Literal["basic", "pro"]


# ---------------------------------------------------------------------------
# PaymentReceipt — the typed shape of ``Provenance.payment``.
#
# Defined here (a leaf module with no payments-side imports) to break the
# cycle that previously forced ``Provenance.payment`` to be typed as
# ``dict | None``: ``payments.protocol`` re-exports this name so the
# Protocol seam stays the canonical surface for consumers, while the
# definition itself sits where it can be referenced without dragging
# ``payments/__init__.py`` (and the four concrete client classes) into
# the import graph of ``gecko_core.models``.
#
# TypedDict (not a Pydantic model) on purpose:
#   1. Backwards compat — call sites build it inline as a plain dict and
#      index via ``payment["status"]``. A TypedDict is a dict at runtime,
#      so both keep working.
#   2. ``model_dump()`` round-trips a paid provider's receipt without a
#      second validation hop.
# ---------------------------------------------------------------------------


class PaymentReceipt(TypedDict, total=False):
    """The on-the-wire receipt stamped onto a ``Provenance`` for paid sources.

    All fields optional so partial receipts (e.g. stub-mode with no
    ``tx_signature``) round-trip cleanly through Pydantic.
    """

    intent_id: str
    status: Literal["success", "failed"]
    tx_signature: str | None
    facilitator_id: str
    network: str
    error: str | None


SourceType = Literal["youtube", "web", "provider"]
# S17-WEDGE-CITE-03 — accepted URI schemes for Citation.source_url and
# SourceInfo.url. "https" is the legacy web/youtube/arxiv path; "bazaar"
# and "twitsh" are synthetic URIs minted in workflows._dispatch_stub_integration_providers
# (see design memo §3.2). Order matters for the prefix check — "https://"
# must precede shorter schemes to avoid false positives. Drift between
# this tuple and the citation validator below is caught by
# tests/orchestration/test_basic.py.
_ALLOWED_CITATION_SCHEMES: tuple[str, ...] = (
    "https://",
    "http://",
    "bazaar://",
    "twitsh://",
)


def _validate_citation_uri(value: str) -> str:
    s = str(value).strip()
    for scheme in _ALLOWED_CITATION_SCHEMES:
        if s.startswith(scheme):
            return s
    raise ValueError(
        f"citation/source URI must use one of {_ALLOWED_CITATION_SCHEMES}; got {value!r}"
    )


SessionStatus = Literal["pending", "indexing", "generating", "complete", "failed"]


class SourceInfo(BaseModel):
    """A source indexed into a session's knowledge base.

    S17-WEDGE-CITE-03 — `url` was previously `HttpUrl`; relaxed to a string
    with scheme validation so synthetic URIs from non-web providers
    (`bazaar://`, `twitsh://`) round-trip cleanly through `list_sources`.
    Validator ensures we still reject garbage.
    """

    url: str
    type: SourceType
    chunk_count: int = Field(ge=0)
    indexed_at: datetime

    @field_validator("url")
    @classmethod
    def _check_url(cls, v: str) -> str:
        return _validate_citation_uri(v)


class SourceCandidate(BaseModel):
    """A candidate source surfaced by discovery, before user approval / ingestion.

    Crosses CLI/MCP/API boundaries (the approval flow shows these to the user),
    so it lives in `models.py` alongside the other public types.
    """

    url: HttpUrl
    title: str = ""
    type: SourceType
    score: float = Field(default=0.0, ge=0.0, le=1.0)


class Provenance(BaseModel):
    """Where a Citation's evidence came from + how it was paid for.

    Sprint 12 S12-PROVIDER-01: stamped onto every Citation so the
    verdict-renderer (and the S13+ Bazaar receipt anatomy) can surface
    "evidence from <provider>, paid via <facilitator>, $X" without
    changing the verdict shape itself.

    Defaults to the free Tavily path so existing call sites that build
    a Citation without specifying provenance keep working unchanged —
    the Pydantic default fires and the field carries through to the
    serialized output.

    ``payment`` is typed as :class:`PaymentReceipt` (a TypedDict) as of
    S13-PAY-01 — the X402Client Protocol seam now exists, and the receipt
    shape lives at this leaf module so the import cycle that previously
    forced ``dict | None`` is gone. Existing call sites that build
    receipts as plain dicts keep working unchanged: a TypedDict is a dict
    at runtime, and indexing (``payment["status"]``) still works.
    """

    provider_name: str = "tavily"
    # Mirrors `gecko_core.ingestion.providers.ProviderKind`. Kept as a
    # plain str (not Literal) here to avoid an ingestion → models import
    # back-edge; the providers module is the source of truth for the
    # accepted vocabulary.
    provider_kind: str = "free"
    payment: PaymentReceipt | None = None


class Citation(BaseModel):
    """A citation pointing back to a source chunk.

    S17-WEDGE-CITE-03 — `source_url` was previously `HttpUrl`; relaxed to a
    string with scheme validation so non-web providers can cite via
    `bazaar://<service>/<resource>` and `twitsh://<id>` URIs (design memo
    §3.2). The validator still rejects schemeless garbage, so hallucinated
    URLs fail loudly.
    """

    source_url: str
    chunk_index: int
    similarity: float = Field(ge=0.0, le=1.0)

    @field_validator("source_url")
    @classmethod
    def _check_source_url(cls, v: str) -> str:
        return _validate_citation_uri(v)

    # S12-PROVIDER-01 — defaults to free/tavily so legacy call sites
    # (LLM-emitted JSON, hand-built test citations) get a sensible
    # provenance without changes. Paid providers (S13+) override.
    provenance: Provenance = Field(default_factory=Provenance)
    # S13-CITE-01 — optional creator attribution surface (pre-payment for
    # S14 Paragraph creator connector). When all three are None the
    # rendering is byte-identical to pre-S13. When populated, the
    # citation renderer surfaces the handle inline and adds a "Creator
    # payouts" footer block to the receipt anatomy. Tokens / wallet
    # private keys never live on this model — `creator_wallet` is the
    # public payout address only.
    creator_handle: str | None = None
    creator_payout_usd: float | None = None
    creator_wallet: str | None = None


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
    """S11-VERDICT-01 / S17-TONE-01 — single-token go/no-go signal surfaced
    to founders.

    Derived from the structured ``gap_classification`` plus advisor
    consensus (when available). The typed gap stays as evidence — Verdict
    is the headline.

    Mapping rule (see ``derive_verdict``):
      - ``Full`` or ``False``                      → ``PIVOT``
      - ``Partial:pricing`` / ``Partial:integration``
          AND advisor_consensus ≥ 0.8             → ``GO``
          else                                    → ``REFINE``
      - ``Partial:segment | UX | geo``             → ``REFINE``

    S17-TONE-01 — vocabulary softened: ``KILL`` → ``PIVOT`` (same semantics,
    framed as a redirect rather than a kill); ``BUILD`` → ``GO`` (more
    energizing, shorter). ``REFINE`` is unchanged. The ``_missing_`` hook
    below maps legacy ``"KILL"`` / ``"BUILD"`` strings (still present in
    older ``result_json`` rows + on-disk transcripts) to the new members so
    deserialization stays backwards-compatible. The 20260502 migration
    backfills the JSONB rows for clean read paths; the shim is defense in
    depth for any row written by an older deploy mid-rollout or by an
    external SDK consumer that pinned an older Verdict value.
    """

    PIVOT = "PIVOT"
    REFINE = "REFINE"
    GO = "GO"

    @classmethod
    def _missing_(cls, value: object) -> Verdict | None:
        # Backwards-read shim for legacy rows / external callers still
        # emitting the old vocabulary. Case-insensitive on the off chance a
        # consumer lower-cased the token before persisting.
        if isinstance(value, str):
            upper = value.upper()
            if upper == "KILL":
                return cls.PIVOT
            if upper == "BUILD":
                return cls.GO
        return None


# Threshold above which advisor consensus on the panel's "ship" sentiment
# flips a Partial:pricing / Partial:integration gap from REFINE to GO.
# 0.8 = 4-of-5 voices agree on the ship-shaped lever — leaves room for one
# legitimate dissent without softening the headline.
ADVISOR_CONSENSUS_BUILD_THRESHOLD: float = 0.8

# S17-VERDICT-01 — evidence-strength floor. Below either threshold the
# verdict is forced to REFINE regardless of gap classification: Gecko's
# wedge is grounded provenance, and a verdict built on ≤2 chunks or sub-
# 0.40 cosine similarity is the model bluffing. Tune downward only with
# fixture evidence (≥2 baseline runs).
SIGNAL_STRENGTH_MIN_CITATIONS: int = 3
SIGNAL_STRENGTH_MIN_SIMILARITY: float = 0.40


def is_low_grounding(citations: list[Citation] | None) -> bool:
    """Return True when citation evidence is too weak to back a confident
    PIVOT/GO verdict. The two failure modes are:

      * fewer than ``SIGNAL_STRENGTH_MIN_CITATIONS`` chunks total
      * no chunk above ``SIGNAL_STRENGTH_MIN_SIMILARITY`` cosine similarity

    Either condition fires the floor. Empty/None citations are low-grounding.
    """
    if not citations:
        return True
    if len(citations) < SIGNAL_STRENGTH_MIN_CITATIONS:
        return True
    max_sim = max((c.similarity for c in citations), default=0.0)
    return max_sim < SIGNAL_STRENGTH_MIN_SIMILARITY


def derive_verdict(
    gap: GapClassification,
    advisor_consensus: float | None = None,
    citations: list[Citation] | None = None,
) -> Verdict:
    """Map (gap_classification, advisor_consensus, citations) → single-token Verdict.

    ``advisor_consensus`` is the fraction of advisor-panel voices whose
    closing line aligns with a ship/go sentiment. ``None`` means we
    have no advisor signal (basic tier, or pro debate that didn't emit
    explicit consensus): we treat that as 0.0 — i.e. lean REFINE for the
    pricing / integration partials rather than promoting them to GO on
    silence.

    ``citations`` enables the S17-VERDICT-01 evidence-strength floor: when
    the grounding evidence is too thin (see :func:`is_low_grounding`), the
    verdict floors to REFINE regardless of gap. The floor is the wedge —
    Gecko's claim to a verdict rests on the chunks the model can point at.
    Pass ``None`` to skip the floor (legacy callers, or contexts where
    the floor is enforced upstream).
    """
    if citations is not None and is_low_grounding(citations):
        return Verdict.REFINE
    if gap in ("Full", "False"):
        return Verdict.PIVOT
    consensus = advisor_consensus if advisor_consensus is not None else 0.0
    if gap in ("Partial:pricing", "Partial:integration"):
        if consensus >= ADVISOR_CONSENSUS_BUILD_THRESHOLD:
            return Verdict.GO
        return Verdict.REFINE
    # Partial:segment | Partial:UX | Partial:geo — always REFINE; the
    # advisor panel can promote to GO once it weighs in via a separate
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
    # S11-VERDICT-01 / S17-TONE-01 — single-token verdict (PIVOT | REFINE | GO) derived
    # from the typed gap_classification + advisor consensus. The headline
    # the landing copy promises; the typed gap stays as evidence under it.
    # Defaults to REFINE so legacy callers that build a ResearchResult by
    # hand (tests, external SDK consumers on older versions) get a safe
    # value rather than a crash. Workflow code stamps the derived value.
    verdict: Verdict = Verdict.REFINE
    # S17-VERDICT-01 — True when the citation evidence backing this
    # verdict failed the signal-strength floor (see ``is_low_grounding``).
    # The verdict is already floored to REFINE in that case; this flag
    # lets the renderer surface "low confidence — gather more sources"
    # instead of pretending certainty.
    low_grounding: bool = False


class AskResult(BaseModel):
    """Result of a follow-up `ask()` query."""

    session_id: str
    answer: str
    citations: list[Citation]
