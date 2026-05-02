-- 20260501150000_bazaar_spend_ledger.sql
-- Purpose: persistent ledger for outbound x402 (Bazaar buyer) spend.
--          Backs S16-BAZAAR-CONSUMER-02 daily + per-session caps so a
--          crash mid-session can't lose accounting and concurrent
--          ``bb research`` invocations can't double-spend the daily cap.
-- Reversible: yes (drops the table — pure accounting + observability,
--             no business data the rest of the codebase depends on).
-- Touches: new table ``bazaar_spend_ledger``. No existing rows changed.
--
-- Mode set canonical source (Pattern A in CLAUDE.md):
--   gecko_core.payments.modes.ConsumerMode  -> ('stub', 'live', 'cdp')
-- Adding a value = touch exactly one Python file (modes.py) + one
-- migration (a new file that ALTERs the CHECK below). Never edit this
-- file in place.

CREATE TABLE IF NOT EXISTS bazaar_spend_ledger (
  id              UUID NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
  session_id      UUID NOT NULL,
  resource_url    TEXT NOT NULL,
  amount_usd      NUMERIC(10, 4) NOT NULL,
  tx_hash         TEXT,
  network         TEXT NOT NULL DEFAULT '',
  consumer_mode   TEXT NOT NULL DEFAULT 'stub',
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

  CONSTRAINT bazaar_spend_ledger_amount_nonneg CHECK (amount_usd >= 0),
  CONSTRAINT bazaar_spend_ledger_consumer_mode_check CHECK (
    consumer_mode IN ('stub', 'live', 'cdp')
  )
);

-- Read pattern A: doctor + cap pre-flight roll up today's spend by the
-- ``created_at >= today_utc_midnight`` filter.
CREATE INDEX IF NOT EXISTS bazaar_spend_ledger_created_at_idx
  ON bazaar_spend_ledger (created_at DESC);

-- Read pattern B: per-session debug — sum the spend belonging to one
-- research run.
CREATE INDEX IF NOT EXISTS bazaar_spend_ledger_session_created_at_idx
  ON bazaar_spend_ledger (session_id, created_at DESC);

-- Idempotency defense: a real on-chain ``tx_hash`` is unique. Stub-mode
-- tx hashes (``stub-...``) are intentionally excluded so dev runs can
-- replay without conflict, and rows with NULL tx_hash (settle returned
-- no signature) are also excluded so we don't reject legitimate retries
-- against a facilitator that skipped the signature.
CREATE UNIQUE INDEX IF NOT EXISTS bazaar_spend_ledger_tx_hash_unique
  ON bazaar_spend_ledger (tx_hash)
  WHERE tx_hash IS NOT NULL AND tx_hash NOT LIKE 'stub-%';

-- Service-role only. Buyer accounting must never reach the anon /
-- gecko-mcpay-app surface — it's leaked treasury data.
ALTER TABLE bazaar_spend_ledger ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS bazaar_spend_ledger_no_anon ON bazaar_spend_ledger;
CREATE POLICY bazaar_spend_ledger_no_anon
  ON bazaar_spend_ledger
  FOR ALL
  TO anon
  USING (false)
  WITH CHECK (false);

COMMENT ON TABLE bazaar_spend_ledger IS
  'S16-BAZAAR-CONSUMER-02 — outbound x402 spend ledger backing the daily '
  '+ per-session caps in BazaarSourceProvider. consumer_mode set mirrors '
  'gecko_core.payments.modes.ConsumerMode. Service-role only; never '
  'expose to anon / web app.';
