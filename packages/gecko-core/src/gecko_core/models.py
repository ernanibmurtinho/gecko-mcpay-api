"""Public types for the gecko-core SDK.

These models cross every boundary (CLI ↔ core, MCP ↔ core, API ↔ core).
Keep them stable — breaking changes here ripple to every consumer (CLI, MCP, API, web).
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal, TypedDict

from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator

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


class CitationMarker(BaseModel):
    """S20-C-CITATION-CONTRACT-01 — structured `[N]` → chunk_id marker map.

    Distinct from :class:`Citation` (which carries (source_url, chunk_index,
    similarity, provenance) and is the *evidence* shape attached to plan /
    validation_report / prd). ``CitationMarker`` is the *prose-marker* shape:
    each item maps an inline ``[N]`` reference in the synth output to the
    chunk_id of the chunk it cites. It powers two downstream features that
    Citation alone can't:

      - the C4 enrichment writer bumps ``usage_count`` on chunks that were
        *cited in output*, not merely *retrieved* (per user decision);
      - telemetry can correlate ``[N]`` markers in rendered prose to the
        underlying chunk store row without re-parsing the source_url.

    Fields are deliberately small. ``span`` is optional and ≤200 chars when
    present — enough to make a citation auditable without bloating the row.
    """

    idx: int = Field(ge=1)
    doc_id: str = Field(min_length=1)
    url: str
    span: str | None = Field(default=None, max_length=200)

    @field_validator("url")
    @classmethod
    def _check_url(cls, v: str) -> str:
        return _validate_citation_uri(v)


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


IdeaClassification = Literal["greenfield", "iterative", "unclear"]
"""S21-CALIBRATION-CLASSIFY-01 — first-pass classifier emitted by the
judge agent under the ``--calibration colosseum`` regime.

Values:
  - ``greenfield``: a new product category with no incumbent. Evidence
    requirement is experimental rigor + willingness to be wrong + a
    novel mechanism.
  - ``iterative``: an improvement to an existing category. Evidence
    requirement is organic users, real feedback loops, category-specific
    PMF metrics, and the absence of airdrop-farmer dependency.
  - ``unclear``: classification is genuinely ambiguous (rare; the judge
    is instructed to attempt a call before falling back).

Distilled from the public-feedback corpus (``docs/judges/sources/``) into
the calibration block — the framework attribution is in the corpus, not
in the verdict surface. Excluded from ``verdict_hash._verdict_payload``
because calibration prompts influence prose tone, not structural
verdict shape; reruns under the same retrieval must reproduce the
digest regardless of which classifier label the judge emitted.
"""


FounderPosture = Literal["high", "moderate", "unclear"]
"""S21-CALIBRATION-FOUNDER-POSTURE-01 — parallel founder-lens classifier
emitted by the post-processor batch under the calibration regime.

Where ``IdeaClassification`` evaluates the IDEA (greenfield vs iterative
+ evidence demand), ``FounderPosture`` evaluates the FOUNDER's surfaced
force-of-will: contrarian thinking, willingness to be wrong, evidence of
shipping, openness to public pushback. Distilled from the second
calibration corpus (web3 accelerators / Alliance DAO public threads) into
the calibration block — same anti-naming rule: the framework attribution
stays in the corpus, never in the voice output.

Values:
  - ``high``: the idea text or surfaced founder context shows contrarian
    framing, prior shipping evidence, public solicitation of pushback,
    or named time-to-pay urgency. Strong signal regardless of idea polish.
  - ``moderate``: some founder signal is present but mixed (e.g. shipping
    history without contrarian framing, or contrarian framing without
    evidence of follow-through).
  - ``unclear``: no founder context surfaced (the modal case for an idea
    submitted as a single sentence). The Critic flags the gap rather than
    inferring posture from the idea polish alone.

Excluded from ``verdict_hash._verdict_payload`` for the same reason as
``idea_classification`` — calibration tilt must not flap the digest.
"""


ProviderMixFlag = Literal[
    "balanced",
    "single_provider_dominates",
    "thin_diversity",
]
"""S20-PROVIDER-MIX-FLOOR-01 — soft, informational audit of the
provider_kind distribution in a verdict's citation set.

