-- 011_waitlist.sql
-- Purpose: capture pre-launch email signups from geckovision.tech apex landing.
--          Write-only sink. RLS enabled with no policies, so only the
--          service role can read/write — the marketing landing's API route
--          uses the service role from a server-only env var.
-- Reversible: yes (drops table + index).
-- Touches: waitlist (new).
--
-- Design notes:
-- - email_lower is a generated column for case-insensitive uniqueness without
--   forcing canonicalization on insert. Reads/exports get the original casing.
-- - source defaults to 'apex_landing' for the marketing site; can be set to
--   'app_landing' or 'cli' later when other surfaces capture intent.
-- - referrer + user_agent help triage signups by funnel without coupling to
--   a separate analytics product. Both nullable (privacy-conscious clients
--   strip them).

CREATE TABLE waitlist (
  id            BIGSERIAL PRIMARY KEY,
  email         TEXT NOT NULL,
  email_lower   TEXT GENERATED ALWAYS AS (lower(email)) STORED,
  source        TEXT NOT NULL DEFAULT 'apex_landing',
  user_agent    TEXT,
  referrer      TEXT,
  ip_hash       TEXT,                -- optional sha256(ip + secret) for dedupe without storing IPs
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX idx_waitlist_email_lower ON waitlist (email_lower);
CREATE INDEX idx_waitlist_created_at ON waitlist (created_at DESC);

-- Defense in depth: RLS on, no policies. Only service role can read/write.
ALTER TABLE waitlist ENABLE ROW LEVEL SECURITY;

COMMENT ON TABLE waitlist IS
  'Pre-launch email capture from marketing surfaces. Service-role only — no anon access.';
COMMENT ON COLUMN waitlist.source IS
  'Funnel attribution: apex_landing | app_landing | cli | other';
COMMENT ON COLUMN waitlist.ip_hash IS
  'Optional sha256 of client IP + a server secret. Use for dedupe without retaining raw IPs.';
