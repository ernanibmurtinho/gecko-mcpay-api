-- 20260502000000_provider_kind.sql
-- Purpose: S17-WEDGE-DATA-01 — make `provider_kind` a first-class column on
--          chunks and sources, and surface it through the match_chunks /
--          match_chunks_windowed RPCs so retrieval can attribute every chunk
--          to its provider (web / youtube / bazaar / arxiv / twitsh / hn /
--          reddit / gecko_precedent). Path B from
--          docs/strategy/2026-05-02-wedge-wire-path-b-design.md.
-- Reversible: yes (additive columns + index + RPC redefinition).
--             DROP COLUMN provider_kind on both tables; restore the prior
--             RPC definitions from 20260425000200_rag_match.sql and
--             20260501110000_chunks_temporal.sql.
-- Touches: chunks (new column + CHECK + index), sources (new column + CHECK,
--          extends sources.type CHECK to include 'provider'),
--          match_chunks RPC, match_chunks_windowed RPC.
--
-- Mirrors gecko_core.sources.types.ProviderKind. Drift between this CHECK
-- and the Python Literal is caught by tests/test_provider_kind_consistency.py
-- (Pattern A — same shape as test_payment_mode_consistency.py).
--
-- Backfill: NOT NULL DEFAULT 'web' covers every existing row in one
-- statement. All chunks today are Tavily web; the default is correct.

-- ---------------------------------------------------------------------------
-- chunks.provider_kind
-- ---------------------------------------------------------------------------

ALTER TABLE chunks
  ADD COLUMN provider_kind TEXT NOT NULL DEFAULT 'web';

ALTER TABLE chunks
  ADD CONSTRAINT chunks_provider_kind_check
  CHECK (provider_kind IN (
    'web','youtube','bazaar','arxiv','twitsh','hn','reddit','gecko_precedent'
  ));

COMMENT ON COLUMN chunks.provider_kind IS
  'Mirrors gecko_core.sources.types.ProviderKind. Drift caught by tests/test_provider_kind_consistency.py.';

-- Per-provider grouping queries (e.g. "how many bazaar chunks did this
-- session retrieve?") scan the column directly. Cheap btree index — the
-- cardinality is bounded by the Literal's arity (8 today).
CREATE INDEX chunks_provider_kind_idx ON chunks (provider_kind);

-- ---------------------------------------------------------------------------
-- sources.provider_kind  +  extend sources.type CHECK to allow 'provider'
-- ---------------------------------------------------------------------------

ALTER TABLE sources
  ADD COLUMN provider_kind TEXT NOT NULL DEFAULT 'web';

ALTER TABLE sources
  ADD CONSTRAINT sources_provider_kind_check
  CHECK (provider_kind IN (
    'web','youtube','bazaar','arxiv','twitsh','hn','reddit','gecko_precedent'
  ));

COMMENT ON COLUMN sources.provider_kind IS
  'Mirrors gecko_core.sources.types.ProviderKind.';

-- The original sources.type CHECK was inline on the column
-- (init.sql line 27): CHECK (type IN ('youtube', 'web')). Postgres named
-- it sources_type_check. Drop and re-add to extend with 'provider' for the
-- Bazaar/Arxiv/twit.sh synthetic source rows (per design memo §1.4).
ALTER TABLE sources
  DROP CONSTRAINT IF EXISTS sources_type_check;

ALTER TABLE sources
  ADD CONSTRAINT sources_type_check
  CHECK (type IN ('youtube', 'web', 'provider'));

-- ---------------------------------------------------------------------------
-- match_chunks RPC — surface provider_kind
-- ---------------------------------------------------------------------------
-- Original definition: 20260425000200_rag_match.sql.
-- Only change: add c.provider_kind to RETURNS TABLE and SELECT. Pre-filter
-- on session_id is unchanged. ai-ml-engineer's per-provider score boosts
-- run on the Python side post-RPC (design memo §1.5).
--
-- Postgres rule: CREATE OR REPLACE FUNCTION cannot change the RETURNS TABLE
-- shape — adding `provider_kind` is a shape change. DROP first, then create.

DROP FUNCTION IF EXISTS match_chunks(uuid, vector, integer);
DROP FUNCTION IF EXISTS match_chunks(vector, integer, uuid);
DROP FUNCTION IF EXISTS match_chunks_windowed(uuid, vector, integer, integer);
DROP FUNCTION IF EXISTS match_chunks_windowed(vector, integer, uuid, integer);

CREATE OR REPLACE FUNCTION match_chunks(
  p_session_id    UUID,
  query_embedding VECTOR(1536),
  match_count     INT DEFAULT 8
)
RETURNS TABLE (
  id            UUID,
  source_id     UUID,
  source_url    TEXT,
  chunk_index   INT,
  text          TEXT,
  similarity    FLOAT,
  provider_kind TEXT
)
LANGUAGE sql STABLE AS $$
  SELECT
    c.id,
    c.source_id,
    s.url AS source_url,
    c.chunk_index,
    c.text,
    1 - (c.embedding <=> query_embedding) AS similarity,
    c.provider_kind
  FROM chunks c
  JOIN sources s ON s.id = c.source_id
  WHERE c.session_id = p_session_id
  ORDER BY c.embedding <=> query_embedding
  LIMIT match_count;
$$;

-- ---------------------------------------------------------------------------
-- match_chunks_windowed RPC — surface provider_kind
-- ---------------------------------------------------------------------------
-- Original definition: 20260501110000_chunks_temporal.sql.
-- Same additive change. captured_at + project_id pre-filter shape preserved.

CREATE OR REPLACE FUNCTION match_chunks_windowed(
  query_embedding VECTOR(1536),
  window_days     INTEGER,
  p_project_id    UUID,
  match_count     INT DEFAULT 8
)
RETURNS TABLE (
  id            UUID,
  source_id     UUID,
  source_url    TEXT,
  chunk_index   INT,
  text          TEXT,
  captured_at   TIMESTAMPTZ,
  similarity    FLOAT,
  provider_kind TEXT
)
LANGUAGE sql STABLE AS $$
  SELECT
    c.id,
    c.source_id,
    s.url AS source_url,
    c.chunk_index,
    c.text,
    c.captured_at,
    1 - (c.embedding <=> query_embedding) AS similarity,
    c.provider_kind
  FROM chunks c
  JOIN sources s ON s.id = c.source_id
  WHERE (p_project_id IS NULL OR c.project_id = p_project_id)
    AND (
      window_days IS NULL
      OR window_days <= 0
      OR c.captured_at >= now() - make_interval(days => window_days)
    )
  ORDER BY c.embedding <=> query_embedding
  LIMIT match_count;
$$;

GRANT EXECUTE ON FUNCTION match_chunks_windowed(VECTOR(1536), INTEGER, UUID, INT) TO service_role;
GRANT EXECUTE ON FUNCTION match_chunks_windowed(VECTOR(1536), INTEGER, UUID, INT) TO authenticated;
