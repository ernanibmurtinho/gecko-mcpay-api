-- 012_x402_settlements_network.sql
-- Purpose: attribute each settled x402 tx to either Solana devnet or mainnet so
--          we can run the cutover canary (10% mainnet rollout) and answer
--          forensic questions like "which sessions actually settled on mainnet
--          during the rollout window?". Without this column, the only signal
--          we have is `sessions.x402_tx_signature` and we'd have to round-trip
--          to an explorer to disambiguate cluster.
-- Reversible: yes (drops the new column + index).
-- Touches: table `sessions` (the x402 settlement signature lives on the
--          session row — see migration 20260426000000_x402_tx_signature.sql).
--
-- Naming note: the Sprint 2 spec referenced `x402_settlements`, but this
-- repo never split out a settlements table — every x402 settle writes its
-- signature to `sessions.x402_tx_signature`. So `network` belongs on
-- `sessions` for now. If/when a dedicated `x402_settlements` table lands
-- (e.g. for multi-signature-per-session retries), backfill from here.
--
-- Backfill: every existing row predates mainnet, so DEFAULT 'solana-devnet'
-- is a one-time correctness convenience. Future inserts MUST specify the
-- column explicitly via the writer path; the default is retained so
-- migrations don't break on stub-mode rows that never touch the column.

ALTER TABLE sessions
  ADD COLUMN IF NOT EXISTS network TEXT NOT NULL DEFAULT 'solana-devnet'
    CHECK (network IN ('solana-devnet', 'solana-mainnet'));

-- Partial index — only rows with a real on-chain signature are interesting
-- for the canary forensic queries. Stub-mode rows (no x402_tx_signature)
-- still carry network='solana-devnet' but we don't pay to index them.
CREATE INDEX IF NOT EXISTS idx_sessions_network
  ON sessions (network)
  WHERE x402_tx_signature IS NOT NULL;

COMMENT ON COLUMN sessions.network IS
  'Solana cluster the x402 tx settled on. CHECK enforces only the two valid '
  'CAIP-2-friendly values; expand if we ever onboard a third network. '
  'Defaulted to solana-devnet for legacy rows that predate the cutover canary.';
