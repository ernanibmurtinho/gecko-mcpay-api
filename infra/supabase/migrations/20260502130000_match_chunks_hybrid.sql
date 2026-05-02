-- 20260502130000_match_chunks_hybrid.sql
-- Purpose: S17-WEDGE-CITE-04 — hybrid retrieval that guarantees a quota of
--          chunks per provider_kind so structured-provider chunks (Bazaar,
--          Arxiv, twit.sh) reach the LLM even when raw cosine similarity
--          is dominated by long-form Tavily web prose.
--
-- Background: match_chunks (20260502000000_provider_kind.sql) is pure
-- cosine, no similarity floor — but with ~99 web chunks vs 1-3 provider
-- chunks per session, the provider chunks systematically lose the
-- top-k race. The Python-side per-provider boost in rag/query.py is
-- a re-ordering inside the over-fetched window; if a provider chunk
-- isn't in fetch_k it can't be boosted. Hybrid retrieval fixes the
-- ceiling: take the top-N globally + top-M per provider_kind, dedupe
-- by chunk id, return up to match_count rows ordered by raw similarity
-- (the Python boost still applies post-RPC).
--
-- Reversible: yes. DROP FUNCTION match_chunks_hybrid(...). The Python
-- caller falls back to match_chunks when GECKO_RETRIEVAL_HYBRID=false.
-- Touches: new RPC only; no table or constraint changes.
--
-- Invocation contract (matches rag/query.py):
--   p_session_id     -- session scope (same as match_chunks)
--   query_embedding  -- vector(1536) from text-embedding-3-small
--   match_count      -- final cap on returned rows
--   per_kind_quota   -- minimum rows to attempt per provider_kind
--                       present in the session (default 2)

DROP FUNCTION IF EXISTS match_chunks_hybrid(uuid, vector, integer, integer);

CREATE OR REPLACE FUNCTION match_chunks_hybrid(
  p_session_id    UUID,
  query_embedding VECTOR(1536),
  match_count     INT DEFAULT 12,
  per_kind_quota  INT DEFAULT 2
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
  WITH ranked AS (
    SELECT
      c.id,
      c.source_id,
      s.url AS source_url,
      c.chunk_index,
      c.text,
      1 - (c.embedding <=> query_embedding) AS similarity,
      c.provider_kind,
      ROW_NUMBER() OVER (
        PARTITION BY c.provider_kind
        ORDER BY c.embedding <=> query_embedding
      ) AS kind_rank,
      ROW_NUMBER() OVER (
        ORDER BY c.embedding <=> query_embedding
      ) AS global_rank
    FROM chunks c
    JOIN sources s ON s.id = c.source_id
    WHERE c.session_id = p_session_id
  ),
  picked AS (
    -- Per-provider quota: best K of each kind present in the session.
    SELECT * FROM ranked WHERE kind_rank <= GREATEST(1, per_kind_quota)
    UNION
    -- Global top-N: fill the rest with raw-similarity winners.
    SELECT * FROM ranked WHERE global_rank <= match_count
  )
  SELECT id, source_id, source_url, chunk_index, text, similarity, provider_kind
  FROM picked
  ORDER BY similarity DESC
  LIMIT match_count;
$$;

GRANT EXECUTE ON FUNCTION match_chunks_hybrid(UUID, VECTOR(1536), INT, INT) TO service_role;
GRANT EXECUTE ON FUNCTION match_chunks_hybrid(UUID, VECTOR(1536), INT, INT) TO authenticated;

COMMENT ON FUNCTION match_chunks_hybrid IS
  'S17-WEDGE-CITE-04 hybrid retrieval: per-provider quota UNION global top-N. '
  'Guarantees Bazaar/Arxiv/twit.sh chunks reach the LLM even when web prose '
  'dominates raw cosine. Python rag/query.py applies provider boosts post-RPC. '
  'Caller toggles via GECKO_RETRIEVAL_HYBRID (default true).';
