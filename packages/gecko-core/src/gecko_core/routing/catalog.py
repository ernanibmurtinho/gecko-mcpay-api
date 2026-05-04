"""Curated model catalog — single source of truth for per-agent model selection.

The JSON sidecar (``model_catalog.json``) is operational data, not external
research: it ships with the package and the loader validates it strictly so a
malformed catalog fails loud at import time, not at first agent reply.

Three lookup paths are exposed:

- ``lookup_model(task, tier)`` — pick the curated model for a task profile +
  user-facing tier preset. Backed by the 15 × 4 = 60-cell matrix transcribed
  from the GeckoVision Quick Reference.
- ``models_for_role(role)`` — return all four tier choices for an agent role
  (Pro debate or Advisor Panel). Used at orchestration build time.
- ``all_models()`` — full catalog for introspection surfaces.

Model bumps (e.g. DeepSeek V5 lands) are JSON edits, not code changes — but
the ``_NAME_TO_ID`` resolver expects display names to match catalog ``name``
fields, so renames stay in sync.
"""

from __future__ import annotations

import json
from enum import Enum
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

_CATALOG_PATH = Path(__file__).with_name("model_catalog.json")


class Tier(str, Enum):
    """User-facing tier preset (per Quick Reference table)."""

    quality = "quality"
    balanced = "balanced"
    budget = "budget"
    free = "free"


class ModelTier(str, Enum):
    """Catalog-side cost/quality tier for an individual model.

    Mirrors the ``tier`` field in ``model_catalog.json``. Some catalog values
    (``balanced``, ``budget_powerhouse``, ``ultra_budget``) are finer-grained
    than the four user-facing presets — they're kept here because they're
    informative for filtering and pricing analysis even though the user never
    sees them directly.
    """

    frontier = "frontier"
    premium = "premium"
    premium_value = "premium_value"
    balanced = "balanced"
    budget = "budget"
    budget_powerhouse = "budget_powerhouse"
    ultra_budget = "ultra_budget"
    free = "free"


class TaskProfile(str, Enum):
    """The 15 task categories from the Quick Reference matrix."""

    complex_coding = "complex_coding"
    simple_coding = "simple_coding"
    planning = "planning"
    file_navigation = "file_navigation"
    code_review = "code_review"
    general_reasoning = "general_reasoning"
    creative_writing = "creative_writing"
    summarization = "summarization"
    image_analysis = "image_analysis"
    audio_processing = "audio_processing"
    tool_calling = "tool_calling"
    data_analysis = "data_analysis"
    classification = "classification"
    long_context = "long_context"
    math_science = "math_science"


class AgentRole(str, Enum):
    """Roles staffed by the curated matrix.

    First five are the Pro-debate agents (already in production); the next
    five are the Sprint 4 Advisor Panel — wired here so the orchestration
    shell ships with model selection ready when the panel lands. The final
    five (LLM-hygiene Commit B) cover the orchestration-layer bypass sites
    that previously hardcoded ``gpt-4o-mini`` / ``orch.chat_model``.
    """

    # Pro debate
    analyst = "analyst"
    critic = "critic"
    architect = "architect"
    scoper = "scoper"
    judge = "judge"
    # Advisor Panel (Sprint 4)
    ceo = "ceo"
    cto = "cto"
    business_manager = "business_manager"
    product_manager = "product_manager"
    staff_manager = "staff_manager"
    # Orchestration bypass sites (LLM-hygiene Commit B). These previously
    # hardcoded ``gpt-4o-mini`` / ``orch.chat_model`` instead of resolving
    # through the catalog. Each maps to a TaskProfile via _ROLE_TO_TASK_MATRIX
    # below; the per-call-site default Tier is pinned at the call site.
    post_processor = "post_processor"  # transcript -> structured JSON
    refiner = "refiner"  # idea sharpening prompt
    judge_synth = "judge_synth"  # tweet corpus -> skill.md envelope
    research_basic = "research_basic"  # single-pass basic-tier research
    ask = "ask"  # follow-up Q&A grounded in session chunks


class ModelPricing(BaseModel):
    """USD per 1M tokens, separate input/output rails."""

    model_config = ConfigDict(extra="forbid")

    input: float
    output: float


