# Gecko Test Plan — Sprint 2 → 6

Comprehensive test plan covering everything shipped Sprint 2 through Sprint 6. Use this before any release.

Pre-flight (always):

```bash
uv run ruff format
uv run ruff check --fix
uv run mypy packages/ apps/cli
uv run pytest
```

---

## 1. Automated coverage matrix

Grounded in tests that exist on `main`. Sprint 7 Track A/B items are marked **forthcoming**.

| Feature | Test file(s) | Asserts |
|---|---|---|
| **Pro debate (AutoGen GroupChat)** | `packages/gecko-core/tests/orchestration/test_workflows_pro.py`, `test_pro_skeleton.py`, `test_pro_transcript.py`, `test_pro_router.py`, `test_pro_agents_matrix.py`, `test_pro_precedents.py` | GroupChat assembly, role matrix wiring, transcript capture, precedent injection, router fallback |
| Pro budget guard | `packages/gecko-core/tests/orchestration/test_pro_budget.py`, `test_pro_tokens.py` | Token cap respected, request truncated defensively |
| Pro retry on transient | `tests/api/test_research_pro_retry.py`, `packages/gecko-mcp/tests/test_sse_client_retry.py` | 5xx retried with backoff; SSE reconnect on disconnect |
| Pro SSE stream | `tests/api/test_pro_sse.py`, `tests/mcp/test_sse_client.py` | Event ordering, completion frame, abort propagation |
| **Scaffold (S3)** | `packages/gecko-core/tests/orchestration/test_scaffold_synth.py`, `packages/gecko-mcp/tests/test_scaffold_tool.py`, `tests/cli/test_scaffold_command.py` | Synth output schema, MCP tool wiring, CLI Rich render |
| **Route — 3 tiers (S4)** | `packages/gecko-core/tests/routing/test_route.py`, `test_matrix.py`, `test_costs.py`, `packages/gecko-mcp/tests/test_route_tool.py`, `packages/gecko-api/tests/test_route_endpoint.py`, `tests/cli/test_route_command.py` | basic/quality/max selection, per-role model from matrix, cost ladder monotonic, endpoint contract |
| Catalog drift guard (S6) | `tests/routing/test_catalog_drift.py`, `packages/gecko-core/tests/routing/test_catalog.py` | Catalog matches matrix; missing model fails CI |
| Tier ladder fallback (S6) | `packages/gecko-core/tests/routing/test_route.py` (`test_tier_fallback_*`) | Auto-downgrade when upstream errors; respects `tier_preset` arg |
| **Advise — 5 voices (S4)** | `packages/gecko-core/tests/orchestration/test_advisor.py`, `packages/gecko-mcp/tests/test_advisor_tools.py`, `tests/cli/test_advise_command.py` | Per-voice persona prompt, JSON schema, single-voice vs panel |
| **Plan (S5)** | `packages/gecko-api/tests/test_plan_endpoint.py`, `tests/cli/test_plan_command.py` | Paid `/plan` charges once, idempotent on retry, returns plan + receipt |
| Pricing surface | `tests/api/test_pricing.py`, `packages/gecko-core/tests/orchestration/test_pricing.py` | Session-unit pricing only; no per-op leak |
| **Pulse (S4/S5)** | `tests/cli/test_pulse_command.py` | Delta vs prior pulse_run; renders Rich panel |
| Pulse runs persistence | (covered via `tests/sessions/test_store.py` + memory write hook) | `pulse_runs` row created with diff payload |
| **Resume (S5)** | `tests/cli/test_resume_command.py` | Loads session + replays last journaled state |
| **Memory: save/recall/search/query** | `packages/gecko-core/tests/memory/test_store.py`, `test_auto_journal.py`, `packages/gecko-mcp/tests/test_memory_tools.py`, `tests/cli/test_memory_command.py`, `tests/rag/test_query.py` | CRUD, scope filtering, vector search top-k, RAG `query` returns citations |
| Auto-journal write hook | `tests/flywheel/test_write_hook.py`, `tests/flywheel_privacy.py` | Every paid call journals; PII redacted |
| **Contradiction detection (S5)** | `packages/gecko-core/tests/memory/test_contradictions.py` | Conflicting verdicts surfaced with both entry IDs |
| Priors injection (S6) | `tests/memory/test_priors_injection.py`, `tests/eval/test_priors_inversions.py` | Prior verdicts injected into Pro prompt; inversions don't poison eval |
| **Eval gate — general** | `tests/eval/test_gate.py`, `tests/eval/test_runner.py`, `tests/eval/test_rubric.py`, `scripts/run_eval_gate.sh` | ≥0.95 gate; rubric scores deterministic on stub |
| Eval — holdout no-leakage | `tests/eval/test_suite_no_leakage.py` | Holdout prompts unseen by training set |
| Eval — crypto / saas | `scripts/run_eval_gate.sh` (sub-suites) | Per-domain ≥0.95 |
| Eval — holdout-live | `scripts/run_eval_gate_live.sh` | Live RAG ≥0.80 (V1 baseline) |
| **Per-tier model selection (S4)** | `packages/gecko-core/tests/routing/test_matrix.py` | `(role, tier) → model` deterministic |
| **V2 sources — Reddit** | `tests/sources/test_reddit_adapter.py`, `packages/gecko-core/tests/test_source_reddit.py` | Subreddit search, comment chunk, dedup |
| V2 sources — GitHub | `tests/sources/test_github_adapter.py` | README + issues fetch, license filter |
| V2 sources — PDF | `tests/sources/test_pdf_adapter.py` | Page-level chunking, OCR fallback skipped |
| V2 sources — HN / Twitsh | `packages/gecko-core/tests/test_source_hn.py`, `test_source_twitsh.py`, `test_twitsh_active.py`, `test_source_dispatcher.py`, `test_source_catalog.py` | Adapter dispatch, catalog membership |
| **Reconcile (S6)** | `tests/economics/test_verify.py`, `scripts/reconcile_economics.py` | `bb economics --verify` matches on-chain; status transitions stub→confirmed |
| Idempotency — payments | `tests/payments/test_idempotency.py`, `tests/payments/test_factory.py`, `tests/payments/test_gate.py`, `tests/payments/test_stub_client.py`, `tests/payments/test_x402_network.py` | Replay yields same receipt; gate enforces mode |
| Privy provisioning | `tests/api/test_privy_provisioning.py`, `tests/payments/test_privy_client.py`, `tests/mcp/test_wallet.py` | Wallet bound to user, OTP flow stub |
| **Ingestion pipeline** | `tests/ingestion/test_pipeline.py`, `test_chunker.py`, `test_idempotency.py`, `test_url_validation.py`, `test_deepgram_provider.py`, `packages/gecko-core/tests/ingestion/test_tavily_truncation.py`, `test_embed_retry.py`, `test_web_parser.py` | SSRF guard, chunk size, embed retry, dedup by URL hash |
| Classify | `packages/gecko-core/tests/test_classifier_accuracy.py`, `packages/gecko-mcp/tests/test_classify_tool.py`, `tests/cli/test_classify_command.py` | Idea → category mapping above accuracy floor |
| Precedents tool | `packages/gecko-mcp/tests/test_precedents_tool.py`, `tests/cli/test_precedents_command.py` | Returns top-k prior projects from memory |
| Projects API | `tests/api/test_projects.py`, `tests/cli/test_project.py` | CRUD + RLS scoping |
| Auth + middleware | `tests/api/test_auth.py`, `tests/api/test_middleware.py` | Bearer required, anon vs service-role split |
| Doctor | `tests/mcp/test_doctor.py` | Env + schema sanity |
| MCP tool surface | `tests/mcp/test_tools.py`, `tests/mcp/test_api_client.py` | Tool registry shape stable |
| End-to-end smoke | `tests/smoke/test_e2e_user_flow.py`, `tests/smoke/test_phases.py`, `tests/demo_agent/test_main.py` | Full loop in stub mode |
| **`gecko_review` core (S7-A)** | `packages/gecko-core/tests/review/test_review.py` — **forthcoming (Sprint 7)** | Reads git log + memory + sprint docs → `SprintReview` schema |
| **`bb sprint-review` CLI (S7-A)** | `tests/cli/test_sprint_review_command.py` — **forthcoming (Sprint 7)** | `--since`, `--write-doc`, Rich panel |
| **E2E CI workflow (S7-B)** | `.github/workflows/e2e-smoke.yml` — **forthcoming (Sprint 7)** | Postgres+pgvector container, full loop, receipt assertion |
| **Sources collision regression (S7-B)** | `tests/sources/test_dispatcher_import.py` — **forthcoming (Sprint 7)** | Subprocess import asserts no `gecko_core.sources` shadow |

