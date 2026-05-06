-- 20260506000000_session_status_interrupted.sql
-- Purpose: Allow sessions to land in an 'interrupted' terminal state when
--          the API container is shut down (ECS rolling deploy, autoscale)
--          mid-flight. Without this, a research workflow killed by SIGTERM
--          leaves its row stuck at status='generating' indefinitely and the
--          /sessions/{id}/result endpoint loops on 425 forever.
--
-- Reversible: yes (additive; widens the CHECK and adds an optional column).
-- Touches: sessions table only. No data movement; legacy rows keep their
--          existing status values, all of which remain valid.
--
-- Canonical Python enum: gecko_core.models.SessionStatus
--   Literal["pending", "indexing", "generating", "complete", "failed", "interrupted"]
-- Drift between this CHECK and the Literal is caught by
-- tests/test_session_status_consistency.py (Pattern A).
--
-- interrupted_reason is free-form text — today we only emit
-- "container_shutdown" but leaving it open lets us add granular reasons
-- (oom_kill, deploy_drain, ...) without another migration.

ALTER TABLE sessions
  DROP CONSTRAINT IF EXISTS sessions_status_check;

ALTER TABLE sessions
  ADD CONSTRAINT sessions_status_check
    CHECK (status IN ('pending', 'indexing', 'generating', 'complete', 'failed', 'interrupted'));

ALTER TABLE sessions
  ADD COLUMN IF NOT EXISTS interrupted_reason TEXT;
