"""Advisor Panel context loader (S4-ADVISOR-04).

The "what does the panel KNOW" layer. Synthesizes the session's Pro debate
output + scaffold artifacts (if scaffolded) + flywheel precedents + V1
source signal into one structured input. Each advisor voice gets the SAME
context — they differ in how they REASON about it.

Unlike the scaffold pipeline this loader does NOT refuse on
``verdict=kill``. After a kill the user might want advisor input on
whether to pivot, fold, or re-attack from a different angle. The
``pro_verdict`` field surfaces the verdict so each voice prompt can react
appropriately ("Strategic priority: stop, the kill is real" is a valid
CEO output here).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from pydantic import BaseModel

from gecko_core.orchestration.advisor.models import AdvisorSessionNotFoundError
from gecko_core.sessions.store import GeckoPrecedent, SessionStore

logger = logging.getLogger(__name__)

# Excerpt budget per scaffold artifact when we surface them to the panel.
# 500 chars = ~125 tokens — the panel reads orientation, not full doc.
_SCAFFOLD_EXCERPT_CHARS = 500


class AdvisorContext(BaseModel):
    """The structured input every advisor voice receives.

    All fields are best-effort: a fresh session with no scaffold and a
    cold flywheel still produces a usable context (with empty lists /
    None values surfaced explicitly so each voice can cite absence).
    """

    idea: str
    project_id: str | None
    pro_verdict: str | None
    pro_transcript: str | None
    scaffold_paths: list[str]
    scaffold_excerpts: dict[str, str]
    flywheel_precedents: list[GeckoPrecedent]
    v1_source_signal: str

    model_config = {"arbitrary_types_allowed": True}


def _format_transcript(transcript: dict[str, Any] | None) -> str | None:
    """Flatten the persisted Pro transcript turns. Mirrors scaffold's helper."""
    if not isinstance(transcript, dict):
        return None
    turns = transcript.get("turns")
    if not isinstance(turns, list):
        return None
    blocks: list[str] = []
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        agent = str(turn.get("agent") or "unknown").upper()
        content = str(turn.get("content") or "").strip()
        if not content:
            continue
        blocks.append(f"=== {agent} ===\n{content}")
    return "\n\n".join(blocks) if blocks else None


def _detect_verdict(summary: str | None, transcript: dict[str, Any] | None) -> str | None:
    """Best-effort verdict extraction. Mirrors scaffold._detect_verdict shape
    but returns None instead of 'unknown' so kills are surfaced to voices."""
    import re

    candidate = (summary or "").strip()
    if not candidate and transcript is not None:
        turns = transcript.get("turns") if isinstance(transcript, dict) else None
        if isinstance(turns, list):
            for turn in reversed(turns):
                if isinstance(turn, dict) and turn.get("agent") == "judge":
                    candidate = str(turn.get("content") or "")
                    break
    if not candidate:
        return None
    match = re.search(r"verdict\s*[:\-]\s*(ship|kill|pivot)", candidate, re.IGNORECASE)
    return match.group(1).lower() if match else None


def _scaffold_excerpts_for(session_id: UUID, output_dir: Path) -> tuple[list[str], dict[str, str]]:
    """If a scaffold bundle exists for this session, return (paths, excerpts).

    Mirrors ``scaffold._safe_session_dir`` resolution. We don't error on a
    missing scaffold — the panel works without one, just with less context.
    """
    base = output_dir.expanduser().resolve()
    target = (base / ".gecko" / "scaffolds" / str(session_id)).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        # Path traversal somehow — refuse to read.
        return [], {}
    if not target.is_dir():
        return [], {}

    excerpts: dict[str, str] = {}
    paths: list[str] = []
    for name in ("PRD.md", "business-plan.md", "BUILDING.md"):
        p = target / name
        if not p.is_file():
            continue
        paths.append(str(p))
        try:
            text = p.read_text(encoding="utf-8")
        except OSError as exc:  # pragma: no cover — defensive
            logger.warning("advisor: could not read scaffold %s: %s", p, exc)
            continue
        excerpts[name] = text[:_SCAFFOLD_EXCERPT_CHARS]
    return paths, excerpts


