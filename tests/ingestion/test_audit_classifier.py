"""S16-INGEST-01 — fault-injection unit tests for the audit classifier.

One test per `ErrorKind` literal (Pattern A from CLAUDE.md). The classifier
is the single point that maps real exceptions / partial-write outcomes to
the bucketed audit Literal — if any branch regresses, an `unknown` row
shows up in production and the `bb doctor --recent` rollup gets noisy.

Acceptance for the parent ticket: zero `unknown` rows when re-running the
4 ideas in `tests/eval/live_runs/2026-04-30-*.json`. We don't run live
mode here — the synthetic suite below is the gating proxy.
"""

from __future__ import annotations

from gecko_core.ingestion.audit import (
    ERROR_KINDS,
    ErrorKind,
    classify_exception,
)
from gecko_core.ingestion.exceptions import ChunkValidationError

# ---------------------------------------------------------------------------
# Lightweight stand-ins for the SDK exceptions we don't want to import. The
# classifier matches on class name + message + status_code attributes only.
# ---------------------------------------------------------------------------


class _Resp:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class RateLimitError(Exception):
    """Mimics openai.RateLimitError shape (class name match)."""


class PoolTimeout(Exception):
    """Mimics httpx.PoolTimeout shape (class name match)."""


class ReadTimeout(Exception):
    pass


class _HttpExc(Exception):
    def __init__(self, msg: str, status_code: int | None = None) -> None:
        super().__init__(msg)
        if status_code is not None:
            self.status_code = status_code
            self.response = _Resp(status_code)


# ---------------------------------------------------------------------------
# One test per ErrorKind value. Adding a new kind to the Literal must come
# with a new test below, otherwise the parametrize test at the bottom will
# fail.
# ---------------------------------------------------------------------------


def test_classify_none_is_unreachable_via_classify_exception() -> None:
    # S16-INGEST-02 removed `classify_partial_batch`. The "none" bucket
    # is now only set by the pipeline directly on the success path; the
    # classifier itself never returns "none" — it always sees a real
    # exception. Coverage for "none" is the success-path unit test in
    # test_audit_pipeline_emit.py::test_audit_emitted_on_clean_success.
    assert "none" in ERROR_KINDS


def test_classify_toast_limit_message() -> None:
    exc = _HttpExc("row is too big: size 16384 exceeds maximum")
    assert classify_exception(exc) == "toast_limit"


def test_classify_toast_limit_413_status() -> None:
    exc = _HttpExc("payload rejected", status_code=413)
    assert classify_exception(exc) == "toast_limit"


def test_classify_pool_timeout_class_name() -> None:
    exc = PoolTimeout("connection pool exhausted")
    assert classify_exception(exc) == "pool_timeout"


def test_classify_pool_timeout_message() -> None:
    exc = ReadTimeout("read timed out after 30s")
    assert classify_exception(exc) == "pool_timeout"


def test_classify_rls_denied_message() -> None:
    exc = _HttpExc("permission denied: row-level security policy")
    assert classify_exception(exc) == "rls_denied"


def test_classify_rls_denied_403() -> None:
    exc = _HttpExc("forbidden", status_code=403)
    assert classify_exception(exc) == "rls_denied"


def test_classify_embedding_null_via_rate_limit() -> None:
    # Embedder retry exhaustion (FM-3) — terminal failure that would
    # leave embedding NULL on insert.
    exc = RateLimitError("rate limit exceeded for tokens")
    assert classify_exception(exc) == "embedding_null"


def test_classify_embedding_null_explicit_message() -> None:
    exc = ValueError("embedding is null for chunk 4")
    assert classify_exception(exc) == "embedding_null"


def test_classify_dim_mismatch() -> None:
    exc = _HttpExc("expected 1536 dimensions, got 768")
    assert classify_exception(exc) == "dim_mismatch"


def test_classify_dim_mismatch_vector_dimension() -> None:
    exc = _HttpExc("vector dimension mismatch in column embedding")
    assert classify_exception(exc) == "dim_mismatch"


def test_classify_supabase_5xx_status() -> None:
    exc = _HttpExc("upstream gateway error", status_code=503)
    assert classify_exception(exc) == "supabase_5xx"


def test_classify_supabase_5xx_message() -> None:
    exc = _HttpExc("502 Bad Gateway")
    assert classify_exception(exc) == "supabase_5xx"


def test_classify_unknown_falls_through() -> None:
    exc = ValueError("something nobody planned for")
    assert classify_exception(exc) == "unknown"


def test_classify_chunk_validation_empty_text() -> None:
    """S16-INGEST-02 — pre-flight rejected an empty chunk. The audit
    histogram should bucket this as `embedding_null` (text exists but
    the chunk has no useful content for the embedder)."""
    exc = ChunkValidationError("chunk_index=2 has empty text", kind="empty_text")
    assert classify_exception(exc) == "embedding_null"


def test_classify_chunk_validation_dim_mismatch() -> None:
    """S16-INGEST-02 — pre-flight rejected a wrong-dim embedding (e.g.
    poisoned cache after a model swap). The audit row points at FM-2."""
    exc = ChunkValidationError("chunk_index=0 dim 768 != 1536", kind="dim_mismatch")
    assert classify_exception(exc) == "dim_mismatch"


# ---------------------------------------------------------------------------
# Coverage guard — every value in ERROR_KINDS must be reachable through one
# of the tests above. If you add a new bucket, add a test for it.
# ---------------------------------------------------------------------------


_COVERED: set[ErrorKind] = {
    "none",
    "toast_limit",
    "pool_timeout",
    "rls_denied",
    "embedding_null",
    "dim_mismatch",
    "supabase_5xx",
    "unparseable_pdf",
    "unknown",
}


def test_classify_unparseable_pdf() -> None:
    """S17-INGEST-FALLBACK-01 — UnparseablePDFError propagates through
    the chunk-write path classifier as `unparseable_pdf` so the pipeline
    can record a clean info-level skip with the right audit bucket."""
    from gecko_core.sources.pdf import UnparseablePDFError

    exc = UnparseablePDFError(
        "https://example.com/broken.pdf",
        [
            ("pypdfium2", "PdfiumError"),
            ("pdfminer", "PDFSyntaxError"),
            ("pypdf", "PdfReadError"),
        ],
    )
    assert classify_exception(exc) == "unparseable_pdf"


def test_every_error_kind_has_a_test() -> None:
    """Pattern-A guard: schema-drift bites here too. If the Literal grows,
    this fails until the new bucket gets explicit coverage above."""
    assert set(ERROR_KINDS) == _COVERED, (
        f"ErrorKind drifted: covered={_COVERED} actual={set(ERROR_KINDS)}. "
        "Add a test_classify_<new_kind>_* function above and add the kind "
        "to _COVERED."
    )
