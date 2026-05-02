-- S17-TONE-01 — rename single-token verdict vocabulary surfaced to founders.
--   KILL  -> PIVOT  (same semantics: don't build as-is, framed as a redirect)
--   BUILD -> GO     (same semantics: greenlight, more energizing)
--   REFINE          (unchanged)
--
-- The canonical Python enum lives in
-- ``packages/gecko-core/src/gecko_core/models.py::Verdict`` (S17-TONE-01).
-- That enum is the single source of truth — this migration mirrors the
-- rename into the JSONB-persisted ResearchResult shape on
-- ``sessions.result_json``.
--
-- Affected storage:
--   * ``sessions.result_json -> 'verdict'``  (text inside JSONB)
--   * No CHECK constraint touches the new vocabulary — the precedent
--     verdict column (``gecko_precedent.verdict``) uses the legacy v1
--     ship/kill/pivot taxonomy via ``workflows._detect_research_verdict``,
--     so it is unaffected by this rename.
--
-- The rename is paired with a ``Verdict._missing_`` shim in Python so any
-- row written by an older deploy mid-rollout (or by an external SDK
-- consumer pinned to the old enum) still deserializes correctly. Running
-- this migration eliminates the legacy values from clean read paths;
-- the shim is defense in depth.
--
-- Idempotent: WHERE clauses skip rows that have already been migrated, so
-- replays are safe.

begin;

-- Top-level result.verdict (the structured S11 single-token surface).
update sessions
set result_json = jsonb_set(
    result_json,
    '{verdict}',
    to_jsonb('PIVOT'::text),
    false
)
where result_json is not null
  and result_json ? 'verdict'
  and result_json->>'verdict' = 'KILL';

update sessions
set result_json = jsonb_set(
    result_json,
    '{verdict}',
    to_jsonb('GO'::text),
    false
)
where result_json is not null
  and result_json ? 'verdict'
  and result_json->>'verdict' = 'BUILD';

commit;
