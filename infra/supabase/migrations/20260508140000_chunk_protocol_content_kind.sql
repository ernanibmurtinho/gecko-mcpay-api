-- 20260508140000_chunk_protocol_content_kind.sql
-- Purpose: add protocol (text[]), content_kind (text), is_stale (bool) to
--          chunks for the trading-oracle vertical. Pattern A: the
--          content_kind CHECK mirrors gecko_core.sources.types.ContentKind.
-- Reversible: yes (drops three new columns + three indexes + one CHECK).
--
-- IMPORTANT: chunks table was dropped in Sprint 19 (see 20260503010000_drop_chunks_legacy.sql).
-- This file is a Pattern A drift contract — the literals 'quote' / 'mechanism' / 'governance' / 'unknown'
-- are read by tests/test_provider_kind_consistency.py to verify ContentKind stays in sync.
-- The DDL is wrapped in IF EXISTS so the file is a no-op when applied (chunks lives in Mongo Atlas now).
--
-- All defaults preserve existing chunks (protocol=[], content_kind='unknown',
-- is_stale=false) so existing retrieval is unchanged.

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'chunks') THEN
    ALTER TABLE chunks
      ADD COLUMN IF NOT EXISTS protocol text[] NOT NULL DEFAULT '{}';

    ALTER TABLE chunks
      ADD COLUMN IF NOT EXISTS content_kind text NOT NULL DEFAULT 'unknown';

    ALTER TABLE chunks
      ADD COLUMN IF NOT EXISTS is_stale boolean NOT NULL DEFAULT false;

    ALTER TABLE chunks
      DROP CONSTRAINT IF EXISTS chunks_content_kind_check;

    ALTER TABLE chunks
      ADD CONSTRAINT chunks_content_kind_check
      CHECK (content_kind IN ('quote', 'mechanism', 'governance', 'unknown'));

    CREATE INDEX IF NOT EXISTS chunks_protocol_gin ON chunks USING GIN (protocol);
    CREATE INDEX IF NOT EXISTS chunks_content_kind_idx ON chunks (content_kind);
    CREATE INDEX IF NOT EXISTS chunks_is_stale_idx ON chunks (is_stale);
  END IF;
END $$;

-- ---------------------------------------------------------------------------
-- ATLAS SEARCH INDEX UPDATE (manual operator action)
--
-- The Mongo Atlas Search index `chunks_vector` must be updated to add
-- protocol, content_kind, is_stale as filter-type fields. Without this,
-- queries like {vertical:"...", protocol:"jupiter"} will fall back to
-- collection-scan filtering (slow + expensive at scale).
--
-- Operator action via Atlas UI or API (PATCH index definition):
--
-- definition.fields += [
--   {"type": "filter", "path": "protocol"},
--   {"type": "filter", "path": "content_kind"},
--   {"type": "filter", "path": "is_stale"},
-- ]
--
-- This is non-blocking for the trading-oracle ingest — the ingest will
-- still write correctly; only retrieval performance degrades until the
-- index is updated. Operator should run this WITHIN 24h of ingest.
-- ---------------------------------------------------------------------------
