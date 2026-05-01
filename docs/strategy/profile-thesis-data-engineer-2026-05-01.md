# Profile Thesis — Data Engineer Lens (2026-05-01)

**Verdict:** thesis holds, but only if we stop pretending "one unified index" means one table. From the data lens this is a **three-tier composition** over what we already shipped, not a greenfield index.

## 1. Schema for the contributor profile registry

New `contributors` table — *do not* extend `gecko_precedent` (that's verdict-shaped, not contributor-shaped) and *do not* graft onto `chunk_embedding_cache` (that's content-addressed cache, profile is orthogonal).

```
contributors
  id              uuid pk
  wallet_address  text unique not null         -- 0x or solana pubkey, lowercased
  chain           text not null                -- 'base'|'solana'|'evm'|...
  display_handle  text                         -- self-claim, mutable
  reputation      jsonb not null default '{}'  -- denormalized counters (see §3)
  created_at      timestamptz, deleted_at timestamptz
```

Then the join surface — every cited chunk needs to link back to a contributor:

```
contributor_attributions
  chunk_id       uuid references chunks(id) on delete cascade
  contributor_id uuid references contributors(id)
  source_platform text not null                -- 'publish.new'|'paragraph'|'twitter'
  attested_at    timestamptz
  primary key (chunk_id, contributor_id)
```

This is the seam. `chunks` (init.sql) stays profile-agnostic; SourceProvider chunks from S12 keep working. Only chunks we *can* attribute get a row.

## 2. Profile typing — tag table, not enum

A fixed enum is wrong. Reality: a Paradigm partner is "investor + security researcher + judge"; a Vitalik post is "founder + protocol-design + judge". Use a tag table:

```
contributor_profile_tags
  contributor_id uuid, profile_type text, source text  -- 'self'|'derived'|'verified'
  confidence     real, primary key (contributor_id, profile_type, source)
```

Self-claims and graph-derived tags coexist with provenance. The classifier at `gecko_core.classify` writes `source='derived'`; the contributor writes `source='self'`; staff writes `source='verified'`.

## 3. Cited-precedent counter — aggregated view, refreshed on verdict-time

Counter column on `contributors` is tempting but races. Real shape: materialized view rolled by verdict emission, *not* by cite-time. Cite-time fires on every retrieval (noisy, untrustworthy). Verdict-time fires once per session, on `gecko_precedent` insert — that's the trust signal.

```
mv_contributor_citation_counts (contributor_id, verdict, outcome, n)
```

Refresh after each Pro session inside the same transaction that writes `gecko_precedent`. Composes cleanly with `precedent_outcomes` (20260430054542) — when an outcome flips from `unknown` → `shipped`, recount affected contributors only.

The "cited 47 times in DeFi BUILD verdicts that later shipped" claim is a single SELECT against the MV joined to `category_tags`.

## 4. Storage cost at scale

10k × 100 = 1M chunks. We already pay this — `chunk_embedding_cache` deduplicates by `url_hash`, so the marginal cost of profile attribution is ~zero embedding spend (we attribute already-embedded chunks). The growth is in `contributor_attributions` rows: ~50 bytes × 1M = 50MB. Trivial.

Index strategy: **do not partition by profile_type or chain**. The retrieval pattern is "per query, route across profile types," which means every query crosses partitions. Instead: keep the existing ivfflat on `chunks.embedding` (20260425000100), add a btree on `contributor_attributions(contributor_id)`, and let the planner pre-filter via a CTE when the orchestrator asks for a profile-typed slice. RPC latency stays sub-100ms at 1M rows with `lists≈1000` on ivfflat (current is 100; bump in a follow-up migration when `n_chunks > 200k`).

## 5. Migration sequencing

1. `2026MMDD_contributors.sql` — table + RLS read-all, write service-role only (mirrors `gecko_precedent`).
2. `2026MMDD_contributor_attributions.sql` — join table, backfill empty (S12 SourceProvider chunks stay un-attributed; that's fine, they're still retrievable).
3. `2026MMDD_contributor_profile_tags.sql` — tag table.
4. `2026MMDD_mv_contributor_citation_counts.sql` — MV + refresh trigger on `gecko_precedent` insert.

Backward-compat is free: every new table is additive, every existing RPC keeps its signature.

## 6. Sprint 15+ ticket

**S15-DATA-01 — Contributor registry + attribution join (3-4 days).**

Acceptance:
- Migrations 1-3 above land; `supabase db reset` clean.
- New RPC `match_chunks_by_profile(query_embedding, profile_types text[], match_count)` pre-filters via attribution join, returns `{chunk, contributor_id, profile_types[], similarity}`.
- Backfill script populates `contributors` from existing publish.new wallets in `gecko_precedent.user_id` and Paragraph cited-URL hosts (best-effort, mark `profile_tags.source='derived'`).
- Unit test: profile-filtered retrieval excludes un-attributed S12 chunks.
- No paid runs; stub embeddings only.

MV + refresh trigger ships as **S15-DATA-02** once S15-DATA-01 is merged.
