-- 20260509000000_provider_kind_marketplace.sql
-- Purpose: forward-migrate the sources_provider_kind_check constraint to
--          admit the marketplace ProviderKind values (paysh_manifest,
--          paysh_live, bazaar_manifest, bazaar_live) plus judge_corpus,
--          which the Python Literal in gecko_core.sources.types has
--          carried since S22/S23 but the SQL CHECK has not.
--
-- Discovered: 2026-05-09 dogfood. Every paysh_live / bazaar_live source-row
-- ingest has been silently failing 23514 against the live Supabase since
-- the marketplace dispatchers shipped. Mongo chunks land fine; only the
-- Postgres `sources` provenance row gets rejected, so the ingest pipeline
-- ran but provenance wasn't captured.
--
-- chunks table is intentionally not touched. Per
-- memory/project_supabase_chunks_dropped_2026_05_08, chunks live in Mongo
-- Atlas only since 2026-05-08; the Supabase chunks table no longer exists.
-- The Python ProviderKind Literal is shared between the Mongo writer and
-- the Postgres `sources` provenance row, so admitting the new values on
-- the surviving CHECK is enough to unblock end-to-end ingest.
--
-- Pattern A: this migration mirrors gecko_core.sources.types.ProviderKind
-- exactly. A drift-test for ProviderKind doesn't exist yet (only
-- PaymentMode has one); filed as a follow-up.
--
-- Reverse: drop the constraint and re-add the prior 9-value list. Don't
-- ship the rollback unless every paysh/bazaar/judge_corpus row is first
-- reclassified or deleted; otherwise rollback raises 23514 itself.

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
    'bazaar_live'
  ));

COMMENT ON COLUMN sources.provider_kind IS
  'Mirrors gecko_core.sources.types.ProviderKind.';
