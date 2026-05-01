"""Advisor Panel orchestration shell (Sprint 4 Track A).

Five named leadership voices (CEO, CTO, business_manager, product_manager,
staff_manager) read a session's Pro debate output + scaffold artifacts +
flywheel + V1 source signal and produce opinionated, persona-anchored
advice.

Voices are INDEPENDENT, parallel ``asyncio.gather`` calls — NOT an AG2
GroupChat. The panel's value comes from voices DISAGREEING (CEO timing vs
CTO refactor timing is the canonical example); a conversational chat would
let one persona dominate. Per-voice model selection comes from
``routing.catalog.lookup_model``: at ``Tier.balanced`` the panel uses
five different models tuned for each voice's primary task profile.

Public surface:

- ``generate_panel(session_id, ...)`` — full 5-voice panel.
- ``generate_voice(session_id, voice, ...)`` — single voice (cheaper).
- ``run_pulse(panel, previous_panel)`` — delta detection vs prior run.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from openai import AsyncOpenAI

from gecko_core.orchestration.advisor.agents import run_voice
from gecko_core.orchestration.advisor.context import (
    AdvisorContext,
    load_context,
    render_context_block,
)
from gecko_core.orchestration.advisor.models import (
    PANEL_VOICE_ORDER,
    AdvisorError,
    AdvisorPanel,
    AdvisorSessionNotFoundError,
    AdvisorVoice,
    PulseDelta,
    PulsePanel,
    PulseResult,
)
from gecko_core.orchestration.advisor.prompts import REQUIRED_VOICES, load_prompts
from gecko_core.orchestration.advisor.pulse_engine import run_pulse_v14
from gecko_core.routing.catalog import AgentRole, Tier
from gecko_core.sessions.store import SessionStore

logger = logging.getLogger(__name__)


def _voice_role(voice: str | AgentRole) -> AgentRole:
    """Normalize a voice identifier (str or enum) to an AgentRole.

    Accepts both ``"ceo"`` / ``"cto"`` / ``"business_manager"`` / etc. and
    short aliases ``"bm"`` / ``"pm"`` / ``"sm"`` (used by MCP/CLI).
    """
    if isinstance(voice, AgentRole):
        return voice
    key = voice.strip().lower()
    aliases = {
        "bm": AgentRole.business_manager,
        "pm": AgentRole.product_manager,
        "sm": AgentRole.staff_manager,
    }
    if key in aliases:
        return aliases[key]
    try:
        role = AgentRole(key)
    except ValueError as exc:
        raise ValueError(
            f"unknown advisor voice {voice!r}; expected one of {list(REQUIRED_VOICES)} "
            "(or aliases bm/pm/sm)"
        ) from exc
    if role not in PANEL_VOICE_ORDER:
        raise ValueError(f"role {role.value!r} is not an Advisor Panel voice")
    return role


def _build_default_client() -> AsyncOpenAI:
    """Build an AsyncOpenAI client via the unified resolver (S8-CONFIG-01).

    Honors ``LLM_ROUTER`` (the same plane Pro debate uses) so setting
    ``LLM_ROUTER=openrouter`` routes the advisor through OpenRouter without
    a ``GECKO_LLM_ENDPOINT`` override. Legacy env still wins if both are
    set, with an INFO-level deprecation log.
    """
    from gecko_core.orchestration.settings import resolve_llm_config

    cfg = resolve_llm_config()
    kwargs: dict[str, Any] = {"api_key": cfg.api_key, "base_url": cfg.base_url}
    if cfg.extra_headers:
        kwargs["default_headers"] = dict(cfg.extra_headers)
    return AsyncOpenAI(**kwargs)


async def generate_voice(
    session_id: UUID | str,
    voice: str | AgentRole,
    *,
    tier_preset: Tier = Tier.balanced,
    store: SessionStore | None = None,
    output_dir: Path | None = None,
    v1_source_signal: str = "",
    openai_client: AsyncOpenAI | None = None,
    context: AdvisorContext | None = None,
    journal: bool = True,
) -> AdvisorVoice:
    """Run a single advisor voice. ~5x cheaper than a full panel.

    Args:
        session_id: UUID of an existing session (any verdict OK — kills
            still get advice on whether to pivot).
        voice: One of {ceo, cto, business_manager, product_manager,
            staff_manager} or aliases {bm, pm, sm}.
        tier_preset: Cost/quality preset; selects the per-voice model
            from the curated catalog. Default: balanced.
        store: SessionStore. Defaults to env-configured.
        output_dir: Workspace root for scaffold lookup. None skips it.
        v1_source_signal: Pre-rendered V1 block (caller owns dispatch).
        openai_client: AsyncOpenAI client. Defaults to env-configured.
        context: Pre-loaded context (used by ``generate_panel`` to share
            one load across all 5 voices). When None, this function
            loads context itself.

    Raises:
        AdvisorSessionNotFoundError: session row missing (only when this
            function loads context itself).
    """
    role = _voice_role(voice)

    if store is None:
        store = SessionStore.from_env()

    if context is None:
        context = await load_context(
            session_id,
            store=store,
            output_dir=output_dir,
            v1_source_signal=v1_source_signal,
        )

    if openai_client is None:
        openai_client = _build_default_client()

    prompts = load_prompts()
    user_prompt = render_context_block(context)

    # S6-MINE-02 — prepend a "PRIOR DECISIONS" block to the user prompt
    # when the project has prior verdicts/shipments on file AND the env
    # gate is on. Best-effort; never raises.
    try:
        from gecko_core.memory.priors import build_priors_block

        priors = await build_priors_block(context.project_id)
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("generate_voice: priors load failed: %s", exc)
        priors = ""
    if priors:
        user_prompt = f"{priors}\n\n{user_prompt}"

    voice_result = await run_voice(
        role=role,
        system_prompt=prompts[role.value],
        user_prompt=user_prompt,
        tier_preset=tier_preset,
        client=openai_client,
    )

    # Auto-journal advisor_voiced (S5-MEM-04). Best-effort.
    try:
        from gecko_core.memory.auto_journal import journal_voice

        sid = session_id if isinstance(session_id, UUID) else UUID(str(session_id))
        record = await store.get(sid)
        project_id = record.project_id if record is not None else None
        await journal_voice(
            session_id=sid,
            project_id=project_id,
            role=voice_result.role.value,
            closing_line=voice_result.closing_line,
            model_used=voice_result.model_used,
            cost_usd=float(voice_result.cost_usd or 0.0),
            journal=journal,
        )
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("auto-journal advisor_voiced failed: %s", exc)

    return voice_result


async def generate_panel(
    session_id: UUID | str,
    *,
    tier_preset: Tier = Tier.balanced,
    store: SessionStore | None = None,
    output_dir: Path | None = None,
    v1_source_signal: str = "",
    openai_client: AsyncOpenAI | None = None,
    journal: bool = True,
) -> AdvisorPanel:
    """Run all 5 advisor voices in parallel and return the AdvisorPanel.

    Voices fan out via ``asyncio.gather`` — total wall-time is roughly
    the slowest single voice (~3x faster than sequential). Failures in one
    voice surface as an AdvisorVoice with empty ``output_md``; the panel
    still returns the other four.

    Voices are returned in ``PANEL_VOICE_ORDER`` (CEO first, staff_manager
    last) regardless of which finished first — staff_manager's prompt
    expects to see the others' outputs in CONTEXT, but in v1 each voice
    sees the same input context. Sprint 5 may chain staff_manager after
    the other four; for now the panel value is the parallel speed.
    """
    sid = session_id if isinstance(session_id, UUID) else UUID(str(session_id))

    if store is None:
        store = SessionStore.from_env()

    context = await load_context(
        sid,
        store=store,
        output_dir=output_dir,
        v1_source_signal=v1_source_signal,
    )

    if openai_client is None:
        openai_client = _build_default_client()

    prompts = load_prompts()
    user_prompt = render_context_block(context)

    # S6-MINE-02 — same priors prefix used by `generate_voice`. Computed
    # once and shared across all five panel voices.
    try:
        from gecko_core.memory.priors import build_priors_block

        priors = await build_priors_block(context.project_id)
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("generate_panel: priors load failed: %s", exc)
        priors = ""
    if priors:
        user_prompt = f"{priors}\n\n{user_prompt}"

    async def _one(role: AgentRole) -> AdvisorVoice:
        return await run_voice(
            role=role,
            system_prompt=prompts[role.value],
            user_prompt=user_prompt,
            tier_preset=tier_preset,
            client=openai_client,
        )

    voices = await asyncio.gather(*(_one(r) for r in PANEL_VOICE_ORDER))
    total_cost = sum(v.cost_usd for v in voices if v.cost_usd is not None)
    # S9-ADVISOR-01: panel-level rollup for monitoring quality drift.
    voices_no_closing_line = sum(1 for v in voices if v.error_kind == "no_closing_line")

    panel = AdvisorPanel(
        session_id=str(sid),
        voices=list(voices),
        total_cost_usd=float(total_cost),
        generated_at=datetime.now().astimezone(),
        voices_no_closing_line=voices_no_closing_line,
    )

    # Auto-journal plan_advised (S5-MEM-04). Best-effort.
    try:
        from gecko_core.memory.auto_journal import journal_plan

        record = await store.get(sid)
        project_id = record.project_id if record is not None else None
        await journal_plan(
            session_id=sid,
            project_id=project_id,
            voices=[
                {
                    "role": v.role.value,
                    "closing_line": v.closing_line,
                    "model_used": v.model_used,
                }
                for v in voices
            ],
            tier_preset=tier_preset.value if hasattr(tier_preset, "value") else str(tier_preset),
            total_cost_usd=float(total_cost),
            journal=journal,
        )
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("auto-journal plan_advised failed: %s", exc)

    return panel


def compute_pulse_deltas(
    *,
    panel: AdvisorPanel,
    previous_panel: AdvisorPanel | None,
) -> list[PulseDelta]:
    """Compare ``panel.voices`` to ``previous_panel.voices`` by closing line.

    v1 heuristic: closing-line text equality. Embedding-based delta
    detection is Sprint 5. Returns one PulseDelta per voice in
    ``PANEL_VOICE_ORDER`` so callers can render a stable table.
    """
    prev_by_role: dict[AgentRole, str] = {}
    if previous_panel is not None:
        for v in previous_panel.voices:
            prev_by_role[v.role] = v.closing_line

    deltas: list[PulseDelta] = []
    for v in panel.voices:
        prev_line = prev_by_role.get(v.role)
        changed = prev_line is not None and prev_line.strip() != v.closing_line.strip()
        # If there was no prior, it's not a "change" — it's a new voice.
        # We surface that as changed=False with reason='no prior pulse'.
        reason: str | None
        if prev_line is None:
            reason = "no prior pulse on file"
        elif changed:
            reason = "closing line shifted vs prior pulse"
        else:
            reason = None
        deltas.append(
            PulseDelta(
                role=v.role,
                previous_closing_line=prev_line,
                current_closing_line=v.closing_line,
                changed=changed,
                reason=reason,
            )
        )
    return deltas


async def run_pulse(
    session_id: UUID | str | None = None,
    *,
    project_id: UUID | str | None = None,
    previous_panel: AdvisorPanel | None = None,
    tier_preset: Tier = Tier.balanced,
    store: SessionStore | None = None,
    output_dir: Path | None = None,
    v1_source_signal: str = "",
    openai_client: AsyncOpenAI | None = None,
    journal: bool = True,
) -> PulsePanel:
    """Re-run the panel and surface deltas vs the prior pulse (S4-ADVISOR-05 / S5-API-02).

    Resolution order for the prior panel:
        1. Caller-supplied ``previous_panel`` (back-compat — wins).
        2. ``project_id`` walk: most recent ``pulse_runs`` row for the project.
        3. ``session_id`` walk: most recent ``pulse_runs`` row for that session.

    ``project_id`` takes precedence over ``session_id`` when both are
    given — most teams pulse across a project's history (multiple
    sessions) rather than against one session. ``session_id`` is still
    needed to load the panel's context (idea + transcript live on
    ``sessions``); when only ``project_id`` is provided we use the
    session_id from the most recent prior pulse.

    On every successful run we INSERT into ``pulse_runs`` so the next
    pulse has prior data to compare against. Persistence failures are
    swallowed (best-effort) so a transient DB blip doesn't crash the
    user-visible pulse.
    """
    if session_id is None and project_id is None:
        raise ValueError("run_pulse: session_id or project_id is required")

    if store is None:
        store = SessionStore.from_env()

    pid_uuid: UUID | None = (
        project_id
        if isinstance(project_id, UUID)
        else (UUID(str(project_id)) if project_id is not None else None)
    )
    sid_uuid: UUID | None = (
        session_id
        if isinstance(session_id, UUID)
        else (UUID(str(session_id)) if session_id is not None else None)
    )

    # Resolve the prior pulse via DB walk if the caller didn't pre-fetch one.
    if previous_panel is None:
        prior_row: dict[str, Any] | None = None
        try:
            prior_row = await store.get_latest_pulse_run(
                project_id=pid_uuid,
                session_id=sid_uuid if pid_uuid is None else None,
            )
        except Exception:  # pragma: no cover — best-effort
            logger.exception("run_pulse: prior pulse lookup failed; treating as no prior")
            prior_row = None
        if prior_row is not None:
            try:
                previous_panel = AdvisorPanel.model_validate(prior_row["panel_json"])
            except Exception:  # pragma: no cover — corrupt row, skip
                logger.exception("run_pulse: could not decode prior panel_json")
                previous_panel = None
            # Project-only callers: backfill session from the prior row so
            # we know which session's transcript to feed the panel.
            if sid_uuid is None and prior_row.get("session_id"):
                sid_uuid = UUID(str(prior_row["session_id"]))

    if sid_uuid is None:
        raise ValueError(
            "run_pulse: no session_id resolvable from project_id walk; "
            "first-time project pulses must pass session_id explicitly"
        )

    panel = await generate_panel(
        sid_uuid,
        tier_preset=tier_preset,
        store=store,
        output_dir=output_dir,
        v1_source_signal=v1_source_signal,
        openai_client=openai_client,
        journal=journal,
    )
    deltas = compute_pulse_deltas(panel=panel, previous_panel=previous_panel)

    # Persist this pulse so the NEXT one has prior data. Best-effort.
    try:
        await store.insert_pulse_run(
            session_id=sid_uuid,
            project_id=pid_uuid,
            panel_json=panel.model_dump(mode="json"),
            deltas_json=[d.model_dump(mode="json") for d in deltas],
        )
    except Exception:  # pragma: no cover — best-effort
        logger.exception("run_pulse: insert_pulse_run failed; pulse history not extended")

    # Auto-journal pulse_run (S5-MEM-04). Best-effort.
    try:
        from gecko_core.memory.auto_journal import journal_pulse

        record = await store.get(sid_uuid)
        record_project_id = record.project_id if record is not None else None
        await journal_pulse(
            session_id=sid_uuid,
            project_id=record_project_id,
            prior_panel_id=str(previous_panel.session_id) if previous_panel else None,
            current_closing_lines=[v.closing_line for v in panel.voices],
            deltas=[
                {
                    "voice": d.role.value,
                    "before": d.previous_closing_line,
                    "after": d.current_closing_line,
                }
                for d in deltas
            ],
            journal=journal,
        )
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("auto-journal pulse_run failed: %s", exc)

    return PulsePanel(
        panel=panel,
        deltas=deltas,
        previous_panel_at=previous_panel.generated_at if previous_panel else None,
    )


__all__ = [
    "AdvisorContext",
    "AdvisorError",
    "AdvisorPanel",
    "AdvisorSessionNotFoundError",
    "AdvisorVoice",
    "PulseDelta",
    "PulsePanel",
    "PulseResult",
    "compute_pulse_deltas",
    "generate_panel",
    "generate_voice",
    "load_context",
    "render_context_block",
    "run_pulse",
    "run_pulse_v14",
]