---

## 2. Manual smoke checklist (10–15 min)

Run before any release. Stub mode only.

```bash
# 0. clean env
grep '^X402_MODE' .env   # must be 'stub'
uv sync
uv run gecko-mcp doctor  # env + schema green

# 1. boot api
uv run uvicorn gecko_api.main:app --port 8000 &
API_PID=$!

# 2. research
bb research --idea "Should Sprint 8 prioritize live cutover or V3 dashboard?"
# capture printed session_id → $SID

# 3. advise (single voice) + plan (full panel, paid in stub)
bb advise --session $SID --voice cto
bb plan   --session $SID

# 4. pulse + resume
bb pulse  --session $SID
bb resume --session $SID

# 5. pricing surface
bb pricing  # session-unit prices only; no per-token output

# 6. sprint review (forthcoming Sprint 7 Track A)
bb sprint-review --since 14d
# until merged, skip with note

# 7. economics
bb economics $SID            # all receipts stub_-prefixed in stub mode

# 8. teardown
kill $API_PID
```

Expect: every command exits 0, journal entries land in memory (`bb memory recall --session $SID`), receipts present for paid calls.

---

## 3. Dogfood flow — `gecko_review` against this repo

Sprint 7 Track A deliverable. Use to review Gecko's own delivery each sprint.

