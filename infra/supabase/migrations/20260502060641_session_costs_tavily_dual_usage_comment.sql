-- 20260502060641_session_costs_tavily_dual_usage_comment.sql
-- Purpose: S17-INGEST-FALLBACK-01 — annotate `sessions.cost_tavily_usd`
--          to document its dual-source usage. The column accumulates BOTH:
--             1. Tavily Search/Discovery spend (the original wedge), and
--             2. Tavily Extract spend used as the bot-wall fallback after
--                the in-process httpx retry chain exhausts.
--          We chose to reuse one column rather than split — both line items
--          are the same vendor (Tavily), are billed against the same API
--          key, and are economically interchangeable from the per-session
--          dashboard's POV. Splitting them would require a new column,
--          a new ledger function, and a new aggregate view for marginal
--          benefit (we already filter by URL-hash + session in logs to
--          attribute per-call when debugging).
-- Reversible: trivial (COMMENT only).
-- Touches: session_costs (column comment only — no schema change).

COMMENT ON COLUMN sessions.cost_tavily_usd IS
  'Cumulative USD spend with Tavily for the session. Includes both '
  'discovery (search) calls AND extract-fallback calls invoked when the '
  'in-process httpx retry chain in gecko_core.ingestion.web exhausts '
  '(S17-INGEST-FALLBACK-01). Per-call attribution lives in structured '
  'logs (search vs extract); the column itself is the economic total.';
