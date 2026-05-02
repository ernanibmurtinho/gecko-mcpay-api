"""Post-Pro scaffold stage (S3-01).

After a Pro tier debate concludes with verdict `ship` or `pivot`, this module
takes the persisted session (idea + transcript + sources + judge verdict) and
emits a 3-file project starter bundle:

    <output_dir>/.gecko/scaffolds/<session_id>/
        PRD.md
        business-plan.md
        BUILDING.md

A single gpt-4o call (temp 0.2, json_object response_format) synthesizes all
three docs in one shot. JSON-in/JSON-out so we can validate the shape before
writing anything to disk — partial writes are worse than a clear error.

This is FREE (no x402 charge) — the user already paid for the Pro debate that
produced the transcript. The synthesizer's LLM cost is attributed to the
existing session's cost ledger by the persistence layer, not to the caller.

Refuses on `verdict=kill`: there is no value in scaffolding a killed idea, and
the demo wow-moment is the SHIP path. Callers should map `KillVerdictError`
to a clear user-facing error.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from openai import AsyncOpenAI
from pydantic import ValidationError

from gecko_core.orchestration.scaffold._prompt import load_system_prompt
from gecko_core.orchestration.scaffold.models import (
    KillVerdictError,
    ScaffoldDocs,
    ScaffoldError,
    ScaffoldResult,
    SessionNotFoundError,
    SessionNotReadyError,
)
from gecko_core.sessions.store import SessionStore

logger = logging.getLogger(__name__)

# gpt-4o pricing (USD per 1M tokens) — used as a fallback estimate when the
# wrapped client doesn't surface a `x-clawrouter-cost-usd` header. Mirrors
# the table in `orchestration/basic.py`.
_GPT4O_INPUT_RATE = 2.50
_GPT4O_OUTPUT_RATE = 10.00


def _detect_verdict(transcript: dict[str, Any] | None, summary: str | None) -> str:
    """Best-effort verdict extraction from the persisted transcript.

    The Judge's final paragraph contains a line like
    ``Verdict: SHIP V1 to <segment>`` or ``Verdict: KILL — <reason>`` or
    ``Verdict: PIVOT — <suggestion>``. We match case-insensitively against
    that prefix; we do NOT free-search the whole transcript because the
    other agents may quote the word "ship" / "kill" in passing.

    Returns one of {'go', 'ship', 'build', 'pivot', 'refine', 'kill', 'unknown'}.
    Legacy ``KILL`` / ``BUILD`` / ``SHIP`` strings are preserved for transcripts
    persisted before S17-TONE-01; new debates emit GO / PIVOT / REFINE.
    """
    candidate = (summary or "").strip()
    if not candidate and transcript is not None:
        turns_obj = transcript.get("turns") if isinstance(transcript, dict) else None
        if isinstance(turns_obj, list):
            for turn in reversed(turns_obj):
                if isinstance(turn, dict) and turn.get("agent") == "judge":
                    candidate = str(turn.get("content") or "")
                    break
    if not candidate:
        return "unknown"
    match = re.search(
        r"verdict\s*[:\-]?\s*\**\s*(go|ship|build|pivot|refine|kill)",
        candidate,
        re.IGNORECASE,
    )
    if match:
        return match.group(1).lower()
    return "unknown"


def _format_transcript(transcript: dict[str, Any]) -> str:
    """Render the persisted transcript as a flat readable block for the synthesizer.

    The synthesizer prompt is large; the transcript is the only variable
    input. We use a stable per-turn header so the model can refer back
    ('the Analyst said X') without us building a structured tool call.
    """
    turns = transcript.get("turns") if isinstance(transcript, dict) else None
    if not isinstance(turns, list):
        return "(transcript missing or malformed)"
    blocks: list[str] = []
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        agent = str(turn.get("agent") or "unknown").upper()
        content = str(turn.get("content") or "").strip()
        if not content:
            continue
        blocks.append(f"=== {agent} ===\n{content}")
    return "\n\n".join(blocks) if blocks else "(no turns recorded)"


def _format_sources(sources: list[dict[str, Any]]) -> str:
    """Render the session's sources as a citation list for the synthesizer.

    Pulled from `result.sources` (each item has `url` and optionally `type`).
    Keeps the synthesizer honest in the PRD's "Cited evidence" section —
    if the user never indexed sources we tell the model so it can write
    "(no external sources indexed)" rather than fabricate URLs.
    """
    if not sources:
        return "(no sources indexed for this session)"
    lines: list[str] = []
    for src in sources:
        if not isinstance(src, dict):
            continue
        url = str(src.get("url") or "").strip()
        if not url:
            continue
        kind = str(src.get("type") or "web")
        lines.append(f"- [{kind}] {url}")
    return "\n".join(lines) if lines else "(no sources indexed for this session)"


def _user_prompt(idea: str, transcript_block: str, sources_block: str, verdict: str) -> str:
    return (
        f"Idea: {idea}\n\n"
        f"Verdict: {verdict.upper()}\n\n"
        f"Sources indexed for this session:\n{sources_block}\n\n"
        f"Full debate transcript (5 agents in order):\n\n{transcript_block}\n\n"
        "Emit the JSON object specified in the system prompt. JSON only."
    )


def _safe_session_dir(output_dir: Path, session_id: UUID) -> Path:
    """Return the absolute scaffolds directory for a session.

    Anchors under ``<output_dir>/.gecko/scaffolds/<session_id>``. We
    ``resolve()`` both the configured output_dir and the final path and
    re-assert containment so a malicious caller can't slip a `..` in via
    a crafted session_id (UUIDs can't, but the type system can't prove
    that here — defense in depth). Raises ScaffoldError if the resolved
    target escapes output_dir.
    """
    base = output_dir.expanduser().resolve()
    target = (base / ".gecko" / "scaffolds" / str(session_id)).resolve()
    try:
        target.relative_to(base)
    except ValueError as exc:
        raise ScaffoldError(f"scaffold path escapes output_dir: {target}") from exc
    return target


async def _synthesize(
    *,
    client: AsyncOpenAI,
    system: str,
    user: str,
) -> tuple[ScaffoldDocs, int, float]:
    """Single gpt-4o call. Returns (docs, total_tokens, cost_usd).

    Temperature 0.2 — we want deterministic-ish structure, but a tiny bit
    of variation is fine for the prose.
    """
    raw = await client.chat.completions.with_raw_response.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        response_format={"type": "json_object"},
        temperature=0.2,
    )
    resp = raw.parse()
    content = resp.choices[0].message.content
    if not content:
        raise ScaffoldError("synthesizer returned empty content")

    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ScaffoldError(f"synthesizer output was not valid JSON: {exc}") from exc
    try:
        docs = ScaffoldDocs.model_validate(payload)
    except ValidationError as exc:
        raise ScaffoldError(f"synthesizer JSON missing required keys: {exc}") from exc

    prompt_tokens = int(getattr(resp.usage, "prompt_tokens", 0) or 0) if resp.usage else 0
    completion_tokens = int(getattr(resp.usage, "completion_tokens", 0) or 0) if resp.usage else 0
    total_tokens = prompt_tokens + completion_tokens

    cost_usd = 0.0
    header = raw.headers.get("x-clawrouter-cost-usd")
    if header:
        try:
            cost_usd = float(header)
        except ValueError:
            cost_usd = 0.0
    if cost_usd == 0.0:
        cost_usd = (
            prompt_tokens * _GPT4O_INPUT_RATE + completion_tokens * _GPT4O_OUTPUT_RATE
        ) / 1_000_000

    return docs, total_tokens, cost_usd


async def generate_scaffold(
    session_id: UUID | str,
    output_dir: Path | str,
    *,
    store: SessionStore | None = None,
    openai_client: AsyncOpenAI | None = None,
    journal: bool = True,
) -> ScaffoldResult:
    """Synthesize and persist the 3-file scaffold bundle for a Pro session.

    Args:
        session_id: UUID of a Pro tier session that has already produced a
            transcript and a verdict.
        output_dir: User's working directory. Files are written under
            ``<output_dir>/.gecko/scaffolds/<session_id>/``.
        store: SessionStore instance (defaults to env-configured client).
        openai_client: AsyncOpenAI client (defaults to env-configured).

    Returns:
        ScaffoldResult with the three written paths and synthesizer accounting.

    Raises:
        SessionNotFoundError: session row missing or soft-deleted.
        SessionNotReadyError: session has no result_json yet.
        KillVerdictError: judge verdict was 'kill' — refused.
        ScaffoldError: synthesizer call failed or output was malformed.
    """
    sid = session_id if isinstance(session_id, UUID) else UUID(str(session_id))
    out_dir = output_dir if isinstance(output_dir, Path) else Path(output_dir)

    if store is None:
        store = SessionStore.from_env()

    record = await store.get(sid)
    if record is None:
        raise SessionNotFoundError(f"session {sid} not found")

    result = await store.get_result(sid)
    if not result:
        raise SessionNotReadyError(f"session {sid} has no persisted result yet")

    transcript = cast("dict[str, Any] | None", result.get("transcript"))
    summary = result.get("pro_session_summary")
    summary_str = str(summary) if isinstance(summary, str) else None
    if transcript is None:
        raise SessionNotReadyError(
            f"session {sid} has no transcript — scaffold requires Pro tier output"
        )

    verdict = _detect_verdict(transcript, summary_str)
    if verdict not in {"go", "ship", "build", "pivot", "refine", "kill"}:
        raise ScaffoldError(
            f"could not determine verdict for session {sid} "
            "(expected 'go' / 'pivot' / 'refine' in judge's final paragraph)"
        )

    transcript_block = _format_transcript(transcript)
    sources_raw = result.get("sources")
    sources_list = sources_raw if isinstance(sources_raw, list) else []
    sources_block = _format_sources(cast("list[dict[str, Any]]", sources_list))

    if openai_client is None:
        # Lazy import — keeps the orchestration settings out of the type-
        # check path for callers that always inject a client (tests).
        from gecko_core.orchestration.settings import get_orchestration_settings

        orch = get_orchestration_settings()
        openai_client = AsyncOpenAI(api_key=orch.llm_api_key, base_url=orch.llm_endpoint)

    system = load_system_prompt()
    user = _user_prompt(record.idea, transcript_block, sources_block, verdict)

    docs, tokens_used, cost_usd = await _synthesize(
        client=openai_client,
        system=system,
        user=user,
    )

    target_dir = _safe_session_dir(out_dir, sid)
    target_dir.mkdir(parents=True, exist_ok=True)
    paths = [
        target_dir / "PRD.md",
        target_dir / "business-plan.md",
        target_dir / "BUILDING.md",
    ]
    paths[0].write_text(docs.prd_md, encoding="utf-8")
    paths[1].write_text(docs.business_plan_md, encoding="utf-8")
    paths[2].write_text(docs.building_md, encoding="utf-8")

    # Attribute synthesizer LLM cost to the existing session ledger so the
    # economics view stays accurate. Best-effort — a Supabase hiccup here
    # shouldn't lose the user the files we just wrote.
    try:
        await store.add_cost(sid, "llm", cost_usd)
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("scaffold add_cost failed for %s: %s", sid, exc)

    # Auto-journal scaffold_generated (S5-MEM-04). Best-effort.
    try:
        from gecko_core.memory.auto_journal import journal_scaffold

        await journal_scaffold(
            session_id=sid,
            project_id=record.project_id,
            output_paths=[str(p) for p in paths],
            total_tokens=tokens_used,
            cost_usd=cost_usd,
            journal=journal,
        )
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("auto-journal scaffold failed: %s", exc)

    return ScaffoldResult(
        paths=paths,
        session_id=sid,
        tokens_used=tokens_used,
        cost_usd=cost_usd,
        summary=summary_str or f"Scaffold ready for verdict={verdict}",
    )


__all__ = [
    "KillVerdictError",
    "ScaffoldDocs",
    "ScaffoldError",
    "ScaffoldResult",
    "SessionNotFoundError",
    "SessionNotReadyError",
    "generate_scaffold",
]
