-- Adds freshness_tier to chunks. Mirrors gecko_core.sources.types.FreshnessTier.
-- Defaults existing rows to 'static' (preserves current retrieval behavior).
-- Pattern A: any addition here must update FreshnessTier literal in the same commit.
--
-- IMPORTANT: chunks table was dropped in Sprint 19 (see 20260503010000_drop_chunks_legacy.sql).
-- This file is a Pattern A drift contract — the literals 'static' / 'daily' / 'live_only'
-- are read by tests/test_provider_kind_consistency.py to verify FreshnessTier stays in sync.
-- The DDL is wrapped in IF EXISTS so the file is a no-op when applied (chunks lives in Mongo Atlas now).

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'chunks') THEN
    ALTER TABLE chunks
      ADD COLUMN IF NOT EXISTS freshness_tier text NOT NULL DEFAULT 'static';

    ALTER TABLE chunks
      DROP CONSTRAINT IF EXISTS chunks_freshness_tier_check;

    ALTER TABLE chunks
      ADD CONSTRAINT chunks_freshness_tier_check
      CHECK (freshness_tier IN ('static', 'daily', 'live_only'));

    CREATE INDEX IF NOT EXISTS chunks_freshness_tier_idx ON chunks (freshness_tier);
  END IF;
END $$;
