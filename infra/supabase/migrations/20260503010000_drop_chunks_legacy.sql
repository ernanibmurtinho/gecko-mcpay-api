-- 20260503010000_drop_chunks_legacy.sql
-- Purpose: S19-S3 second stage — DROP the legacy Supabase chunk store now
--          that S18 cutover is live, gecko-mcp doctor is green, and the
--          user explicitly waived the soak window (no real users on the
--          legacy chunks; no rollback dependency).
-- Reversible: NO. This drops 23k chunk rows + 176 cache rows + 33 audit
--             rows + three RPC functions. The Mongo path
--             (gecko_rag.{chunks,chunk_embedding_cache,chunks_write_audit})
--             is the only chunk store after this lands.
-- Touches: chunks, chunk_embedding_cache, chunks_write_audit, plus the
--          match_chunks / match_chunks_windowed / match_chunks_hybrid RPCs
--          that reference those tables.
--
-- Pre-flight (already satisfied 2026-05-02):
--   1. GECKO_CHUNK_STORE=mongo in production env
--   2. gecko-mcp doctor green on chunk_store:mongo:*
--   3. ≥1 successful bb research run against Mongo
--   4. User waived the ≥1 day soak (explicit "go for it" 2026-05-02)

-- ---------------------------------------------------------------------------
-- (1) Drop RPCs first — they FROM-reference the tables.
-- ---------------------------------------------------------------------------
DROP FUNCTION IF EXISTS match_chunks_hybrid(uuid, vector, integer, integer);
DROP FUNCTION IF EXISTS match_chunks_windowed(vector, integer, uuid, integer);
DROP FUNCTION IF EXISTS match_chunks(uuid, vector, integer);

-- ---------------------------------------------------------------------------
-- (2) Drop tables. CASCADE handles any indexes / triggers / dependent views.
-- ---------------------------------------------------------------------------
DROP TABLE IF EXISTS chunks_write_audit CASCADE;
DROP TABLE IF EXISTS chunk_embedding_cache CASCADE;
DROP TABLE IF EXISTS chunks CASCADE;

-- After this migration, GECKO_CHUNK_STORE=supabase will fail loudly because
-- the chunks RPCs no longer exist. That's the intended end-state — no
-- rollback path other than restoring from a Supabase point-in-time backup.
