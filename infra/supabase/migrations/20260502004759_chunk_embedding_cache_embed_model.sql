-- 20260502004759_chunk_embedding_cache_embed_model.sql
-- Purpose: S16-INGEST-03 — fingerprint the embed model on the cache PK so a
--          model change is a constraint *separation*, not silent corruption.
--          The 2026-04-30 dogfood diagnosis (docs/diagnostics/2026-05-01-
--          chunk-write-failures.md FM-2) called out that swapping
--          text-embedding-3-small → text-embedding-3-large would poison
--          the cache because the PK only covered (url_hash, chunk_index).
--          With this column on the PK, two rows for the same chunk under
--          different models coexist and the lookup filters by the active
--          model.
-- Reversible: yes, but you'd lose the model fingerprint on existing
--             rows. Re-up the migration if rolled back.
-- Touches: chunk_embedding_cache (new column + PK rotation + backfill).

-- ---------------------------------------------------------------------------
-- (1) Add the column with the legacy default so the backfill is implicit.
-- ---------------------------------------------------------------------------
ALTER TABLE chunk_embedding_cache
  ADD COLUMN IF NOT EXISTS embed_model TEXT NOT NULL
    DEFAULT 'text-embedding-3-small';

-- The DEFAULT only fires on rows inserted post-ALTER; existing rows get
-- the same value because Postgres rewrites them with the column default.
-- Belt-and-braces: explicit UPDATE for any row that somehow holds NULL
-- (would only happen if the DEFAULT were dropped before backfill, which
-- it isn't, but defensive).
UPDATE chunk_embedding_cache
   SET embed_model = 'text-embedding-3-small'
 WHERE embed_model IS NULL;

-- ---------------------------------------------------------------------------
-- (2) Rotate the PK to include the model. Two rows for the same chunk
--     under different models can now coexist; the lookup filters on the
--     active model from IngestionSettings().embed_model.
-- ---------------------------------------------------------------------------
ALTER TABLE chunk_embedding_cache
  DROP CONSTRAINT chunk_embedding_cache_pkey;

ALTER TABLE chunk_embedding_cache
  ADD CONSTRAINT chunk_embedding_cache_pkey
    PRIMARY KEY (url_hash, chunk_index, embed_model);

-- Read pattern: pipeline._process_one issues
--   SELECT chunk_index, embedding, embed_model
--   FROM chunk_embedding_cache
--   WHERE url_hash = $1 AND embed_model = $2 AND chunk_index = ANY($3)
-- The new PK index covers it.
--
-- Prefix-only lookups by (url_hash, chunk_index) — none today, but if a
-- future cross-model dedup admin tool wants one, add a separate index
-- then; we don't pre-create indexes "just in case".

COMMENT ON COLUMN chunk_embedding_cache.embed_model IS
  'S16-INGEST-03 — model fingerprint. PK includes this so two rows for '
  'the same chunk under different embed models coexist. Source of truth '
  'for the value: gecko_core.ingestion.settings.IngestionSettings.embed_model.';