Values:
  - ``balanced``: ≥3 distinct provider_kinds AND no kind > 50% of citations.
  - ``single_provider_dominates``: one provider_kind exceeds 50% of citations.
    More severe than ``thin_diversity`` because the verdict leans on one
    source even if several technically appear.
  - ``thin_diversity``: fewer than 3 distinct provider_kinds in the set.

The flag is informational only — it never changes the verdict and is
deliberately excluded from ``verdict_hash`` inputs so reruns under the
same retrieval reproduce the digest.
"""


class Verdict(str, Enum):
    """S11-VERDICT-01 / S17-TONE-01 / S20-COHERENCE-VERDICT-LABEL-01 —
    single-token go/no-go signal surfaced to founders.

    Derived from the structured ``gap_classification`` plus advisor
    consensus (when available) plus the pro-tier coherence-flag count.
    The typed gap stays as evidence — Verdict is the headline.

    Mapping rule (see ``derive_verdict``):
      - ≥2 critic/voice flags of ``incoherent_premise``  → ``KILL``  (pro-tier only)
      - ``Full`` or ``False``                            → ``PIVOT``
      - ``Partial:pricing`` / ``Partial:integration``
          AND advisor_consensus ≥ 0.8                   → ``GO``
          else                                          → ``REFINE``
      - ``Partial:segment | UX | geo``                   → ``REFINE``

    S17-TONE-01 — vocabulary softened: ``KILL`` → ``PIVOT`` (same semantics,
    framed as a redirect rather than a kill); ``BUILD`` → ``GO`` (more
    energizing, shorter). ``REFINE`` is unchanged.

    S20-COHERENCE-VERDICT-LABEL-01 — ``KILL`` is RE-INTRODUCED with refined
    semantics distinct from the legacy v1 token. The legacy KILL meant
    "weak idea, don't build as-is" and was renamed to PIVOT in S17. The
    new KILL means "premise is INCOHERENT or unverifiable; no amount of
    refinement saves it" — the dog-emotional-AI-blockchain class of idea
    where the wedge can't be made real because the components don't
    compose. Trigger condition is structural: ≥2 voices in the 5-agent
    pro-tier debate flag ``incoherent_premise``. Basic tier never emits
    KILL (no debate to count flags from). The ``_missing_`` hook below
    maps the LEGACY ``"KILL"`` string still present in older
    ``result_json`` rows / on-disk transcripts to ``PIVOT`` (their actual
    semantic), NOT to the new KILL — the fresh KILL only ever comes from
    the synthesizer's coherence-flag count, never from an upstream string.
    The 20260502120000 migration backfills the JSONB rows for clean read
    paths; the shim is defense in depth for any row written by an older
    deploy mid-rollout or by an external SDK consumer that pinned an
    older Verdict value.
    """

    PIVOT = "PIVOT"
    REFINE = "REFINE"
    GO = "GO"
    KILL = "KILL"  # S20-COHERENCE-VERDICT-LABEL-01 — premise incoherence

    @classmethod
    def _missing_(cls, value: object) -> Verdict | None:
        # Backwards-read shim for legacy rows / external callers still
        # emitting the old vocabulary. Case-insensitive on the off chance a
        # consumer lower-cased the token before persisting. The legacy
        # "KILL" string maps to PIVOT (S17 rename) — NOT to the new
        # S20 KILL, which is only ever produced fresh by the synthesizer
        # from a coherence-flag count, never deserialised from an
        # upstream string. Exact-case "KILL" already resolves via the
        # native enum lookup before _missing_ fires, so this shim is only
        # reached for differently-cased / typoed legacy values.
        if isinstance(value, str):
            upper = value.upper()
            if upper == "BUILD":
                return cls.GO
        return None


# Threshold above which advisor consensus on the panel's "ship" sentiment
# flips a Partial:pricing / Partial:integration gap from REFINE to GO.
# 0.8 = 4-of-5 voices agree on the ship-shaped lever — leaves room for one
# legitimate dissent without softening the headline.
ADVISOR_CONSENSUS_BUILD_THRESHOLD: float = 0.8

# S20-COHERENCE-VERDICT-LABEL-01 — minimum number of distinct voices in
# the 5-agent pro debate that must flag ``incoherent_premise`` for the
# headline to flip to KILL. ≥2 is the floor: a single dissenter could be
# noise, two converging dissents on premise (not feasibility, not market)
# is the structural signal. Bump only with fixture evidence.
COHERENCE_KILL_MIN_FLAGS: int = 2


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
    *,
    incoherence_flag_count: int = 0,
) -> Verdict:
    """Map (gap_classification, advisor_consensus, citations, coherence) →
    single-token Verdict.

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

    ``incoherence_flag_count`` (S20-COHERENCE-VERDICT-LABEL-01) is the
    number of distinct voices in the 5-agent pro debate that flagged
    ``incoherent_premise`` (the dog-emotional-AI-blockchain class — premise
    can't compose, no refinement saves it). When the count is at or above
    :data:`COHERENCE_KILL_MIN_FLAGS` (default 2), Verdict.KILL fires
    regardless of gap classification, advisor consensus, or grounding —
    incoherence overrides every other signal because the founder
    surface lies if it says "refine" to a premise that doesn't compose.
    Defaults to 0 so basic-tier callers (no debate) and legacy callers
    keep their existing GO/REFINE/PIVOT mapping unchanged. KILL is
    pro-tier-only at first; if a future basic-tier coherence check ships,
    extend this kwarg through ``orchestration.basic.generate``.
    """
    # S20: KILL on premise incoherence overrides every other signal —
    # including the low-grounding floor. An incoherent premise with weak
    # grounding is still incoherent; flooring to REFINE would mislead.
    if incoherence_flag_count >= COHERENCE_KILL_MIN_FLAGS:
        return Verdict.KILL
    # PIVOT on a clear invalidation (Full/False gap) regardless of grounding —
    # a sparse evidence set for a non-existent problem is still PIVOT.
    if gap in ("Full", "False"):
        return Verdict.PIVOT
    if citations is not None and is_low_grounding(citations):
        return Verdict.REFINE
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
    # 2026-05-03 dogfood — `gap_classification` alone is opaque (a viewer of
    # `Partial:UX` cannot tell whether the wedge category is weak or the
    # presentation is shallow). `gap_explanation` is the 2-3 sentence
    # narrative that grounds the label in specific chunks via inline `[n]`
    # markers and names the consequence for the founder. Optional for
    # backwards compat with existing eval cassettes / serialized verdicts
    # that pre-date the field; new runs populate it from the basic-tier
    # synthesizer (pro tier inherits via the basic ValidationReport). The
    # prose drifts run-to-run even when the structural decision is identical,
    # so it is deliberately EXCLUDED from `verdict_hash._verdict_payload` —
    # same posture as `pro_session_summary` and the post-processor readouts.
    gap_explanation: str | None = None

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

    @field_validator(
        "v1_scope",
        "v2_scope",
        "v3_scope",
        "acceptance_criteria",
        "non_functional",
        "success_metrics",
        mode="before",
    )
    @classmethod
    def _coerce_str_to_list(cls, v: object) -> object:
        # Permissive boundary coercion — same posture as the
        # `low_grounding` / `provider_mix_flag` flags: we'd rather absorb
        # mild LLM output drift than fail validation on a string-vs-list
        # flap. Three observed shapes from real prompt invocations:
        #   1. The model returns a single string instead of a list — wrap.
        #   2. The model returns list items as dicts (e.g.
        #      ``{"criterion": "..."}`` or ``{"text": "..."}``). Pydantic
        #      would then reject each dict against ``str``. Flatten by
        #      pulling the first string-valued field out.
        #   3. The model returns a list with mixed string + dict items.
        if isinstance(v, str):
            return [v] if v.strip() else []
        if isinstance(v, list):
            normalised: list[str] = []
            for item in v:
                if isinstance(item, str):
                    if item.strip():
                        normalised.append(item)
                elif isinstance(item, dict):
                    # Pull the first non-empty string value from the dict.
                    # Common LLM keys: criterion / text / value / item /
                    # description. We don't enumerate; the first string-
                    # valued entry wins.
                    for candidate in item.values():
                        if isinstance(candidate, str) and candidate.strip():
                            normalised.append(candidate)
                            break
                else:
                    normalised.append(str(item))
            return normalised
        return v


# ---------------------------------------------------------------------------
# v5.5 demo post-processor readouts (S20-DEMO-OUTPUT-01).
#
# Canonical VoiceName / VoiceStatus / DissentStatus Literals live in
# ``gecko_core.orchestration.pro.voices`` per CLAUDE.md Pattern A.
# These fields are POST-HOC readouts: they describe the debate, they don't
# change the verdict. They are deliberately excluded from
# ``verdict_hash._verdict_payload``.
# ---------------------------------------------------------------------------

from gecko_core.voices import (  # noqa: E402
    DissentStatus,
    VoiceName,
    VoiceStatus,
)


class VoicePosition(BaseModel):
    name: VoiceName
    position: str | None
    tension: str | None
    recommendation: str | None
    status: VoiceStatus


class PerVoiceReadout(BaseModel):
    voices: list[VoicePosition]


CompetitorFlag = Literal["cannot_articulate_difference"]
LandscapeSectionFlag = Literal[
    "insufficient_competitors_in_chunks",
    # S20-FIX-05 — best-effort recovery flags raised by the
    # ``market_landscape`` post-processor when the LLM output truncated
    # mid-list. ``truncated_partial_recovery`` = at least one valid
    # competitor parsed from the prefix; ``truncated_zero_recovery`` =
    # no parseable prefix, ``competitors=[]`` propagates so the renderer
    # can show a degraded-section indicator instead of a silent ``null``.
    "truncated_partial_recovery",
    "truncated_zero_recovery",
]

# v5.5 — closed list of differentiator axes the market_landscape post-
# processor picks from. Decoupled from `why_we_are_not_them` (sentence)
# so the model cannot collapse the prose field into the bare axis name
# (the bug fixed in the 2026-05-02 dogfood smokes). The renderer only
# surfaces the sentence in V1; `axis` stays for analytics.
CompetitorAxis = Literal[
    "verdict_shape",
    "debate_vs_single_voice",
    "judge_attribution",
    "falsifier_layer",
    "settlement_layer",
    "contributor_reputation",
    "provider_mix",
    "surviving_dissent",
]


class Competitor(BaseModel):
    name: str
    what_they_do: str
    axis: CompetitorAxis | None = None
    why_we_are_not_them: str | None = None
    flag: CompetitorFlag | None = None

    # 2026-05-03 dogfood bug: gpt-4o-mini intermittently emits the
    # competitor description under `description` (and occasionally
    # `summary` / `what_they_do_text`) instead of the prompted
    # `what_they_do`, even though the system message names the field
    # explicitly. Same permissive boundary posture as PRD list coercion
    # and `low_grounding`: absorb the drift here rather than fail the
    # whole MarketLandscape and force a `--refresh` retry. The prompt
    # remains the source of truth; this is a safety net, not the spec.
    @model_validator(mode="before")
    @classmethod
    def _coerce_what_they_do_aliases(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        if data.get("what_they_do"):
            return data
        for alias in ("description", "what_they_do_text", "summary", "details"):
            value = data.get(alias)
            if isinstance(value, str) and value.strip():
                data = {**data, "what_they_do": value}
                return data
        return data


class MarketLandscape(BaseModel):
    competitors: list[Competitor]
    section_flag: LandscapeSectionFlag | None = None


class Dissent(BaseModel):
    voice: VoiceName
    verbatim: str
    on_topic: str


class SurvivingDissent(BaseModel):
    dissent_status: DissentStatus
    dissents: list[Dissent] = Field(default_factory=list)
    rationale: str = ""


class Falsifier(BaseModel):
    what_would_disprove_this: str
    by_when: str


class NextStep(BaseModel):
    action: str
    surfaced_by_voice: VoiceName
    falsifier: Falsifier


class NextStepsWithFalsifiers(BaseModel):
    steps: list[NextStep] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# v5.5 `bb refine <hash>` output (S20-REFINE-IDEA-01).
#
# Produced by the `refine_idea` prompt in `_default_prompts_v5_5.json`. NOT
# part of the verdict — refinement is a post-hoc editorial pass on a saved
# verdict. Excluded from `verdict_hash._verdict_payload` for the same reason
# the post-processor readouts are: refining the prose does not change what
# the verdict said.
#
# `confidence` is bucketed (sharper / narrower / pivoted) per CLAUDE.md
# conventions ("bucketed reputation/score bands, never raw floats public"):
# even though this is not a public score, the same discipline of named
# buckets > raw scalars makes the field meaningful at a glance.
# ---------------------------------------------------------------------------

RefinementConfidence = Literal["sharper", "narrower", "pivoted"]


class DissentResolution(BaseModel):
    dissent_quote: str
    voice: VoiceName
    resolution: str


class RefinedIdea(BaseModel):
    refined_statement: str
    addresses_dissent: list[DissentResolution] = Field(default_factory=list)
    new_falsifiers_now_harder: list[str] = Field(default_factory=list)
    what_it_no_longer_claims: list[str] = Field(default_factory=list)
    what_it_now_claims_instead: list[str] = Field(default_factory=list)
    confidence: RefinementConfidence


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
    # 2026-05-03 v0.1.10 dogfood — True when ``validation_report.gap_explanation``
    # is None or empty after the basic-tier synthesizer's bounded retry
    # loop. Mirrors the ``low_grounding`` posture: we surface the model's
    # non-compliance honestly rather than synthesise a fallback narrative
    # from ``gap_summary`` / ``gap_classification``. Renderers / API
    # consumers branch on this flag to display "the model did not produce
    # an explanation; treat the verdict letter alone" instead of printing
    # ``None``. Excluded from ``verdict_hash._verdict_payload`` — like
    # ``gap_explanation`` itself, this is a prose-surface property of the
    # specific run rather than the structural verdict shape.
    low_explanation: bool = False
    # S20-PROVIDER-MIX-FLOOR-01 — informational audit of the provider_kind
    # mix in the citation set. Computed post-synthesis (see
    # ``gecko_core.orchestration.provider_mix_audit.audit_provider_mix``).
    # Does NOT change the verdict; the renderer surfaces a soft warning
    # when provenance is too narrow. ``None`` for legacy code paths that
    # haven't run the audit (kept Optional so existing serialised rows
    # round-trip without backfill). Verdict-hash inputs deliberately
    # exclude this field — see ``verdict_hash._verdict_payload``.
    provider_mix_flag: ProviderMixFlag | None = None
    # S20-VERDICT-URL-IMPL-01 — deterministic sha256 stamped at workflow
    # finalisation (after ``provider_mix_flag``) via
    # :func:`gecko_core.verdict_hash.verdict_hash`. Identifies the verdict on
    # the public ``/v1/verdict/{hash}`` URL surface and joins to the
    # persisted ``judge_transcripts`` row. Optional/None for legacy callers
    # and for results built by hand in tests; the workflow always stamps it.
    verdict_hash: str | None = None
    # v5.5 demo post-processor readouts — pro tier only, populated AFTER
    # verdict synthesis. Excluded from verdict_hash inputs so prompt drift
    # in these prose surfaces doesn't flap the hash.
    per_voice: PerVoiceReadout | None = None
    transcript_summary: str | None = None
    market_landscape: MarketLandscape | None = None
    surviving_dissent: SurvivingDissent | None = None
    next_steps_with_falsifiers: NextStepsWithFalsifiers | None = None
    # S21-CALIBRATION-01 — opaque corpus identifier when the run was
    # calibrated against an external judge corpus (e.g.
    # ``"colosseum:34_judges:2026-05-03"``). Informational only —
    # excluded from verdict-hash inputs because calibration prompts
    # influence prose tone, not the structural verdict shape.
    calibration_corpus: str | None = None
    # S21-CALIBRATION-CLASSIFY-01 — judge's first-pass framing of the
    # idea (greenfield / iterative / unclear) emitted under the
    # ``--calibration colosseum`` regime. ``None`` when the run did not
    # use a calibration corpus that requests classification, or when
    # the judge prose did not include the sentinel line. Excluded from
    # verdict-hash inputs (see ``verdict_hash._verdict_payload``) so
    # the calibration tilt does not flap the digest.
    idea_classification: IdeaClassification | None = None
    # S21-CALIBRATION-FOUNDER-POSTURE-01 — parallel founder-lens classifier
    # produced by the v5.5.2 post-processor batch (the regex sentinel is
    # extracted first; the post-processor JSON call fills in when the
    # judge prose drops it). ``None`` when the run did not use a calibration
    # regime that requests founder evaluation, when no founder signal was
    # present in the idea text (modal case → "unclear" still gets stamped
    # if the post-processor saw the transcript at all; ``None`` only when
    # the post-processor itself failed). Excluded from verdict-hash inputs
    # — see the FounderPosture docstring above.
    founder_posture: FounderPosture | None = None
    # S21-REFINE-01 — populated by ``bb refine <hash>`` when the founder
    # post-hoc refines this verdict's idea. One slot for V1.5
    # (multiple refinements over time = V2). Excluded from verdict-hash
    # inputs for the same reason as the post-processor readouts.
    refinement: RefinedIdea | None = None
    # S23-REPORT-01 — persisted AdvisorPanel JSON, written by the /plan
    # route after generate_panel() succeeds. Stored as a raw dict so
    # models.py stays free of an orchestration import cycle (same posture
    # as `transcript`). None when gecko_plan was not called for this
    # session. Excluded from verdict_hash inputs.
    advisor_panel: dict[str, object] | None = None
    # S20-C-CITATION-CONTRACT-01 — chunk_ids actually referenced by ``[N]``
    # markers in synthesized prose, after hallucination-drop validation
    # against the rag_context chunk set. Empty by default so legacy callers
    # / rows that pre-date the contract round-trip cleanly. Powers the C4
    # enrichment loop's `usage_count` bump (citation in output, not
    # retrieval). Excluded from verdict_hash inputs by design — drift in
    # marker emission must not flap the deterministic hash.
    cited_doc_ids: list[str] = Field(default_factory=list)
    # S20-C-CITATION-CONTRACT-01 — structured `[N]` → chunk_id marker map.
    # Mirrors ``cited_doc_ids`` (the doc_id list is just ``[m.doc_id for m
    # in citation_markers]``) but preserves the marker number, source URL,
    # and optional span excerpt for telemetry / renderer use. Empty by
    # default; legacy renderers continue to read ``validation_report.citations``
    # for footnote rendering and are unaffected.
    citation_markers: list[CitationMarker] = Field(default_factory=list)
    # S20-C-CONFIDENCE-PROMPT-01 — synth-step confidence self-rating, aggregated
    # across per-section emits as the document-level minimum (a verdict is
    # only as confident as its weakest section). Defaults to 0.0 so legacy
    # rows / hand-built results round-trip cleanly. Excluded from
    # ``verdict_hash._verdict_payload`` — confidence is a self-reported
    # calibration signal that drifts run-to-run; structural verdict shape
    # owns the digest. Powers the C5 confidence-gated enrichment loop
    # (separate ticket).
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    # S20-OBS-01 — names of post-processor sections that failed or returned
    # partial output (e.g. ``["market_landscape", "next_steps_with_falsifiers"]``).
    # Empty list signals a clean run. Surfaced so the renderer can show
    # a "this section failed to render" indicator instead of mis-reading
    # ``market_landscape: None`` as "no competitors found." Default empty
    # for back-compat — existing serialized rows round-trip cleanly.
    # Excluded from verdict_hash inputs (telemetry surface, not structural
    # verdict shape).
    degraded_sections: list[str] = Field(default_factory=list)
    # S20-OBS-01 — companion to ``degraded_sections`` mapping section name to
    # a short, machine-greppable reason string (e.g. ``"truncated_partial_recovery"``,
    # ``"validation_dropped_all"``, ``"model_timeout"``). Optional renderer /
    # telemetry surface; producers stamp via
    # ``orchestration.degraded.DegradedSectionTracker.apply_to``.
    degraded_reasons: dict[str, str] = Field(default_factory=dict)


class AskResult(BaseModel):
    """Result of a follow-up `ask()` query."""

    session_id: str
    answer: str
    citations: list[Citation]
