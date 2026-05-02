"""Deterministic verdict hash (S18-VERDICT-HASH-01 / D4).

Foundation for the tradeable-judgment surface (S19-S1). The hash is the
verdict's stable identity: same idea + same retrieved citations + same
verdict shape → same sha256, regardless of which run produced it.

Hash inputs (all canonicalised before hashing so reruns under stable
retrieval reproduce the digest):

1. ``idea`` — caller-supplied prompt, normalised whitespace.
2. ``corpus_fingerprint`` — sha256 over the sorted, deduped list of
   ``source_url`` strings *that survived rerank* into the final result.
   The plan note (s18-plan §3.4) is explicit: hash the retrieved set,
   not the raw corpus, so embed-cache hits don't perturb the digest.
3. ``verdict_json`` — the ResearchResult model_dump'd through a
   stable-key serialiser. We hash the verdict + gap_classification +
   advisor_consensus, NOT the full prose: the prose drifts between
   reruns even when the structural decision is identical.

The CLI footer renders the first 12 hex chars (``verdict@a1b2c3d4e5f6``)
— enough entropy to be unique across a personal corpus while staying
short enough to read aloud. Full sha256 is available via ``verdict_hash``.

This module is store-agnostic by design — same input set produces the
same hash on Supabase and Mongo. The S18-D4 acceptance criterion lives
in tests/test_verdict_hash.py.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gecko_core.models import ResearchResult


_HASH_PREFIX = "verdict"
_SHORT_LEN = 12


def _normalise_idea(idea: str) -> str:
    """Trim + collapse whitespace. Idea text rarely changes mid-rerun
    but trailing newlines from CLI quoting do. Normalising prevents a
    spurious hash flip."""
    return " ".join(idea.split())


def _corpus_fingerprint(source_urls: list[str]) -> str:
    """Sorted, deduped sha256 over the retrieved citation set.

    Caller passes the URLs that landed in the verdict (e.g. from
    ``ResearchResult.sources``). Empty list yields a stable empty digest
    — degenerate but well-defined.
    """
    unique_sorted = sorted({u for u in source_urls if u})
    h = hashlib.sha256()
    for u in unique_sorted:
        h.update(u.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def _verdict_payload(result: ResearchResult) -> str:
    """Stable JSON serialisation of the structural verdict shape.

    Order matters because Python dicts preserve insertion; we use
    ``sort_keys=True`` defensively. Prose fields (``judge_prose``,
    ``pro_session_summary``) are deliberately excluded — they drift
    even when the verdict decision is identical.
    """
    payload = {
        "verdict": result.verdict.value
        if hasattr(result.verdict, "value")
        else str(result.verdict),
        "gap_classification": result.validation_report.gap_classification,
        "low_grounding": result.low_grounding,
        "tier": result.tier,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def verdict_hash(idea: str, result: ResearchResult) -> str:
    """Return the full 64-char hex sha256 for ``(idea, sources, verdict)``."""
    h = hashlib.sha256()
    h.update(_normalise_idea(idea).encode("utf-8"))
    h.update(b"\x00")
    h.update(_corpus_fingerprint([s.url for s in result.sources]).encode("utf-8"))
    h.update(b"\x00")
    h.update(_verdict_payload(result).encode("utf-8"))
    return h.hexdigest()


def verdict_hash_short(idea: str, result: ResearchResult) -> str:
    """Return ``verdict@<first 12 hex chars>`` for CLI footer rendering."""
    full = verdict_hash(idea, result)
    return f"{_HASH_PREFIX}@{full[:_SHORT_LEN]}"


__all__ = ["verdict_hash", "verdict_hash_short"]
