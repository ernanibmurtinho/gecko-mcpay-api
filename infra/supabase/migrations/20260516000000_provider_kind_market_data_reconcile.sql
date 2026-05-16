-- 20260516000000_provider_kind_market_data_reconcile.sql
-- Purpose: reconcile sources_provider_kind_check after the S33 merge.
--          The branch's 20260514 migration redefined the CHECK from a
--          list that omitted 'market_data' (the branch's ProviderKind
--          Literal had dropped it); merging origin/main re-introduced
--          'market_data' on the Python side. This migration restores the
--          SQL side of the Pattern A contract so the latest CHECK matches
--          gecko_core.sources.types.ProviderKind exactly: the full 21
--          values incl. both 'market_data' (S24 WS-A) and
--          'protocol_native' (S26 #14 / S31-#50).
-- Reversible: yes (drop + re-add the prior 20-value list) — but don't ship
--             rollback unless every market_data row in `sources` is
--             reclassified or deleted, else rollback raises 23514 itself.
-- Touches: sources.provider_kind CHECK only.
--
-- chunks table is intentionally not touched — chunks live in Mongo Atlas
-- since 2026-05-08 (memory/project_supabase_chunks_dropped_2026_05_08).
-- The Python ProviderKind Literal is the source of truth, shared by the
-- Mongo writer and the Postgres `sources` provenance row; admitting both
-- values on the surviving CHECK keeps the drift test
-- (tests/test_provider_kind_consistency.py) green.

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
    'market_data',
    'protocol_native'
  ));

COMMENT ON COLUMN sources.provider_kind IS
  'Mirrors gecko_core.sources.types.ProviderKind.';
