"""Chunk-write audit — error classification + audit row schema.

S16-INGEST-01. Single source of truth for the `error_kind` Literal. Pairs
with migration `20260501140000_chunks_write_audit.sql`; the SQL CHECK
constraint there mirrors `ERROR_KINDS` below. Drift is caught by
`tests/ingestion/test_audit_error_kind_consistency.py` (Pattern A from
CLAUDE.md — same shape as `test_payment_mode_consistency.py`).

Why classify at write time, not post-hoc:

The diagnostic memo (`docs/diagnostics/2026-05-01-chunk-write-failures.md`)
identified three confirmed failure modes (FM-1/FM-2/FM-3). We cannot fix
what we cannot count. This module turns an exception (or a partial-write
result) into one of a small fixed set of buckets that the audit table
indexes on. `bb doctor --recent` then prints the histogram — which mode
dominates determines which of S16-INGEST-02/03 fires first.

This module deliberately does NOT change behaviour: it only observes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, get_args
from uuid import UUID

ErrorKind = Literal[
    "none",
    "toast_limit",
    "pool_timeout",
    "rls_denied",
    "embedding_null",
    "dim_mismatch",
    "supabase_5xx",
    "unparseable_pdf",
    "unknown",
]
"""Canonical set of audit failure buckets. Mirrors the SQL CHECK in the
*latest* `*_chunks_write_audit*.sql` migration (last-write-wins —
20260502004217 dropped `partial_batch` after S16-INGEST-02 made
`insert_chunks` transactional, which makes FM-1 unreachable).

Adding a value: update this Literal AND ship a migration that ALTERs
the CHECK. Schema-drift test enforces equality (Pattern A from CLAUDE.md)."""

ERROR_KINDS: tuple[ErrorKind, ...] = get_args(ErrorKind)


@dataclass(frozen=True)
class ChunksWriteAudit:
    """One row destined for the `chunks_write_audit` table.

    `embed_model` is nullable: for batches that never reached the embedder
    (e.g. extract failed before any text was chunked) we don't pretend to
    know which model would have been used.
    """

    session_id: UUID
    source_id: UUID | None
    batch_size: int
    succeeded: int
    failed: int
    error_kind: ErrorKind
    embed_model: str | None


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------
#
# The order of checks below matters: a Supabase 503 may also have "timeout"
# in its message, but the 5xx bucket is the more specific story. Keep the
# most-specific check FIRST in each block.


def _msg(exc: BaseException) -> str:
    """Lowercased exception message — every classifier branch matches against
    this, so normalise once."""
    return str(exc).lower()


def _status_code(exc: BaseException) -> int | None:
    """Pull an HTTP status off `exc` if the underlying client surfaced one.
    Supabase-py wraps PostgREST errors in classes with `.code` (string) or
    `.status_code`; httpx exceptions carry a `.response.status_code`.
    """
    code = getattr(exc, "status_code", None)
    if isinstance(code, int):
        return code
    resp = getattr(exc, "response", None)
    if resp is not None:
        rc = getattr(resp, "status_code", None)
        if isinstance(rc, int):
            return rc
    # Supabase-py PostgrestAPIError.code is a string like "23514"; not an HTTP code.
    return None


def classify_exception(exc: BaseException) -> ErrorKind:
    """Map an exception raised inside the chunk-write path to an `ErrorKind`.

    Driven off exception class name + message inspection. We match on
    *class name string* (not `isinstance`) so this module does not import
    supabase-py / httpx / openai — keeps the audit layer dependency-free
    and unit-testable with bare `Exception` subclasses.
    """
    cls = type(exc).__name__
    msg = _msg(exc)
    status = _status_code(exc)

    # --- UnparseablePDFError (S17-INGEST-FALLBACK-01) -------------------
    # Raised when *every* parser in the PDF fallback chain (pdfium →
    # pdfminer → pypdf) fails on the same payload. The pipeline catches
    # this and records a clean info-level skip; the audit row gets
    # `unparseable_pdf` so the histogram can separate "PDF is broken" from
    # "we have a real bug".
    if cls == "UnparseablePDFError":
        return "unparseable_pdf"

    # --- ChunkValidationError (S16-INGEST-02 pre-flight) ----------------
    # Carries an explicit `kind` attr ('empty_text' | 'dim_mismatch') so
    # we don't re-parse the message — Pattern A: the producer labels it,
    # the classifier respects the label.
    if cls == "ChunkValidationError":
        kind = getattr(exc, "kind", None)
        if kind == "empty_text":
            return "embedding_null"
        if kind == "dim_mismatch":
            return "dim_mismatch"
        return "unknown"

    # --- Embedder side (FM-3) -------------------------------------------
    if cls == "RateLimitError" or "rate limit" in msg or "ratelimit" in msg:
        # The pipeline raises *after* embedder retries are exhausted. From
        # the chunk-write perspective the embedding column would be NULL,
        # which is what trips the NOT NULL constraint downstream — bucket
        # this as embedding_null so the histogram points at FM-3 directly.
        return "embedding_null"
    if "embedding" in msg and ("null" in msg or "none" in msg or "missing" in msg):
        return "embedding_null"

    # --- Vector dim drift (FM-2) ----------------------------------------
    if "expected 1536 dimensions" in msg or ("dimension" in msg and "vector" in msg):
        return "dim_mismatch"
    if "vector(1536)" in msg and ("mismatch" in msg or "expected" in msg):
        return "dim_mismatch"

    # --- TOAST / payload-size limit -------------------------------------
    if "toast" in msg or "row is too big" in msg or "payload too large" in msg:
        return "toast_limit"
    if status == 413:
        return "toast_limit"

    # --- RLS / auth -----------------------------------------------------
    if "row-level security" in msg or "rls" in msg or "permission denied" in msg:
        return "rls_denied"
    if status == 401 or status == 403:
        return "rls_denied"

    # --- Pool / connection timeout --------------------------------------
    if "pool" in msg and ("timeout" in msg or "exhausted" in msg):
        return "pool_timeout"
    if cls in {"PoolTimeout", "ConnectTimeout", "ReadTimeout", "TimeoutError"}:
        return "pool_timeout"
    if "timed out" in msg or "timeout" in msg:
        return "pool_timeout"

    # --- Supabase / PostgREST 5xx ---------------------------------------
    if status is not None and 500 <= status < 600:
        return "supabase_5xx"
    if "503" in msg or "502" in msg or "504" in msg or "500 internal" in msg:
        return "supabase_5xx"

    return "unknown"


__all__ = [
    "ERROR_KINDS",
    "ChunksWriteAudit",
    "ErrorKind",
    "classify_exception",
]


# ---------------------------------------------------------------------------
# Removed in S16-INGEST-02 (kept here as a tombstone so future readers can
# trace the change without spelunking git):
#
#   classify_partial_batch(attempted, succeeded) -> "partial_batch" | "none"
#
# Removed because `SessionStore.insert_chunks` is now transactional with
# `ON CONFLICT (source_id, chunk_index) DO NOTHING`. A short-write outcome
# without an exception is no longer reachable: either the whole batch
# commits or it raises. The `partial_batch` ErrorKind was dropped from
# the SQL CHECK in migration 20260502004217. Pattern A simplification.
# ---------------------------------------------------------------------------