class ModelEntry(BaseModel):
    """One row of the curated catalog.

    ``extra`` is permissive — future catalog bumps may add fields (e.g. eval
    snapshots, deprecation flags); we don't want a non-breaking metadata
    expansion to crash callers. Known fields are typed strictly so refactors
    catch real schema drift.
    """

    model_config = ConfigDict(extra="allow")

    id: str
    name: str
    provider: str
    pricing: ModelPricing
    context_window: int
    score: int
    score_per_dollar: float | None = None
    tier: ModelTier
    strengths: list[str] = Field(default_factory=list)
    weaknesses: list[str] = Field(default_factory=list)
    best_for: list[str] = Field(default_factory=list)
    swe_bench_verified: float | None = None
    modalities: list[str] = Field(default_factory=list)
    weekly_tokens: str | None = None
    openrouter_rank: dict[str, int] = Field(default_factory=dict)
    note: str | None = None


# ---------------------------------------------------------------------------
# Static matrices — curated from `docs/build-plan-sprint-4.md` §3 + the
# `GeckoVision Model Router - Quick Reference.md` table. Edit when a
# benchmark-driven re-baseline changes the per-task winner.
# ---------------------------------------------------------------------------

_ROLE_TO_TASK_MATRIX: dict[AgentRole, TaskProfile] = {
    # Pro debate
    AgentRole.analyst: TaskProfile.data_analysis,
    AgentRole.critic: TaskProfile.general_reasoning,
    AgentRole.architect: TaskProfile.complex_coding,
    AgentRole.scoper: TaskProfile.classification,
    AgentRole.judge: TaskProfile.planning,
    # Advisor Panel
    AgentRole.ceo: TaskProfile.planning,
    AgentRole.cto: TaskProfile.complex_coding,
    AgentRole.business_manager: TaskProfile.math_science,
    AgentRole.product_manager: TaskProfile.creative_writing,
    AgentRole.staff_manager: TaskProfile.classification,
    # Orchestration bypass sites (LLM-hygiene Commit B)
    AgentRole.post_processor: TaskProfile.classification,
    AgentRole.refiner: TaskProfile.creative_writing,
    AgentRole.judge_synth: TaskProfile.creative_writing,
    AgentRole.research_basic: TaskProfile.general_reasoning,
    AgentRole.ask: TaskProfile.summarization,
}


# Display-name → catalog id resolver. The Quick Reference uses display names
# (e.g. "Claude Opus 4.7") while the catalog keys models by id. Maintaining
# two parallel name conventions in source code is bug-prone, so we resolve
# once here and pin the (TaskProfile, Tier) → id matrix below.
_NAME_TO_ID: dict[str, str] = {
    "Claude Opus 4.7": "anthropic/claude-opus-4.7",
    "Claude Sonnet 4.6": "anthropic/claude-sonnet-4.6",
    "Claude Haiku 4.5": "anthropic/claude-haiku-4.5",
    "Kimi K2.6": "moonshotai/kimi-k2.6",
    "DeepSeek V3.2": "deepseek/deepseek-v3.2",
    "DeepSeek V4 Pro": "deepseek/deepseek-v4-pro",
    "DeepSeek V4 Flash": "deepseek/deepseek-v4-flash",
    "Gemini 3 Flash": "google/gemini-3-flash-preview",
    "Gemini 3.1 Pro": "google/gemini-3.1-pro-preview",
    "Gemini 2.5 Flash Lite": "google/gemini-2.5-flash-lite",
    "Gemini 2.5 Flash": "google/gemini-2.5-flash",
    "GPT-5.5": "openai/gpt-5.5",
    "GPT-5 Mini": "openai/gpt-5-mini",
    "GPT-4.1 Mini": "openai/gpt-4.1-mini",
    "GPT-4.1 Nano": "openai/gpt-4.1-nano",
    "Step 3.5 Flash": "stepfun/step-3.5-flash",
    "MiniMax M2.7": "minimax/minimax-m2.7",
    "Qwen3.6 Flash": "qwen/qwen3.6-flash",
    "Grok 4.1 Fast": "x-ai/grok-4.1-fast",
    # S8-CATALOG-01: dropped DeepSeek V4 Flash Max, Qwen3.5 Plus, Poolside
    # Laguna M.1, Nemotron 3 Super, Nemotron 3 Nano Omni (delisted from
    # OpenRouter 2026-04-29). Free-tier slots in the matrix below now point
    # at the cheapest live budget models.
}


