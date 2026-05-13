"""Chunk-quality invariant — the single pre-write gate.

S29-#32. Existing Mongo corpus contained binary-encoded Berkshire PDFs
and empty-JSON paysh stubs that silently corrupted retrieval quality.
Per founder principle: re-ingestion is cheap and iterative; garbage
chunks are the real liability. This module is the durable artifact —
every chunk that lands in Mongo MUST pass :func:`assess_chunk_quality`
before insert.

Per CLAUDE.md "Single source of truth": this is the ONE quality gate.
If another quality check appears elsewhere, consolidate or remove.

The gate is intentionally cheap (regex-free; pure-Python; ~O(N) over
chunk text length, single pass for the binary check). It catches five
classes of garbage observed in the corpus during the 2026-05-14
diagnostic:

1. ``too_short``       — under min_chars, almost never useful as RAG context.
2. ``binary``          — non-printable byte ratio above 5% (PDF bytes,
                         null-byte padding, image data leaked from pdftotext).
3. ``repeated_chars``  — a single character dominates >50% of the chunk
                         (horizontal-rule pages, whitespace, ``"#"`` headers).
4. ``empty_json``      — exact-match JSON stubs from failed paysh/bazaar
                         live calls (``{"data":[]}`` and friends).
5. ``duplicate``       — RESERVED. The caller (insert path) detects
                         intra-batch duplicates via the unique key already;
                         left in the verdict reason set so the audit
                         script's per-reason counter has a stable enum.

The two thresholds are kwargs so the audit script (Pass 2) can sweep
boundaries without code-edits.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Final, Literal

QualityReason = Literal[
    "ok",
    "binary",
    "too_short",
    "repeated_chars",
    "empty_json",
    "duplicate",
]

# Exact-match strings that indicate an upstream API failure leaked into
# the chunker. NOT a substring check — partial matches (e.g. a citation
# that literally quotes ``{"data":[]}``) are legitimate. The empty-JSON
# class is exact-equality after strip().
_EMPTY_JSON_STUBS: Final[frozenset[str]] = frozenset(
    {
        '{"data":[]}',
        '{"data": []}',
        '{"results":[]}',
        '{"results": []}',
        '{"items":[]}',
        '{"items": []}',
        "{}",
        "[]",
        "null",
    }
)


@dataclass(frozen=True)
class ChunkQualityVerdict:
    """Pure-data verdict. Callers branch on ``is_substantive``."""

    is_substantive: bool
    reason: QualityReason


def assess_chunk_quality(
    text: str,
    *,
    min_chars: int = 200,
    max_repeat_ratio: float = 0.50,
    min_printable_ratio: float = 0.95,
) -> ChunkQualityVerdict:
    """Single-call gate. Returns ``is_substantive`` + ``reason``.

    The five-class reason taxonomy is stable; do not add new reasons
    without updating the audit script's report headers and the
    ``QualityReason`` Literal in tandem (Pattern A).

    ``min_chars=200`` ≈ 35-50 tokens — empirically the floor below which
    investor-canon and protocol-doc chunks carry no usable signal.
    Raising this past ~250 starts dropping real Damodaran one-liners;
    leave it alone unless you measure.
    """
    # Empty-JSON stub check runs FIRST. These are short by definition,
    # so without this short-circuit the audit's per-reason counter would
    # bucket every paysh/bazaar API-failure stub as "too_short" and bury
    # the actual cause. The stub set is exact-match after strip().
    if text and text.strip() in _EMPTY_JSON_STUBS:
        return ChunkQualityVerdict(False, "empty_json")

    if not text or len(text.strip()) < min_chars:
        return ChunkQualityVerdict(False, "too_short")

    # Binary / null-byte check. Counts ``\x00``-style and non-printable
    # control bytes; whitespace (``\n``, ``\t``) is allowed.
    printable_count = sum(1 for c in text if c.isprintable() or c.isspace())
    printable_ratio = printable_count / len(text)
    if printable_ratio < min_printable_ratio:
        return ChunkQualityVerdict(False, "binary")

    # Repeated-character runs (whitespace-only chunks, horizontal-rule
    # pages, header-bar pages from PDFs).
    char_counts = Counter(text)
    if char_counts:
        _top_char, top_count = char_counts.most_common(1)[0]
        if top_count / len(text) > max_repeat_ratio:
            return ChunkQualityVerdict(False, "repeated_chars")

    return ChunkQualityVerdict(True, "ok")


__all__ = [
    "ChunkQualityVerdict",
    "QualityReason",
    "assess_chunk_quality",
]
