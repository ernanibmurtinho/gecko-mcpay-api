# Verdict URL — API Contract

**Sprint:** S20-VERDICT-URL-IMPL-01 (#10)
**Owner:** software-engineer (Python impl) + data-engineer (Mongo index) + staff-engineer (arbitration)
**Stub author:** frontend-engineer (cross-repo coordination)
**Date:** 2026-05-02
**Status:** spec — implementation in flight

---

## Endpoint

```
GET /v1/verdict/{hash}                 → 200 teaser
GET /v1/verdict/{hash}?detail=full     → 402 (placeholder for S20 #11)
```

`{hash}` accepts:
- **64-char full sha256** (canonical) → 200 with teaser body.
- **12-char short hash** → 302 redirect to canonical full-hash URL. 0 matches → 404. >1 match (collision space ~2^48, unlikely) → 409 `{"error": "verdict_hash_ambiguous", "candidates": [...]}` so the client can disambiguate.

Rationale: short hash is a UX affordance (mono token in CLI footer); canonical hash is what's tradeable.

## Teaser response (200)

```json
{
  "verdict_hash": "a1b2c3d4...64chars",
  "verdict_hash_short": "verdict@a1b2c3d4e5f6",
  "idea_text": "...",
  "verdict": "GO|REFINE|PIVOT|KILL",
  "judge_prose_excerpt": "first 1-2 sentences, max 280 chars, ellipsised",
  "gap_classification": "Partial:UX",
  "created_at": "2026-05-02T14:33:00Z",
  "tier": "basic|pro",
  "provider_mix_flag": "balanced|single_provider_dominates|thin_diversity|null",
  "is_paywalled": true,
  "preview_only": true
}
```

**Explicitly NOT exposed:** full citations, full PRD/business-plan, advisor voices, transcript, dissent quotes, source list. All gated behind `?detail=full` (#11).

## 402 stub for `?detail=full`

```json
HTTP 402
{
  "error": "payment_required",
  "message": "x402 settlement coming in S20 #11",
  "verdict_hash": "<echo>",
  "price_usdc": "2.50"
}
```

Include `price_usdc` in the body so frontend renders dynamic CTA copy from the response, not hardcode it. When #11 lands and pricing varies by tier, the frontend already reads from the response and won't need a second deploy.

## Lookup strategy

**Persist the hash at workflow finalisation (option a).**

- Add `verdict_hash: str | None = None` to `ResearchResult` (`packages/gecko-core/src/gecko_core/models.py`).
- Compute + stamp at finalisation, AFTER `provider_mix_flag` is stamped (since the flag is part of the hash input contract per S18-D4). Use `result.model_copy(update={"verdict_hash": verdict_hash(idea, result)})`.
- Persist into `judge_transcripts` at the existing `insert_one` site. New top-level field `verdict_hash`.
- Mongo index: `db.judge_transcripts.create_index("verdict_hash")`. **Not unique** — same idea+verdict could legitimately reproduce identical inputs across runs; API returns most-recent by `created_at desc`.

This is a schema **addition**, not a migration — Mongo is schemaless, no destructive change. New writes carry the field; old documents return 404 on lookup until they're either backfilled (out of scope for #10) or naturally age out.

## 404 semantics

```
HTTP 404
{ "error": "verdict_not_found", "hash": "<echo>" }
```

No timing-side-channel difference between "never existed" and "existed but expired" — single code path. Don't leak future-verdict existence.

## Rate limiting

- Public teaser: **60 req/min/IP**, sliding window.
- `?detail=full`: **10 req/min/IP** (each call eventually settles x402). For #10 it returns 402 immediately, but install the limiter now so #11 doesn't have to retrofit.

## CORS

- Teaser endpoint: `Access-Control-Allow-Origin: *` (public-by-design, embeddable, curl-able).
- `?detail=full`: open until #11; restrict to `https://app.geckovision.tech` once paywall lands.

## Implementation checklist (Python repo)

1. `packages/gecko-core/src/gecko_core/models.py` — add `verdict_hash: str | None = None` to `ResearchResult`.
2. `packages/gecko-core/src/gecko_core/workflows.py` — finalisation (after `provider_mix_flag` is stamped) computes via `verdict_hash(idea, result)` and `model_copy` the result.
3. `packages/gecko-core/src/gecko_core/orchestration/transcripts.py` — extend the `judge_transcripts.insert_one` document with `verdict_hash`; add `create_index("verdict_hash")` in the index-bootstrap helper.
4. `packages/gecko-api/src/gecko_api/routes/verdict.py` — new router per the contract above; register in the FastAPI app factory.
5. Tests:
   - `tests/api/test_verdict_route.py` — teaser happy path, 404, 402 on `?detail=full`, short-hash redirect, ambiguous-short-hash 409.
   - `tests/test_verdict_persistence.py` — finalisation writes `verdict_hash` into the persisted transcript document.

## One thing for #11 (x402 verdict settle) to watch

Per Pattern C in CLAUDE.md: ship the x402 verdict-settle path with a **recorded-fixture contract test** against the real facilitator BEFORE relying on stub-mode green tests. `/verify` passing won't tell you whether `/settle` dispatches correctly on the verdict-bound payload (this is the exact failure mode that bit Sprint 12 CDP).
