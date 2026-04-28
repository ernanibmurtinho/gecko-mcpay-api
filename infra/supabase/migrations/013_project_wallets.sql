-- 013_project_wallets.sql
-- Purpose: per-project Privy embedded-wallet isolation (Sprint 2 S2-05/06).
--          Each project gets its own Privy v2 wallet at creation time so
--          spend is cryptographically bounded per project, not just policy-
--          bounded at the user level. `budget_cap_usd` is the human-facing
--          source of truth; `spent_usd` is a rolled-up running total updated
--          by the API on session completion.
-- Reversible: yes (drops the four new columns + two indexes).
-- Touches: table `projects` (additive; columns nullable or defaulted).
--
-- Coexistence with v1 columns: migration 20260428000000_projects.sql shipped
-- `wallet_address` + `wallet_provider` as forward-compat seats. We keep
-- those (existing callers read them) and add `privy_wallet_id` +
-- `privy_wallet_address` as the v2 fields. When S2-05 lands, new projects
-- write to BOTH (`wallet_address` = `privy_wallet_address`,
-- `wallet_provider` = 'privy-direct') so legacy reads keep working.
--
-- Trigger NOT included here on purpose — auto-incrementing `spent_usd` from
-- session_costs depends on the data flow web3-engineer is wiring this
-- sprint (which line items count, when to debounce, refund handling).
-- Ship the trigger as a separate migration once enforcement lands.
--
-- Lazy provisioning: `privy_wallet_id` is nullable. Existing projects do not
-- get backfilled — they receive a wallet on the next paid call.

ALTER TABLE projects
  ADD COLUMN IF NOT EXISTS privy_wallet_id      TEXT,
  ADD COLUMN IF NOT EXISTS privy_wallet_address TEXT,
  ADD COLUMN IF NOT EXISTS budget_cap_usd       NUMERIC(10, 2) NOT NULL DEFAULT 5.00,
  ADD COLUMN IF NOT EXISTS spent_usd            NUMERIC(10, 2) NOT NULL DEFAULT 0;

-- Privy wallet IDs must be globally unique across projects (one wallet
-- per project). Partial index so projects pre-provisioning don't collide
-- on NULL.
CREATE UNIQUE INDEX IF NOT EXISTS idx_projects_privy_wallet_id
  ON projects (privy_wallet_id)
  WHERE privy_wallet_id IS NOT NULL;

-- Hot path for budget enforcement: "is this project at/over its cap?".
-- Partial index keeps the working set tiny (only over-budget projects)
-- so the lookup is effectively O(1) on the worst-case query.
CREATE INDEX IF NOT EXISTS idx_projects_over_budget
  ON projects (id)
  WHERE spent_usd >= budget_cap_usd;

COMMENT ON COLUMN projects.privy_wallet_id IS
  'Privy v2 wallet ID. Nullable until provisioning lands (S2-05); after that, every project has one.';
COMMENT ON COLUMN projects.privy_wallet_address IS
  'Solana address of the project''s Privy embedded wallet. Mirrors `wallet_address` for projects on the privy-direct provider.';
COMMENT ON COLUMN projects.budget_cap_usd IS
  'Hard spend cap. The wallet enforces cryptographically; this column is the human-facing source of truth.';
COMMENT ON COLUMN projects.spent_usd IS
  'Rolled-up sum of session_costs.cost_usd for this project. Updated by a trigger or by the API on session completion.';

-- Atomic incrementer for `spent_usd`. Distinct from the auto-increment
-- TRIGGER deliberately deferred to the web3-engineer's Sprint 2 follow-up:
-- this RPC is the API-driven path the store helper calls today, so two
-- concurrent session-completion handlers can't race on a
-- read-modify-write. Negative deltas are allowed (refund flows).
-- Returns the new spent_usd or NULL if the project doesn't exist /
-- is soft-deleted (caller raises).
CREATE OR REPLACE FUNCTION gecko_increment_project_spent(
  p_project_id UUID,
  p_delta_usd  NUMERIC
) RETURNS NUMERIC
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_new_spent NUMERIC;
BEGIN
  UPDATE projects
     SET spent_usd = spent_usd + p_delta_usd
   WHERE id = p_project_id
     AND deleted_at IS NULL
  RETURNING spent_usd INTO v_new_spent;

  RETURN v_new_spent;
END $$;

GRANT EXECUTE ON FUNCTION gecko_increment_project_spent(UUID, NUMERIC) TO service_role;
