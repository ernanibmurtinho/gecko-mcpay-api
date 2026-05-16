-- 20260512000000_market_data_provider_kind.sql
-- Purpose: forward-migrate sources_provider_kind_check to admit the new
--          `market_data` ProviderKind value (S24 WS-A — Pyth historical
--          + DefiLlama TVL grounding for the trade panel). Also extends
--          chunks_freshness_tier_check to admit the new 'hot' tier used
--          by market_data chunks (sub-hour cadence; persisted, not
--          live-only).
-- Reversible: yes — drop the new constraint and re-add the prior list.
--             Don't ship rollback unless every market_data row in
--             `sources` is reclassified or deleted; otherwise rollback
--             raises 23514 itself.
-- Touches: sources.provider_kind CHECK (live), chunks.freshness_tier
--          CHECK (drift-contract — chunks table itself lives in Mongo
--          Atlas, per memory/project_supabase_chunks_dropped_2026_05_08;
--          the IF EXISTS guard makes this a no-op on the live Postgres
--          while the Python-side drift test sees the canonical value list).
--
-- Source of truth: gecko_core.sources.types.ProviderKind +
--                  gecko_core.sources.types.FreshnessTier.
-- Drift-guarded by: packages/gecko-core/tests/test_provider_kind_consistency.py.
--
-- Why now: ai-ml-engineer 2026-05-12 review flagged that 3/7 trade-panel
-- voices (technical_analyst, sentiment_via_news_recency, fundamental_via_TVL)
-- structurally bluff without market data — the canon corpus is timeless
-- wisdom literature, useless to a chart-reading voice. WS-A wires Pyth
-- historical daily candles + DefiLlama TVL snapshots into the corpus
-- under provider_kind='market_data', freshness_tier='hot' so the panel
-- can cite real numbers instead of hallucinating price levels from
-- Howard Marks memos.

-- 1) Extend sources.provider_kind CHECK to admit 'market_data'.
ALTER TABLE sources
  DROP CONSTRAINT IF EXISTS sources_provider_kind_check;
ALTER TABLE sources
  ADD CONSTRAINT sources_provider_kind_check
  CHECK (provider_kind IN (
    'web',
    'youtube',
    'bazaar',
    'arxiv',
    'twitsh',
    'hn',
    'reddit',
    'gecko_precedent',
    'judge_corpus',
    'paysh_manifest',
    'paysh_live',
    'bazaar_manifest',
    'bazaar_live',
    'canon_marks',
    'canon_damodaran',
    'canon_mauboussin',
    'canon_youtube',
    'canon_berkshire',
    'canon_macro',
    'market_data'
  ));

COMMENT ON COLUMN sources.provider_kind IS
  'Mirrors gecko_core.sources.types.ProviderKind.';

-- 2) Extend chunks.freshness_tier CHECK to admit 'hot'.
--    chunks table is Mongo-only as of S19 — guard with IF EXISTS so this
--    file is a no-op against the live DB. The Python-side drift test
--    reads this file to verify FreshnessTier stays in sync.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'chunks') THEN
    ALTER TABLE chunks
      DROP CONSTRAINT IF EXISTS chunks_freshness_tier_check;
    ALTER TABLE chunks
      ADD CONSTRAINT chunks_freshness_tier_check
      CHECK (freshness_tier IN ('static', 'daily', 'live_only', 'hot'));
  END IF;
END $$;
