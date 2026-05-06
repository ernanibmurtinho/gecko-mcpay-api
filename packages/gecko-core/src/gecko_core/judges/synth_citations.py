"""S20-C-CITATION-CONTRACT-01 — structured citation extraction.

Parses the model's ``citations`` JSON field into a validated
``list[CitationMarker]`` and the matching ``cited_doc_ids: list[str]``.

Two validation passes are applied (silent at WARN — model hallucinations
are expected, not errors; we just track the rate):

1. **Doc-id grounding.** Every emitted ``doc_id`` must appear in the
   ``rag_context`` chunk set. Hallucinated doc_ids are dropped and the
   per-call hallucination rate is logged at INFO via
   ``synth.citation.hallucination_rate``.

2. **Prose-marker alignment.** Every ``[N]`` marker in the prose surfaces
   surfaces (gap_explanation, business_plan summaries, …) must have a
   matching ``citations[*].idx == N`` entry. Unmatched prose markers are
   dropped from ``cited_doc_ids`` (the marker stays in prose — that's the
   renderer's problem; we just don't credit a chunk for a missing entry).
   Drop count is logged at WARN via ``synth.citation.dropped``.

This module is the I/O-free parser; the basic-tier orchestrator in
``gecko_core.orchestration.basic`` wires the call into the synth path
and the workflows layer propagates the result onto ``ResearchResult``.

REACHABILITY (per CLAUDE.md feedback_wedge_reachability_check):
    rag_query → basic._SYSTEM_PROMPT (instructs JSON `citations`)
        → basic.generate parses with extract_citation_markers
        → ResearchResult.cited_doc_ids / citation_markers
        → workflows.research returns to caller (CLI / API / MCP)
    Pro tier: workflows.research keeps base_result fields via model_copy
    update; cited_doc_ids passes through unchanged.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from typing import Any

from pydantic import ValidationError

from gecko_core.models import CitationMarker

logger = logging.getLogger(__name__)


# Match `[N]` (1-3 digits) — same regex used by basic.py's inline-ref
# scrubber so the contract is consistent across surfaces.
_INLINE_MARKER_RE = re.compile(r"\[(\d{1,3})\]")

# Cap for the optional span excerpt — must match the CitationMarker field
# constraint. Defined here so the parser can truncate before pydantic
# raises a ValidationError on a slightly-too-long span.
_SPAN_MAX_CHARS = 200


def _coerce_marker(item: Any) -> dict[str, Any] | None:
    """Best-effort coercion of one raw citation entry to a dict.

    Returns ``None`` when the entry isn't dict-shaped — the caller drops
    those silently (an LLM occasionally emits a bare URL string under
    ``citations``; that's a structural drop, not a hallucination).
    """
    if not isinstance(item, dict):
        return None
    out: dict[str, Any] = dict(item)
    span = out.get("span")
    if isinstance(span, str) and len(span) > _SPAN_MAX_CHARS:
        out["span"] = span[: _SPAN_MAX_CHARS - 3] + "..."
    return out


def _collect_prose_markers(prose_surfaces: Iterable[str | None]) -> set[int]:
    """Union of every ``[N]`` integer found across the supplied prose strings."""
    found: set[int] = set()
    for text in prose_surfaces:
        if not text:
            continue
        for m in _INLINE_MARKER_RE.finditer(text):
            try:
                found.add(int(m.group(1)))
            except ValueError:  # pragma: no cover — re already constrains digits
                continue
    return found


def extract_citation_markers(
    *,
    raw_citations: Any,
    allowed_doc_ids: set[str],
    prose_surfaces: Iterable[str | None] = (),
) -> tuple[list[CitationMarker], list[str]]:
    """Validate and align the model's ``citations`` field.

    Args:
        raw_citations: the parsed value of the model's top-level
            ``citations`` field (typically ``list[dict]``; tolerated:
            ``None``, non-list values — both treated as empty).
        allowed_doc_ids: the chunk_id set from the rag_context the model
            was shown. Any doc_id outside this set is dropped at WARN-rate
            INFO log.
        prose_surfaces: optional iterable of prose strings (gap_explanation,
            etc.) used to verify every ``[N]`` marker has a matching
            ``citations[*].idx``. Unmatched prose markers are removed from
            the returned ``cited_doc_ids``.

    Returns:
        ``(markers, cited_doc_ids)`` — both empty when input is empty.

    Note: silent-degradation contract. Bad input never raises; it logs
    and returns an empty (or smaller) list. Callers want best-effort.
    """
    if raw_citations is None or not isinstance(raw_citations, list):
        return [], []

    total = len(raw_citations)
    if total == 0:
        return [], []

    # Pass 1 — coerce + pydantic-validate each entry.
    validated: list[CitationMarker] = []
    coercion_dropped = 0
    for entry in raw_citations:
        coerced = _coerce_marker(entry)
        if coerced is None:
            coercion_dropped += 1
            continue
        try:
            marker = CitationMarker.model_validate(coerced)
        except ValidationError:
            coercion_dropped += 1
            continue
        validated.append(marker)

    # Pass 2 — drop hallucinated doc_ids.
    grounded: list[CitationMarker] = []
    hallucinated = 0
    for m in validated:
        if not allowed_doc_ids or m.doc_id in allowed_doc_ids:
            grounded.append(m)
        else:
            hallucinated += 1

    if total > 0:
        logger.info(
            "synth.citation.hallucination_rate dropped=%d total=%d pct=%.1f",
            hallucinated + coercion_dropped,
            total,
            100.0 * (hallucinated + coercion_dropped) / total,
        )

    # Pass 3 — align with prose `[N]` markers (when surfaces supplied).
    prose_idxs = _collect_prose_markers(prose_surfaces)
    if prose_surfaces and prose_idxs:
        # Only emit a doc_id when its idx is referenced in prose AND the
        # entry survived hallucination filtering.
        matched: list[CitationMarker] = []
        unmatched = 0
        for m in grounded:
            if m.idx in prose_idxs:
                matched.append(m)
            else:
                unmatched += 1
        # Also count prose markers with NO matching entry — those are the
        # `[3]` in prose with no `citations[*].idx == 3` case.
        unmatched_prose = len(prose_idxs - {m.idx for m in grounded})
        if unmatched or unmatched_prose:
            logger.warning(
                "synth.citation.dropped unmatched_entries=%d unmatched_prose_markers=%d",
                unmatched,
                unmatched_prose,
            )
        cited_doc_ids = [m.doc_id for m in matched]
        return matched, cited_doc_ids

    # No prose surfaces supplied — trust the entries we have.
    return grounded, [m.doc_id for m in grounded]


__all__ = ["extract_citation_markers"]
