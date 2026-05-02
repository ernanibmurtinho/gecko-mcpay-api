"""S14-PULSE-01 — `gecko_pulse <session_id>` engine.

Re-runs the 5-voice advisor panel against a parent research session's
RAG context + windowed fresh chunks (last 14 days), creates a NEW session
row with ``phase="during_build"`` and ``parent_session_id`` set, and
returns a structured :class:`PulseResult` carrying:

  * single-token ``verdict`` (PIVOT / REFINE / GO)
  * structured ``gap_classification``
  * advisor closing lines (via ``panel.voices``)
  * fresh ``citations`` from the windowed chunk match
  * 3-bullet text-only ``summary_bullets`` (NO delta computation in v14;
    delta render lands in S15-PULSE-DELTA-01)

The advisor panel itself is reused from :mod:`gecko_core.orchestration.advisor`;
this module is the thin "pulse session" wrapper that owns the new-session
creation + verdict/gap synthesis from panel closing lines.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from uuid import UUID

from gecko_core.models import (
    GAP_CLASSIFICATION_FALLBACK,
    Citation,
    GapClassification,
    Verdict,
    derive_verdict,
    is_low_grounding,
)
from gecko_core.orchestration.advisor.models import (
    AdvisorPanel,
    AdvisorSessionNotFoundError,
    PulseResult,
)
from gecko_core.routing.catalog import Tier
from gecko_core.sessions.store import SessionStore

if TYPE_CHECKING:
    from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

# Keywords scanned against advisor closing lines to estimate consensus.
# Cheap heuristic — the panel doesn't emit a structured "ship" flag yet.
# Tightened in S15 once delta-based consensus lands.
_BUILD_KEYWORDS = ("ship", "build", "go", "launch", "yes", "proceed")
_KILL_KEYWORDS = ("kill", "stop", "abandon", "no-go", "pivot away", "drop")
_REFINE_KEYWORDS = ("refine", "narrow", "pivot", "rework", "wait", "test more")


def _consensus_from_panel(panel: AdvisorPanel) -> tuple[float, GapClassification]:
    """Estimate (build_consensus, gap_classification) from panel closing lines.

    Returns:
        (consensus, gap) where ``consensus`` is the fraction of voices
        whose closing line carries a build/ship-shaped sentiment, and
        ``gap`` is a coarse gap_classification estimate. The gap defaults
        to ``Partial:segment`` (the configured fallback) when the panel
        doesn't lean clearly to any signal.
    """
    if not panel.voices:
        return 0.0, GAP_CLASSIFICATION_FALLBACK

    build_count = 0
    kill_count = 0
    for voice in panel.voices:
        line = (voice.closing_line or "").lower()
        if any(k in line for k in _BUILD_KEYWORDS):
            build_count += 1
        elif any(k in line for k in _KILL_KEYWORDS):
            kill_count += 1

    consensus = build_count / max(1, len(panel.voices))

    # Coarse gap mapping. Conservative defaults — S15 will replace this
    # with a structured judge call once the eval rubric speaks the new
    # shape natively.
    if kill_count >= 3:
        gap: GapClassification = "Full"
    elif build_count >= 4:
        # Strong build consensus — suggest pricing/integration partial
        # so derive_verdict can promote to GO.
        gap = "Partial:pricing"
    else:
        gap = GAP_CLASSIFICATION_FALLBACK
    return consensus, gap


def _summary_bullets(panel: AdvisorPanel) -> list[str]:
    """Build a 3-bullet text-only "what changed" summary from the panel.

    v14 is text-only (no delta vs prior pulse). We surface the three
    most distinctive closing lines as proxy bullets — enough to read,
    not yet a real delta. Real delta lands in S15.
    """
    bullets: list[str] = []
    seen: set[str] = set()
    for voice in panel.voices:
        line = (voice.closing_line or "").strip()
        if not line:
            continue
        if line in seen:
            continue
        seen.add(line)
        bullets.append(f"{voice.role.value}: {line}")
        if len(bullets) >= 3:
            break
    return bullets


async def _fresh_citations(
    *,
    store: SessionStore,
    parent_session_id: UUID,
    project_id: UUID | None,
    idea: str,
    window_days: int = 14,
    match_count: int = 6,
) -> list[Citation]:
    """Pull fresh windowed citations via ``match_chunks_windowed``.

    Best-effort: any failure (no embedder configured, RPC missing,
    Supabase down) returns an empty list. Pulse still runs — citations
    are evidence, not blocking.
    """
    try:
        from gecko_core.ingestion.embedder import embed
    except Exception as exc:  # pragma: no cover — defensive import
        logger.debug("pulse: embedder import failed: %s", exc)
        return []

    try:
        vecs, _tokens = await embed([idea])
    except Exception as exc:  # pragma: no cover — env / network failure
        logger.debug("pulse: embed of parent idea failed: %s", exc)
        return []
    if not vecs:
        return []

    try:
        rows = await store.match_chunks_windowed(
            query_embedding=vecs[0],
            window_days=window_days,
            project_id=project_id,
            match_count=match_count,
        )
    except Exception as exc:  # pragma: no cover — RPC missing in stub
        logger.debug("pulse: match_chunks_windowed failed: %s", exc)
        return []

    return [
        Citation(
            source_url=r.source_url,
            chunk_index=r.chunk_index,
            similarity=float(r.similarity),
        )
        for r in rows
    ]


async def run_pulse_v14(
    parent_session_id: UUID | str,
    *,
    tier_preset: Tier = Tier.balanced,
    store: SessionStore | None = None,
    openai_client: AsyncOpenAI | None = None,
    journal: bool = True,
    skip_citations: bool = False,
) -> PulseResult:
    """Run a v14 pulse against ``parent_session_id``.

    Steps:
      1. Load the parent session (404 if missing).
      2. Create a NEW session row with ``phase="during_build"`` and
         ``parent_session_id`` pointing at the parent.
      3. Run the 5-voice advisor panel against the parent's context.
      4. Synthesize verdict + gap_classification from panel consensus.
      5. Pull fresh windowed citations (best-effort; ``window_days=14``).
      6. Return PulseResult.

    Persistence of advisor outputs against the new pulse_session_id is
    handled by the existing ``run_pulse`` plumbing inside ``generate_panel``
    (via ``auto_journal``). The pulse_session_id row itself is created
    here so the FK chain is intact.

    Args:
        parent_session_id: UUID of the original ``phase="pre_product"``
            research session being re-pulsed.
        tier_preset: Routing tier for the panel (default ``balanced``).
        store: SessionStore. Defaults to env-configured.
        openai_client: AsyncOpenAI client. Defaults to env-configured
            via the panel's normal resolver.
        journal: Forwarded to ``generate_panel`` for memory hooks.
        skip_citations: Tests with no embedder set this True to bypass
            the windowed-citation lookup.

    Raises:
        AdvisorSessionNotFoundError: parent session row missing.
    """
    # Lazy import to avoid circular import (advisor/__init__ imports models).
    from gecko_core.orchestration.advisor import generate_panel

    if store is None:
        store = SessionStore.from_env()

    parent_uuid = (
        parent_session_id if isinstance(parent_session_id, UUID) else UUID(str(parent_session_id))
    )

    parent_record = await store.get(parent_uuid)
    if parent_record is None:
        raise AdvisorSessionNotFoundError(f"parent session {parent_uuid} not found")

    # S14-PULSE-01 — create a NEW session row with phase="during_build" and
    # parent_session_id set. The new row carries the parent's idea so the
    # advisor panel context loader (which reads `idea` off the session)
    # produces the same context as the parent run.
    pulse_session_id = await store.create(
        idea=parent_record.idea,
        tier=parent_record.tier,
        payment_mode=parent_record.payment_mode,
        phase="during_build",
        parent_session_id=parent_uuid,
    )

    # Run the panel against the parent session's context — the parent has
    # the transcript + sources the panel needs. The new pulse session row
    # exists for lifecycle tracking; it doesn't yet have its own transcript.
    panel = await generate_panel(
        parent_uuid,
        tier_preset=tier_preset,
        store=store,
        openai_client=openai_client,
        journal=journal,
    )

    # Fresh windowed citations — best-effort. Pulled BEFORE verdict
    # synthesis so the S17-VERDICT-01 evidence-strength floor can read
    # them and force REFINE when grounding is thin.
    citations: list[Citation] = []
    if not skip_citations:
        citations = await _fresh_citations(
            store=store,
            parent_session_id=parent_uuid,
            project_id=parent_record.project_id,
            idea=parent_record.idea,
        )

    # Synthesize verdict + gap from panel consensus + citation grounding.
    consensus, gap = _consensus_from_panel(panel)
    low_grounding = is_low_grounding(citations)
    verdict: Verdict = derive_verdict(
        gap,
        advisor_consensus=consensus,
        citations=citations,
    )

    summary_bullets = _summary_bullets(panel)

    pulse_result = PulseResult(
        parent_session_id=str(parent_uuid),
        pulse_session_id=str(pulse_session_id),
        idea=parent_record.idea,
        verdict=verdict,
        gap_classification=gap,
        panel=panel,
        citations=citations,
        summary_bullets=summary_bullets,
        low_grounding=low_grounding,
    )

    # S17-PERSIST-01 — persist the pulse result JSON on the new pulse
    # session row and mark it complete. Without this, the row stays at
    # status='pending' / result_json=NULL forever and downstream readers
    # (advisor context lookups, pulse-history walks, /sessions/{id}/result
    # polling) fail with "not ready". Best-effort: a Supabase write failure
    # here shouldn't kill the user's already-computed pulse. The API-side
    # caller may also write — that's fine, set_result is idempotent.
    try:
        await store.set_result(pulse_session_id, pulse_result.model_dump(mode="json"))
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("pulse: set_result failed for %s: %s", pulse_session_id, exc)
    try:
        await store.update_status(pulse_session_id, "complete")
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("pulse: update_status(complete) failed for %s: %s", pulse_session_id, exc)

    return pulse_result


__all__ = [
    "run_pulse_v14",
]