def _id(display_name: str) -> str:
    """Resolve a Quick Reference display name to a catalog id. Raise if unknown."""
    if display_name not in _NAME_TO_ID:
        raise KeyError(
            f"display name {display_name!r} has no catalog id mapping; "
            "add it to _NAME_TO_ID in catalog.py"
        )
    return _NAME_TO_ID[display_name]


# 15 task profiles × 4 tiers = 60 cells. Source: GeckoVision Model Router -
# Quick Reference.md "Best Model Per Task" table.
_TASK_TIER_TO_MODEL_ID: dict[tuple[TaskProfile, Tier], str] = {
    (TaskProfile.complex_coding, Tier.quality): _id("Claude Opus 4.7"),
    (TaskProfile.complex_coding, Tier.balanced): _id("Kimi K2.6"),
    (TaskProfile.complex_coding, Tier.budget): _id("DeepSeek V4 Pro"),
    (TaskProfile.complex_coding, Tier.free): _id("DeepSeek V4 Flash"),
    (TaskProfile.simple_coding, Tier.quality): _id("Claude Sonnet 4.6"),
    (TaskProfile.simple_coding, Tier.balanced): _id("DeepSeek V3.2"),
    (TaskProfile.simple_coding, Tier.budget): _id("Step 3.5 Flash"),
    (TaskProfile.simple_coding, Tier.free): _id("GPT-4.1 Nano"),
    (TaskProfile.planning, Tier.quality): _id("Claude Opus 4.7"),
    (TaskProfile.planning, Tier.balanced): _id("Kimi K2.6"),
    (TaskProfile.planning, Tier.budget): _id("Gemini 3 Flash"),
    (TaskProfile.planning, Tier.free): _id("GPT-4.1 Nano"),
    (TaskProfile.file_navigation, Tier.quality): _id("Claude Haiku 4.5"),
    (TaskProfile.file_navigation, Tier.balanced): _id("GPT-4.1 Nano"),
    (TaskProfile.file_navigation, Tier.budget): _id("DeepSeek V4 Flash"),
    (TaskProfile.file_navigation, Tier.free): _id("GPT-4.1 Nano"),
    (TaskProfile.code_review, Tier.quality): _id("Claude Sonnet 4.6"),
    (TaskProfile.code_review, Tier.balanced): _id("DeepSeek V4 Pro"),
    (TaskProfile.code_review, Tier.budget): _id("Kimi K2.6"),
    (TaskProfile.code_review, Tier.free): _id("DeepSeek V4 Flash"),
    (TaskProfile.general_reasoning, Tier.quality): _id("GPT-5.5"),
    (TaskProfile.general_reasoning, Tier.balanced): _id("Kimi K2.6"),
    (TaskProfile.general_reasoning, Tier.budget): _id("Grok 4.1 Fast"),
    (TaskProfile.general_reasoning, Tier.free): _id("GPT-4.1 Nano"),
    (TaskProfile.creative_writing, Tier.quality): _id("Claude Sonnet 4.6"),
    (TaskProfile.creative_writing, Tier.balanced): _id("Kimi K2.6"),
    (TaskProfile.creative_writing, Tier.budget): _id("Qwen3.6 Flash"),
    (TaskProfile.creative_writing, Tier.free): _id("GPT-4.1 Nano"),
    (TaskProfile.summarization, Tier.quality): _id("Gemini 3 Flash"),
    (TaskProfile.summarization, Tier.balanced): _id("DeepSeek V4 Flash"),
    (TaskProfile.summarization, Tier.budget): _id("GPT-4.1 Nano"),
    (TaskProfile.summarization, Tier.free): _id("GPT-4.1 Nano"),
    (TaskProfile.image_analysis, Tier.quality): _id("Claude Sonnet 4.6"),
    (TaskProfile.image_analysis, Tier.balanced): _id("Gemini 2.5 Flash Lite"),
    (TaskProfile.image_analysis, Tier.budget): _id("GPT-4.1 Mini"),
    (TaskProfile.image_analysis, Tier.free): _id("Gemini 2.5 Flash Lite"),
    (TaskProfile.audio_processing, Tier.quality): _id("Gemini 3.1 Pro"),
    (TaskProfile.audio_processing, Tier.balanced): _id("Gemini 3 Flash"),
    (TaskProfile.audio_processing, Tier.budget): _id("Gemini 2.5 Flash"),
    (TaskProfile.audio_processing, Tier.free): _id("Gemini 2.5 Flash Lite"),
    (TaskProfile.tool_calling, Tier.quality): _id("Claude Sonnet 4.6"),
    (TaskProfile.tool_calling, Tier.balanced): _id("Kimi K2.6"),
    (TaskProfile.tool_calling, Tier.budget): _id("Gemini 3 Flash"),
    (TaskProfile.tool_calling, Tier.free): _id("GPT-4.1 Nano"),
    (TaskProfile.data_analysis, Tier.quality): _id("Claude Opus 4.7"),
    (TaskProfile.data_analysis, Tier.balanced): _id("DeepSeek V3.2"),
    (TaskProfile.data_analysis, Tier.budget): _id("MiniMax M2.7"),
    (TaskProfile.data_analysis, Tier.free): _id("GPT-4.1 Nano"),
    (TaskProfile.classification, Tier.quality): _id("GPT-4.1 Mini"),
    (TaskProfile.classification, Tier.balanced): _id("GPT-4.1 Nano"),
    (TaskProfile.classification, Tier.budget): _id("DeepSeek V4 Flash"),
    (TaskProfile.classification, Tier.free): _id("GPT-4.1 Nano"),
    (TaskProfile.long_context, Tier.quality): _id("Gemini 3.1 Pro"),
    (TaskProfile.long_context, Tier.balanced): _id("Grok 4.1 Fast"),
    (TaskProfile.long_context, Tier.budget): _id("DeepSeek V4 Flash"),
    (TaskProfile.long_context, Tier.free): _id("Gemini 2.5 Flash Lite"),
    (TaskProfile.math_science, Tier.quality): _id("GPT-5.5"),
    (TaskProfile.math_science, Tier.balanced): _id("DeepSeek V4 Pro"),
    (TaskProfile.math_science, Tier.budget): _id("Step 3.5 Flash"),
    (TaskProfile.math_science, Tier.free): _id("GPT-4.1 Nano"),
}