async def load_context(
    session_id: UUID | str,
    *,
    store: SessionStore,
    output_dir: Path | None = None,
    v1_source_signal: str = "",
) -> AdvisorContext:
    """Load the AdvisorContext for ``session_id``.

    Args:
        session_id: UUID of an existing session (any tier, any verdict).
        store: SessionStore — required, no env fallback (callers compose).
        output_dir: Workspace root for scaffold lookup. ``None`` skips
            scaffold excerpts (useful for headless API contexts).
        v1_source_signal: Pre-rendered V1 block from
            ``gecko_core.sources.v1_block.render_block``. The advisor
            module does NOT dispatch sources itself — that's an expensive
            paid path that the caller (Pro debate or pulse runner) owns.
            Pass empty string when fresh dispatch isn't appropriate.

    Raises:
        AdvisorSessionNotFoundError: session row missing or soft-deleted.
    """
    sid = session_id if isinstance(session_id, UUID) else UUID(str(session_id))

    record = await store.get(sid)
    if record is None:
        raise AdvisorSessionNotFoundError(f"session {sid} not found")

    # Result may be missing if the session is still running or failed mid-flight.
    # The panel can still run — it just gets a leaner context.
    result = await store.get_result(sid)
    transcript = (
        cast("dict[str, Any] | None", result.get("transcript"))
        if isinstance(result, dict)
        else None
    )
    summary_obj = result.get("pro_session_summary") if isinstance(result, dict) else None
    summary = str(summary_obj) if isinstance(summary_obj, str) else None

    pro_transcript = _format_transcript(transcript)
    pro_verdict = _detect_verdict(summary, transcript)

    scaffold_paths: list[str] = []
    scaffold_excerpts: dict[str, str] = {}
    if output_dir is not None:
        scaffold_paths, scaffold_excerpts = _scaffold_excerpts_for(sid, output_dir)

    # Flywheel precedents — best-effort. Embed the idea so retrieve_gecko_precedent
    # has a query vector. If the embedder fails (no API key in tests, etc.),
    # fall through with an empty precedent list.
    flywheel_precedents: list[GeckoPrecedent] = []
    try:
        from gecko_core.ingestion.embedder import embed_for_postgres_vector

        vecs, _tokens = await embed_for_postgres_vector([record.idea])
        if vecs:
            flywheel_precedents = await store.retrieve_gecko_precedent(embedding=vecs[0], limit=5)
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("advisor: precedent retrieval failed for %s: %s", sid, exc)

    return AdvisorContext(
        idea=record.idea,
        project_id=str(record.project_id) if record.project_id else None,
        pro_verdict=pro_verdict,
        pro_transcript=pro_transcript,
        scaffold_paths=scaffold_paths,
        scaffold_excerpts=scaffold_excerpts,
        flywheel_precedents=flywheel_precedents,
        v1_source_signal=v1_source_signal,
    )


def render_context_block(ctx: AdvisorContext) -> str:
    """Render an AdvisorContext as the user-prompt block fed to each voice.

    Same string for every voice (they differ via system prompt, not input).
    """
    parts: list[str] = [
        f"# Idea\n{ctx.idea}",
    ]
    if ctx.project_id:
        parts.append(f"# Project\n{ctx.project_id}")
    if ctx.pro_verdict:
        parts.append(f"# Pro debate verdict\n{ctx.pro_verdict.upper()}")
    else:
        parts.append("# Pro debate verdict\n(none — session has no recorded verdict)")

    if ctx.pro_transcript:
        parts.append(f"# Pro debate transcript\n{ctx.pro_transcript}")
    else:
        parts.append("# Pro debate transcript\n(no transcript on file)")

    if ctx.scaffold_excerpts:
        excerpt_block = "\n\n".join(
            f"## {name}\n{body}" for name, body in ctx.scaffold_excerpts.items()
        )
        parts.append(
            f"# Scaffold artifacts (first {_SCAFFOLD_EXCERPT_CHARS} chars each)\n{excerpt_block}"
        )
    else:
        parts.append("# Scaffold artifacts\n(no scaffold bundle on disk)")

    if ctx.flywheel_precedents:
        precedent_lines: list[str] = []
        for p in ctx.flywheel_precedents:
            verdict = p.verdict.upper()
            if p.similarity is not None:
                precedent_lines.append(f"- [{verdict}] {p.idea_summary} (sim={p.similarity:.2f})")
            else:
                precedent_lines.append(f"- [{verdict}] {p.idea_summary}")
        parts.append("# Gecko Flywheel precedents\n" + "\n".join(precedent_lines))
    else:
        parts.append("# Gecko Flywheel precedents\n(no similar prior verdicts on file)")

    if ctx.v1_source_signal.strip():
        parts.append(ctx.v1_source_signal)
    else:
        parts.append("# V1 source signal\n(not dispatched for this advisor run)")

    return "\n\n".join(parts)


__all__ = [
    "AdvisorContext",
    "load_context",
    "render_context_block",
]
