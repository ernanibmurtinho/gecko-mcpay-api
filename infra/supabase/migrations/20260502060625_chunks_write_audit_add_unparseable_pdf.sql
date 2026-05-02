-- 20260502060625_chunks_write_audit_add_unparseable_pdf.sql
-- Purpose: S17-INGEST-FALLBACK-01 — add `unparseable_pdf` to the
--          `chunks_write_audit.error_kind` CHECK so the new bucket
--          (raised when pdfium → pdfminer → pypdf all fail) can be
--          recorded as a clean skip rather than an `unknown` lump.
-- Reversible: yes — drop+recreate the constraint without the new value;
--             rows carrying 'unparseable_pdf' would need to be rewritten
--             to 'unknown' first (mirror of the partial_batch backfill in
--             20260502004217).
-- Touches: chunks_write_audit (re-CHECK only).
--
-- error_kind canonical set (single source of truth — Pattern A in CLAUDE.md):
--   gecko_core.ingestion.audit.ErrorKind
-- Schema-drift test in
-- packages/gecko-core/tests/test_audit_error_kind_consistency.py enforces
-- equality between the SQL CHECK and the Python Literal.

ALTER TABLE chunks_write_audit
  DROP CONSTRAINT chunks_write_audit_error_kind_check;

ALTER TABLE chunks_write_audit
  ADD CONSTRAINT chunks_write_audit_error_kind_check CHECK (
    error_kind IN (
      'none',
      'toast_limit',
      'pool_timeout',
      'rls_denied',
      'embedding_null',
      'dim_mismatch',
      'supabase_5xx',
      'unparseable_pdf',
      'unknown'
    )
  );