class CatalogError(Exception):
    """Raised when the catalog file is missing, malformed, or inconsistent."""


@lru_cache(maxsize=1)
def load_catalog() -> dict[str, ModelEntry]:
    """Load + validate ``model_catalog.json`` once per process.

    Returns a mapping of model id (e.g. ``anthropic/claude-opus-4.7``) to a
    parsed ``ModelEntry``. Raises ``CatalogError`` on any schema violation —
    bad operational data must fail loud at boot, not at first agent reply.
    """
    try:
        raw = json.loads(_CATALOG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CatalogError(f"model_catalog.json missing at {_CATALOG_PATH}") from exc
    except json.JSONDecodeError as exc:
        raise CatalogError(f"model_catalog.json is not valid JSON: {exc}") from exc

    models = raw.get("models")
    if not isinstance(models, dict):
        raise CatalogError("model_catalog.json missing top-level 'models' object")

    out: dict[str, ModelEntry] = {}
    for key, payload in models.items():
        try:
            entry = ModelEntry.model_validate(payload)
        except ValidationError as exc:
            raise CatalogError(f"catalog entry {key!r} failed validation: {exc}") from exc
        out[entry.id] = entry
    if not out:
        raise CatalogError("model_catalog.json contained zero models")
    return out


def lookup_model(task: TaskProfile, tier: Tier) -> ModelEntry:
    """Pick the curated model for ``(task, tier)``.

    Raises ``CatalogError`` if the matrix references an id not present in the
    catalog (a sign that catalog and matrix have drifted apart).
    """
    try:
        model_id = _TASK_TIER_TO_MODEL_ID[(task, tier)]
    except KeyError as exc:
        raise CatalogError(f"no curated model for task={task.value} tier={tier.value}") from exc
    catalog = load_catalog()
    if model_id not in catalog:
        raise CatalogError(f"matrix references model id {model_id!r} but it's not in the catalog")
    return catalog[model_id]


def models_for_role(role: AgentRole) -> dict[Tier, ModelEntry]:
    """Return ``{tier: ModelEntry}`` for an agent role's primary task profile.

    Used at orchestration build time to populate per-role model selection.
    """
    task = _ROLE_TO_TASK_MATRIX[role]
    return {tier: lookup_model(task, tier) for tier in Tier}


def model_id_for_role(role: AgentRole, tier: Tier) -> str:
    """Sibling to ``models_for_role`` that returns just the catalog id.

    Most call sites pass the model id straight into ``client.chat.completions
    .create(model=...)`` and don't need the rest of the ``ModelEntry``. Use
    this when you've already settled on a tier; use ``resolve_model_for_router``
    when ``LLM_ROUTER`` may require an OpenAI-only fallback.
    """
    return lookup_model(_ROLE_TO_TASK_MATRIX[role], tier).id


# Providers reachable directly via OpenAI's API. Other providers must go
# through OpenRouter (or ClawRouter). When ``LLM_ROUTER=openai`` and the
# catalog matrix would pick a non-OpenAI model, we substitute the OpenAI
# fallback for that tier rather than 404 at the API.
#
# This mirrors the legacy ``_OPENAI_FALLBACK_BY_TIER`` that lived in
# ``orchestration/pro/router.py`` — lifted up here so all five orchestration
# bypass sites (post_processor, refiner, judge_synth, research_basic, ask)
# share one fallback table with the AG2 debate.
_OPENAI_FALLBACK_BY_TIER: dict[Tier, str] = {
    Tier.quality: "openai/gpt-5.5",
    Tier.balanced: "openai/gpt-5-mini",
    Tier.budget: "openai/gpt-4.1-nano",
    Tier.free: "openai/gpt-4.1-nano",  # no free OpenAI model — degrade to nano
}


def resolve_model_for_router(role: AgentRole, tier: Tier, router: str) -> str:
    """Return the model id for ``(role, tier)`` adjusted for ``router``.

    When ``router == "openai"`` and the catalog pick is non-OpenAI (Anthropic,
    Moonshot, Google, etc.), substitute the OpenAI fallback for ``tier``
    rather than 404 at api.openai.com. ``openrouter`` and ``clawrouter``
    pass the catalog id through unchanged.
    """
    catalog_id = model_id_for_role(role, tier)
    router_norm = (router or "").strip().lower()
    if router_norm != "openai":
        return catalog_id
    catalog = load_catalog()
    entry = catalog.get(catalog_id)
    if entry is not None and entry.provider == "openai":
        return catalog_id
    # Catalog pick is non-OpenAI; fall back to the OpenAI-only ladder.
    return _OPENAI_FALLBACK_BY_TIER[tier]


def all_models() -> list[ModelEntry]:
    """Return the full catalog as a list, sorted by id for deterministic output."""
    catalog = load_catalog()
    return [catalog[k] for k in sorted(catalog.keys())]


def task_for_role(role: AgentRole) -> TaskProfile:
    """Inverse of the role → task matrix; useful for callers that already
    resolve their own tier and only need the task profile."""
    return _ROLE_TO_TASK_MATRIX[role]


def filter_by_provider(
    catalog: dict[str, ModelEntry], providers: set[str]
) -> dict[str, ModelEntry]:
    """Return the subset of ``catalog`` whose ``provider`` is in ``providers``.

    Used by the router fallback path: when ``LLM_ROUTER=openai`` we filter to
    OpenAI-provider entries before lookup, since OpenAI direct can't reach
    Anthropic / Google / Moonshot models.
    """
    return {k: v for k, v in catalog.items() if v.provider in providers}


__all__ = [
    "AgentRole",
    "CatalogError",
    "ModelEntry",
    "ModelPricing",
    "ModelTier",
    "TaskProfile",
    "Tier",
    "all_models",
    "filter_by_provider",
    "load_catalog",
    "lookup_model",
    "model_id_for_role",
    "models_for_role",
    "resolve_model_for_router",
    "task_for_role",
]
