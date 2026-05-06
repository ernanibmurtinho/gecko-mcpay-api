"""v5.5 demo post-processors.

Five small JSON-in/JSON-out calls that re-read the AG2 5-agent transcript
and produce structured readouts for the demo output (per-voice readout,
4-line transcript summary, market landscape, surviving dissent,
next steps with falsifiers). Run in parallel via ``asyncio.gather``.

Failure of any single section degrades that section to ``None``; renderer
gracefully skips. Only the cross-section coherence pass (next_steps whose
``surfaced_by_voice`` references a silent voice) prunes data — never
synthesises it.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from typing import Any, cast

from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError

from gecko_core.llm_helpers import build_response_format
from gecko_core.models import (
    MarketLandscape,
    NextStepsWithFalsifiers,
    PerVoiceReadout,
    SurvivingDissent,
)
from gecko_core.orchestration.pro.prompts import load_post_processors
from gecko_core.orchestration.pro.transcript import DebateTranscript
from gecko_core.orchestration.settings import (
    get_orchestration_settings,
    resolve_llm_config,
)
from gecko_core.routing.catalog import AgentRole, Tier, resolve_model_for_router

logger = logging.getLogger(__name__)

# Post-processors run on a small fast model; the in-debate matrix is
# decoupled from this surface by design (a v5.6 model swap on the debate
# voices should not perturb the readout shape). Model resolves through the
# catalog (LLM-hygiene Commit B) — pinned to the budget tier of the
# classification task profile by default.
_POST_PROCESSOR_TIER = Tier.budget
_POST_PROCESSOR_TEMPERATURE = 0.2


def _resolve_post_processor_model() -> tuple[str, str]:
    """Resolve the post-processor (model_id, router) through the catalog.

    Reads ``LLM_ROUTER`` from settings (router-driven path) so the OpenAI-only
    fallback fires when the catalog pick is non-OpenAI. The router name is
    returned alongside the model id so the call site can decide whether to
    promote ``response_format`` to strict json_schema (Commit D).
    """
    cfg = resolve_llm_config(settings=get_orchestration_settings())
    # cfg.source is "router:<name>" or "legacy"; legacy plane = openai-shaped
    # endpoint, so treat it as openai for fallback purposes.
    router_name = cfg.source.split(":", 1)[1] if cfg.source.startswith("router:") else "openai"
    model_id = resolve_model_for_router(AgentRole.post_processor, _POST_PROCESSOR_TIER, router_name)
    return model_id, router_name


# Each section ships its own banner to keep the user prompt small but
# disambiguate the model's task when it sees only the transcript text.
_USER_TEMPLATE = (
    "Today: {today}\n\n"
    "Idea: {idea}\n\n"
    "Retrieved knowledge-base context:\n{rag_context}\n\n"
    "Debate transcript (5 voices in canonical order):\n{transcript}\n\n"
    "Respond with the JSON object specified in the system message."
)


def _format_transcript(transcript: DebateTranscript) -> str:
    parts = []
    for turn in transcript.turns:
        parts.append(f"### {turn.agent}\n{turn.content}")
    return "\n\n".join(parts)


async def _call_json(
    client: AsyncOpenAI,
    *,
    system: str,
    user: str,
    model_cls: type[BaseModel] | None = None,
) -> dict[str, Any]:
    # LLM-hygiene Commit C: ``seed=42`` is best-effort determinism.
    # OpenRouter forwards seed per-provider; not all providers honor it
    # (Anthropic and Google silently ignore; OpenAI honours it).
    # LLM-hygiene Commit D: when the (model, router) supports it, opt into
    # OpenAI Structured Outputs strict mode keyed off ``model_cls``.
    #
    # 2026-05-05 — streamed via gecko_core.orchestration.llm_client so
    # the connection stays open against slow OpenRouter providers and a
    # ``finish_reason=length`` truncation surfaces as
    # ``LLMTruncationError`` (caught by ``_run_section``).
    from gecko_core.orchestration.llm_client import (
        LLMStalledError,
        LLMTruncationError,
        stream_chat_completion,
    )

    model_id, router_name = _resolve_post_processor_model()
    response_format = cast(Any, build_response_format(model_cls, model_id, router_name))
    try:
        completion = await stream_chat_completion(
            client,
            model=model_id,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=_POST_PROCESSOR_TEMPERATURE,
            max_tokens=get_orchestration_settings().max_tokens_post_processor,
            response_format=response_format,
        )
    except LLMTruncationError as exc:
        # Truncation is a structural failure — partial JSON cannot be
        # validated. Surface the typed message to the section handler so
        # the operator sees gen_id / provider / model rather than a
        # downstream pydantic ValidationError.
        raise ValueError(f"post-processor truncated: {exc}") from exc
    except LLMStalledError as exc:
        raise ValueError(f"post-processor stalled: {exc}") from exc

    content = completion.content
    if not content:
        raise ValueError(
            f"post-processor returned empty content "
            f"[model={completion.model}, provider={completion.provider}, "
            f"gen_id={completion.gen_id}]"
        )
    parsed = json.loads(content)
    if not isinstance(parsed, dict):
        raise ValueError("post-processor JSON was not an object")
    return cast(dict[str, Any], parsed)


async def _run_section(
    client: AsyncOpenAI,
    *,
    system: str,
    user: str,
    model_cls: type[BaseModel],
    section: str,
) -> BaseModel | None:
    try:
        raw = await _call_json(client, system=system, user=user, model_cls=model_cls)
        return model_cls.model_validate(raw)
    except ValidationError as exc:
        logger.warning("post-processor %s validation failed: %s", section, exc)
        return None
    except Exception as exc:  # JSON, OpenAI, network — degrade gracefully
        logger.warning("post-processor %s call failed: %s", section, exc)
        return None


def _coherence_drop_steps(
    per_voice: PerVoiceReadout | None,
    next_steps: NextStepsWithFalsifiers | None,
) -> tuple[NextStepsWithFalsifiers | None, int]:
    """Drop next_steps whose surfaced_by_voice is silent or missing."""
    if next_steps is None:
        return None, 0
    if per_voice is None:
        return next_steps, 0
    engaged = {v.name for v in per_voice.voices if v.status != "silent"}
    kept = [s for s in next_steps.steps if s.surfaced_by_voice in engaged]
    dropped = len(next_steps.steps) - len(kept)
    if dropped == 0:
        return next_steps, 0
    return NextStepsWithFalsifiers(steps=kept), dropped


def _build_client() -> AsyncOpenAI:
    from gecko_core.orchestration.llm_client import build_async_client

    orch = get_orchestration_settings()
    return build_async_client(resolve_llm_config(settings=orch))


_CLASSIFICATION_VALID_IDEA = frozenset({"greenfield", "iterative", "unclear"})
_CLASSIFICATION_VALID_FOUNDER = frozenset({"high", "moderate", "unclear"})


async def run_post_processors(
    transcript: DebateTranscript,
    rag_context: str,
    idea: str,
    *,
    client: AsyncOpenAI | None = None,
) -> tuple[
    PerVoiceReadout | None,
    str | None,
    MarketLandscape | None,
    SurvivingDissent | None,
    NextStepsWithFalsifiers | None,
    dict[str, Any],
]:
    """Run all 5 v5.5 post-processors in parallel; return the structured readouts.

    Returns ``(per_voice, transcript_summary, market_landscape,
    surviving_dissent, next_steps_with_falsifiers, _meta)``. Any single
    section returns ``None`` if its call or validation failed; renderer
    handles ``None`` per the design spec.
    """
    import asyncio

    prompts = load_post_processors()
    transcript_text = _format_transcript(transcript)
    user = _USER_TEMPLATE.format(
        today=date.today().isoformat(),
        idea=idea,
        rag_context=rag_context,
        transcript=transcript_text,
    )

    own_client = client is None
    c = client if client is not None else _build_client()

    try:
        per_voice_task = _run_section(
            c,
            system=prompts["per_voice_extraction"],
            user=user,
            model_cls=PerVoiceReadout,
            section="per_voice_extraction",
        )

        async def _summary_task() -> str | None:
            try:
                raw = await _call_json(c, system=prompts["transcript_summary"], user=user)
                summary = raw.get("summary")
                if isinstance(summary, str) and summary.strip():
                    return summary.strip()
                logger.warning("transcript_summary missing 'summary' string")
                return None
            except Exception as exc:
                logger.warning("post-processor transcript_summary failed: %s", exc)
                return None

        landscape_task = _run_section(
            c,
            system=prompts["market_landscape"],
            user=user,
            model_cls=MarketLandscape,
            section="market_landscape",
        )
        dissent_task = _run_section(
            c,
            system=prompts["surviving_dissent"],
            user=user,
            model_cls=SurvivingDissent,
            section="surviving_dissent",
        )
        steps_task = _run_section(
            c,
            system=prompts["next_steps_with_falsifiers"],
            user=user,
            model_cls=NextStepsWithFalsifiers,
            section="next_steps_with_falsifiers",
        )

        async def _classification_task() -> tuple[str | None, str | None]:
            # S21-CALIBRATION-FOUNDER-POSTURE-01 — JSON-shaped fallback when
            # the judge sentinel drops. Returns ``(idea_classification,
            # founder_posture)`` with each independently None on failure
            # or invalid label.
            try:
                raw = await _call_json(c, system=prompts["classification_extraction"], user=user)
                ideacls = raw.get("idea_classification")
                founder = raw.get("founder_posture")
                ideacls_norm: str | None = None
                founder_norm: str | None = None
                if isinstance(ideacls, str) and ideacls.lower() in _CLASSIFICATION_VALID_IDEA:
                    ideacls_norm = ideacls.lower()
                if isinstance(founder, str) and founder.lower() in _CLASSIFICATION_VALID_FOUNDER:
                    founder_norm = founder.lower()
                return (ideacls_norm, founder_norm)
            except Exception as exc:
                logger.warning("post-processor classification_extraction failed: %s", exc)
                return (None, None)

        results = await asyncio.gather(
            per_voice_task,
            _summary_task(),
            landscape_task,
            dissent_task,
            steps_task,
            _classification_task(),
        )
    finally:
        if own_client:
            await c.close()

    per_voice = cast("PerVoiceReadout | None", results[0])
    summary = results[1]
    market_landscape = cast("MarketLandscape | None", results[2])
    surviving_dissent = cast("SurvivingDissent | None", results[3])
    next_steps = cast("NextStepsWithFalsifiers | None", results[4])
    classification_pair = results[5]
    assert isinstance(classification_pair, tuple)  # narrow for mypy
    idea_classification_pp, founder_posture_pp = classification_pair

    next_steps, dropped = _coherence_drop_steps(per_voice, next_steps)
    meta: dict[str, Any] = {
        "_dropped_step_count": dropped,
        # S21-CALIBRATION-FOUNDER-POSTURE-01 — surfaced in `_meta` rather
        # than as positional return values so the existing tuple shape
        # (5 readouts + meta) stays stable. Workflow consumes both keys
        # to fill the ResearchResult fields when the judge sentinel
        # extraction returned None.
        "idea_classification": idea_classification_pp,
        "founder_posture": founder_posture_pp,
    }

    return per_voice, summary, market_landscape, surviving_dissent, next_steps, meta


__all__ = ["run_post_processors"]
