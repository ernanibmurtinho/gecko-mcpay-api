-- 20260430052216_chunk_embedding_cache.sql
-- Purpose: cross-session embedding dedup (S8-INGEST-03). Stops re-paying OpenAI
--          when the same URL is ingested in a later session — the dogfood matrix
--          re-runs the same Tavily-discovered URLs across 5 ideas + retries.
-- Reversible: yes (table is a pure cache; safe to drop).
-- Touches: new table `chunk_embedding_cache`.
-- Embedding model assumption: text-embedding-3-small / 1536 dim. If the default
--          embed model changes, ship a follow-up migration that wipes this cache
--          (see ingestion/embedder.py — _EMBED_RATES_USD_PER_1M).

CREATE TABLE IF NOT EXISTS chunk_embedding_cache (
  url_hash     TEXT NOT NULL,
  chunk_index  INT  NOT NULL,
  text         TEXT NOT NULL,
  embedding    VECTOR(1536) NOT NULL,
  cached_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (url_hash, chunk_index)
);

-- Read pattern: pipeline._process_one issues
--   SELECT chunk_index, embedding FROM chunk_embedding_cache
--   WHERE url_hash = $1 AND chunk_index = ANY($2)
-- The PRIMARY KEY index covers it; no extra index needed.

COMMENT ON TABLE chunk_embedding_cache IS
  'S8-INGEST-03 — content-addressed embedding cache keyed on (url_hash, chunk_index). '
  'Pure cache: safe to truncate. Wipe on embed-model change.';
