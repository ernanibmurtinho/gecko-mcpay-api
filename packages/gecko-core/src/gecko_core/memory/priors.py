"""Prior-decision injection (S6-MINE-02).

Reads recent `verdict_received` + `feature_shipped` entries for a project
and renders them as a "PRIOR DECISIONS" context block (≤ ~800 tokens) that
gets prepended to the system prompt of debate / advisor calls.

Gated by ``GECKO_MEMORY_PRIORS=1`` env. ``False`` (the default) returns an
empty string so the prompt surface is unchanged for any caller that hasn't
opted in.
"""

from __future__ import annotations

import logging
import os

from gecko_core.memory.models import MemoryEntry, MemoryEntryType, MemoryScope
from gecko_core.memory.store import MemoryStore
from gecko_core.routing.costs import estimate_tokens

logger = logging.getLogger(__name__)

# Soft cap (in tokens) for the rendered priors block. Cap is enforced by
# *truncating entries from oldest first* until the block fits — see the
# comment in `render_priors_block` for why we don't truncate within an
# entry's value (loses the verdict signal).
PRIORS_TOKEN_BUDGET = 800
PRIORS_MAX_ENTRIES = 12


def is_priors_enabled() -> bool:
    """True if priors injection is opted in via env. Defaults False."""
    return os.environ.get("GECKO_MEMORY_PRIORS", "0").strip() in ("1", "true", "True", "yes")


def _format_entry(entry: MemoryEntry) -> str:
    """Render one memory entry as a single line for the priors block."""
    when = entry.created_at.strftime("%Y-%m-%d")
    if entry.entry_type == MemoryEntryType.verdict_received:
        verdict = str(entry.value.get("verdict") or entry.value.get("decision") or "?").upper()
        idea = str(entry.value.get("idea") or entry.value.get("idea_summary") or "")[:120]
        return f"- [{when}] VERDICT={verdict} on {idea}".rstrip()
    if entry.entry_type == MemoryEntryType.feature_shipped:
        feature = str(entry.value.get("feature") or entry.value.get("name") or "")[:120]
        outcome = str(entry.value.get("outcome") or entry.value.get("result") or "")[:80]
        line = f"- [{when}] SHIPPED {feature}"
        if outcome:
            line += f" ({outcome})"
        return line
    # Fallback for any future type that opts in via the loader.
    return f"- [{when}] {entry.entry_type.value}: {str(entry.value)[:120]}"


async def fetch_priors(
    project_id: str,
    *,
    top_k: int = PRIORS_MAX_ENTRIES,
    store: MemoryStore | None = None,
) -> list[MemoryEntry]:
    """Return up to `top_k` recent `verdict_received` + `feature_shipped`
    entries for a project, newest first.

    Two indexed lookups (one per entry_type), interleaved by created_at.
    No embedding work — this is the cheap path.
    """
    store = store or MemoryStore.from_env()
    scope = MemoryScope(type="project", id=str(project_id))
    try:
        verdicts = await store.list_by_scope(
            scope=scope,
            entry_type=MemoryEntryType.verdict_received,
            limit=top_k,
        )
        ships = await store.list_by_scope(
            scope=scope,
            entry_type=MemoryEntryType.feature_shipped,
            limit=top_k,
        )
    except Exception as exc:  # pragma: no cover — defensive; never fail caller
        logger.warning("fetch_priors: store lookup failed for %s: %s", project_id, exc)
        return []
    merged = sorted(
        list(verdicts) + list(ships),
        key=lambda e: e.created_at,
        reverse=True,
    )
    return merged[:top_k]


def render_priors_block(entries: list[MemoryEntry]) -> str:
    """Render entries as a "PRIOR DECISIONS" block, capped at ~800 tokens.

    Drops oldest entries first when the block would exceed the budget; we
    never truncate inside an entry because the verdict label is the load-
    bearing signal we want preserved.
    """
    if not entries:
        return ""
    header = "# PRIOR DECISIONS (this project's memory)\n"
    lines = [_format_entry(e) for e in entries]
    while lines:
        candidate = header + "\n".join(lines)
        if estimate_tokens(candidate) <= PRIORS_TOKEN_BUDGET:
            break
        # Drop the oldest (the last item in the newest-first list).
        lines.pop()
    if not lines:
        return ""
    return header + "\n".join(lines)


async def build_priors_block(
    project_id: str | None,
    *,
    store: MemoryStore | None = None,
) -> str:
    """High-level helper: env-gate + fetch + render in one call.

    Returns the empty string if priors are disabled, no project_id is
    given, or the project has no recorded verdicts/shipments yet. Errors
    are swallowed defensively — priors must never break the caller.
    """
    if not is_priors_enabled() or not project_id:
        return ""
    try:
        entries = await fetch_priors(project_id, store=store)
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("build_priors_block: fetch failed for %s: %s", project_id, exc)
        return ""
    return render_priors_block(entries)


__all__ = [
    "PRIORS_MAX_ENTRIES",
    "PRIORS_TOKEN_BUDGET",
    "build_priors_block",
    "fetch_priors",
    "is_priors_enabled",
    "render_priors_block",
]