```bash
# stub mode — free
X402_MODE=stub bb sprint-review --since 14d --project-id <repo-project-id>

# archive the output
X402_MODE=stub bb sprint-review --since 14d --write-doc
# -> docs/sprint-reviews/YYYY-MM-DD.md
```

Good output looks like:

- **shipped**: 5–12 bullets. Each maps to a real commit in `git log --since=14d` AND a memory entry (`entry_type` in `{verdict_received, scaffold_generated, plan_advised, advisor_voiced, pulse_run, feature_shipped}`). If a bullet has no commit hash backing it, the synth is hallucinating — file a bug.
- **weakest_link**: one sentence naming the gap (e.g. "no live-mode call landed yet"; "eval-saas suite below 0.95"). Empty `weakest_link` on a sprint with ≥3 shipped bullets is suspicious.
- **proposed_next**: exactly 3 bullets, each phrased as a track (e.g. "S8-LIVE-01 — first paid live call").

What to do with `weakest_link`:

1. If it names a missing test → open Sprint N+1 ticket, link this test plan row.
2. If it names an unfunded path (e.g. live cutover) → escalate to user, don't auto-fund.
3. If it names a code smell → `staff-engineer` triages before next sprint plans.

---

## 4. Live-mode pre-flight checklist

Do not flip `X402_MODE=live` without all five boxes checked.

- [ ] **Fund client wallet** with ~$1 SOL (gas only — USDC roundtrips to your own treasury). Use the frames.ag deposit UI shown in the OTP flow.
- [ ] **Verify `GECKO_WALLET_ADDRESS`** in `.env` is YOUR treasury address. Paste it into a block explorer, confirm the owner is you. A wrong address ships USDC to a stranger; this is irreversible.
- [ ] **Flip mode per-call**, not in `.env`:
      `X402_MODE=live bb research --idea "smoke test live x402"`
      Permanent `.env` flips have caused accidental paid runs in CI — do not.
- [ ] **Verify on-chain** immediately after the call:
      `bb economics <session_id> --verify`
      Expect `tx_signature` without `stub_` prefix, status `confirmed`.
- [ ] **Watch reconcile** for at least one cycle:
      `uv run python scripts/reconcile_economics.py --once`
      Treasury balance should rise by USDC amount minus facilitator fee. Any `pending` row older than 5 min → investigate before further live calls.

---

## 5. Eval gates — how to run + thresholds

| Suite | Command | `expected_verdict` (v1) threshold | `expected_verdict_v2` grading (S12-EVAL-02) |
|---|---|---|---|
| general | `bash scripts/run_eval_gate.sh general` | mean score **≥ 0.95** | **≥ 0.80** (3-bucket KILL/REFINE/BUILD; structurally harder than 2-bucket; tighten after 3 baseline runs) |
| holdout | `bash scripts/run_eval_gate.sh holdout` | **= 1.0** (no leakage tolerated) | **≥ 0.80** (same threshold; v2 grading on relabeled fixtures) |
| crypto | `bash scripts/run_eval_gate.sh crypto` | **≥ 0.95** | **≥ 0.80** |
| saas | `bash scripts/run_eval_gate.sh saas` | **≥ 0.95** | **≥ 0.80** |
| holdout-live (live RAG) | `bash scripts/run_eval_gate_live.sh` | **≥ 0.80** (V1 baseline; tighten in Sprint 11) | **≥ 0.80** (initial; v2 baselines pending Sprint 12 acceptance live run) |

**Two grading modes**: every suite is graded twice — once against the legacy
`expected_verdict` (`ship | kill`, with `pivot ↔ kill` softening) and once
against `expected_verdict_v2` (`KILL | REFINE | BUILD`) when the fixture
has the new field populated. Per S12-EVAL-02 the rubric reads v2 when
present and falls back to v1 otherwise. The legacy column is unchanged so
ADRs/runbooks pinned to the v1 thresholds keep their meaning during the
migration.

**Why v2 thresholds start at 0.80 (not 0.95)**: 3-bucket scoring is
structurally harder than 2-bucket — REFINE is its own bucket, not pivot=kill
softening. Stub-mode v2 floors 0.85–0.87 across general/crypto/saas with
the current relabel (driven by the legacy SHIP→BUILD bridge in
`extract_verdict_v2`); 0.80 is the calibration floor, with tightening
gated on ≥3 live baseline runs per CLAUDE.md "Variance is real at small N".

Mechanics:

- All suites run against fixtures in `tests/eval/` and the rubric in `packages/gecko-core/src/gecko_core/eval/`.
- Gate is enforced by `tests/eval/test_gate.py` — pytest fails if any threshold is missed.
- `holdout-live` is the only suite that costs money (~$5–7). Run on release candidates only, after stub gates green.
- Ablation: `bash scripts/run_eval_ablation.sh` — diagnostic, not gated.
- Drift: `uv run python scripts/check_catalog_drift.py` — must exit 0 before any eval run; otherwise model swap silently changes scores.

If a gate fails:

1. Re-run with `-v`. Flake on stub mode = bug, not noise.
2. Check `tests/eval/test_priors_inversions.py` — a poisoned prior can drag general down without breaking holdout.
3. `staff-engineer` decides whether to revert vs. tighten rubric.
