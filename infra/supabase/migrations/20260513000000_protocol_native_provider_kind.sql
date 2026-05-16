-- 20260513000000_protocol_native_provider_kind.sql
-- Purpose: forward-migrate sources_provider_kind_check to admit the new
--          `protocol_native` ProviderKind value (S26 #14 — direct
--          protocol-API ingest for Kamino, Drift, Jupiter, Jito, Sanctum
--          vault-params / market manifests, replacing the empty-body
--          paysh_live chunks that were dragging citation_relevance to
--          0.35 in the rubric eval).
-- Reversible: yes — drop the constraint and re-add the prior list (only
--             safe if every protocol_native row in `sources` is also
--             removed; otherwise rollback raises 23514 itself).
-- Touches: sources.provider_kind CHECK (live, if the row exists). The
--          chunks table itself lives in Mongo Atlas
--          (memory/project_supabase_chunks_dropped_2026_05_08); this
--          file is the Python-side drift contract for ProviderKind only.
--
-- Source of truth: gecko_core.sources.types.ProviderKind.
-- Drift-guarded by: packages/gecko-core/tests/test_provider_kind_consistency.py.
--
-- Why a new ProviderKind (not "reuse paysh_live"):
--   paysh_live = per-request paid x402 retrieval against a pay.sh-listed
--   provider. The cost lands on a USDC ledger entry per call. Chunks
--   were ingested live during sessions.
--   protocol_native = free, public protocol API content ingested ONCE
--   into the corpus (Kamino markets JSON, Drift DLOB config, Jupiter
--   stats, Jito tip floor history, Sanctum router state). No payment;
--   no per-session re-fetch; persisted with freshness_tier='daily' for
--   the panel to cite across every Kamino/Drift/... question.
--
-- Retrieval admittance: protocol_native chunks are treated like
-- paysh_live in `_apply_retrieval_boosts` — +0.10 PROVIDER_SPECIFIC
-- boost on top of the +0.15 protocol-match boost. See
-- packages/gecko-core/src/gecko_core/orchestration/trade_panel/__init__.py.

ALTER TABLE IF EXISTS sources
  DROP CONSTRAINT IF EXISTS sources_provider_kind_check;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'sources') THEN
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
  END IF;
END $$;
