"""Idea-refinement pipeline (V1.5 — `bb refine <hash>`).

Reads a saved verdict, calls the v5.5 ``refine_idea`` prompt with
``response_format=json_object``, validates against
``RefinedIdea``, and persists the refinement back to the
``ResearchResult.refinement`` slot.

CONTRACT WITH ai-ml-engineer (the prompt owner):
  - Top-level key in ``_default_prompts_v5_5.json``: ``refine_idea``.
  - Output shape: ``RefinedIdea`` (declared in ``gecko_core.models``).

If the prompt key is not yet present in the bundle, the synth call
raises a ``RefineError`` with a clear "ai-ml-engineer prompt pending"
message rather than calling out to OpenAI with an empty system prompt.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from gecko_core.models import RefinedIdea, ResearchResult
from gecko_core.orchestration.pro.post_processors import _build_client
from gecko_core.orchestration.pro.prompts import _BUNDLED_VERSIONS
from gecko_core.orchestration.settings import (
    get_orchestration_settings,
    resolve_llm_config,
)
from gecko_core.routing.catalog import AgentRole, Tier, resolve_model_for_router

logger = logging.getLogger(__name__)

# LLM-hygiene Commit B — model resolves through the catalog at call time,
# adjusted for the active LLM_ROUTER. Refiner sits at the balanced tier of
# the creative_writing task profile (Kimi K2.6 by default).
_REFINE_TIER = Tier.balanced
_REFINE_TEMPERATURE = 0.2


def _resolve_refine_model() -> str:
    cfg = resolve_llm_config(settings=get_orchestration_settings())
    router_name = cfg.source.split(":", 1)[1] if cfg.source.startswith("router:") else "openai"
    return resolve_model_for_router(AgentRole.refiner, _REFINE_TIER, router_name)


class RefineError(Exception):
    """Refine pipeline could not complete (prompt missing, LLM error, etc.)."""


def _load_refine_prompt() -> str:
    """Load the ``refine_idea`` prompt from v5.5. Raises when absent."""
    path = _BUNDLED_VERSIONS["v5.5"]
    data = json.loads(path.read_text(encoding="utf-8"))
    val = data.get("refine_idea")
    if not isinstance(val, str) or not val.strip():
        raise RefineError(
            "refine_idea prompt is not yet available in "
            "_default_prompts_v5_5.json — coordinate with ai-ml-engineer."
        )
    return val.strip()


def _build_user_prompt(*, idea: str, result: ResearchResult, today: str) -> str:
    """Render the user message for the refine call.

    Carries the idea, the verdict envelope, the surviving-dissent block,
    and the existing falsifiers so the LLM has every artefact it needs
    to produce a sharper / narrower / pivoted version.
    """
    surviving = ""
    if result.surviving_dissent is not None and result.surviving_dissent.dissents:
        lines = []
        for d in result.surviving_dissent.dissents:
            lines.append(f"- {d.voice} on {d.on_topic}: {d.verbatim!r}")
        surviving = "\n".join(lines)

    falsifiers = ""
    if result.next_steps_with_falsifiers is not None:
        lines = []
        for s in result.next_steps_with_falsifiers.steps:
            lines.append(
                f"- {s.action} (falsifier: {s.falsifier.what_would_disprove_this} — "
                f"by {s.falsifier.by_when})"
            )
        falsifiers = "\n".join(lines)

    return (
        f"Today: {today}\n\n"
        f"Original idea: {idea}\n\n"
        f"Verdict: {result.verdict.value}\n"
        f"Gap classification: {result.validation_report.gap_classification}\n\n"
        f"Surviving dissent:\n{surviving or '(none)'}\n\n"
        f"Existing falsifiers / next steps:\n{falsifiers or '(none)'}\n\n"
        "Respond with the JSON object specified in the system message."
    )


async def refine_idea(
    idea: str,
    result: ResearchResult,
    *,
    today: str | None = None,
) -> RefinedIdea:
    """Run the refine_idea prompt and return a validated RefinedIdea.

    Raises ``RefineError`` on any failure (prompt missing, network,
    validation) so the CLI can surface a clean message.
    """
    system = _load_refine_prompt()
    user = _build_user_prompt(idea=idea, result=result, today=today or date.today().isoformat())

    client = _build_client()
    try:
        resp = await client.chat.completions.create(
            model=_resolve_refine_model(),
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            response_format={"type": "json_object"},
            temperature=_REFINE_TEMPERATURE,
        )
    except Exception as exc:  # OpenAI / network
        raise RefineError(f"refine_idea call failed: {exc}") from exc
    finally:
        await client.close()

    content = resp.choices[0].message.content
    if not content:
        raise RefineError("refine_idea returned empty content")
    try:
        raw = json.loads(content)
    except json.JSONDecodeError as exc:
        raise RefineError(f"refine_idea returned invalid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise RefineError("refine_idea JSON was not an object")
    try:
        return RefinedIdea.model_validate(raw)
    except ValidationError as exc:
        raise RefineError(f"refine_idea validation failed: {exc}") from exc


async def persist_refinement(
    *,
    result: ResearchResult,
    refinement: RefinedIdea,
    store: Any | None = None,
) -> ResearchResult:
    """Stamp ``refinement`` onto the result and write it back to Supabase.

    Returns the updated ``ResearchResult``. Best-effort persistence:
    persistence failures log a warning but the in-memory result is
    returned populated so the CLI can still render the refinement.
    """
    updated = result.model_copy(update={"refinement": refinement})
    try:
        from gecko_core.persistence import update_result_payload

        await update_result_payload(
            UUID(updated.session_id),
            updated.model_dump(mode="json"),
            store=store,
        )
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("refinement persistence failed: %s", exc)
    return updated


__all__ = [
    "RefineError",
    "persist_refinement",
    "refine_idea",
]
