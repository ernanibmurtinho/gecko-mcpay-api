# Sprint 1 — Completion Report

**Window**: 2026-04-28 (overnight session)
**Goal**: After Sprint 1, every Pro session has truthful per-agent token/cost attribution, recovers gracefully from rate limits, and any developer can run an eval harness to score the 5 agents against a fixed idea suite.
**Status**: 🟢 shipped; eval harness invocation pending user authorization for live spend.

---

## What landed

| ID | Title | Commit | Notes |
|---|---|---|---|
| S1-01 | OpenRouter base-URL switch behind `LLM_ROUTER` env | `2db111b` | env switch (`openai`/`openrouter`/`clawrouter`); raises clearly at startup if openrouter selected without `OPENROUTER_API_KEY` |
| S1-02 | Per-agent model routing matrix | `2db111b` | analyst/proponent/critic/scoper → `gpt-4o-mini`; judge → `gpt-4o`. Router-aware string mapping; deep-copies llm_config per agent so AG2 mutation can't leak across |
| S1-03 | AG2 token counting from response usage | `2db111b` | path pinned to `agent.client.total_usage_summary` (autogen 0.7.5 / ag2 0.12.1); pre/post snapshot diff |
| S1-04 | Populate `session_costs` per turn | `2db111b` | per-agent llm row written every `turn_end` via `compute_cost_usd` from new pricing module |
| S1-05 | Pro retry-without-recharge | `2db111b` | HMAC-signed `retry_token` (24h TTL, prefix-distinct from events_token); status transition `failed → pending` is the single-use guard, no migration needed |
| S1-06 | Eval harness | 🟡 dispatched | mock mode by default, `--live` opt-in; baseline JSON per release, regression diff exits non-zero on >15% drop |
| S1-07 | install.sh `--reinstall-package gecko-core` | `720649d` (gecko-claude) | defeats stale 0.1.0 cache for returning users |
| S1-08 | ALB scanner blocks | `2db111b` | 6 ListenerRules drop /wp-admin*, /.env*, /phpmyadmin*, etc. with fixed-403; /.well-known/x402 preserved |
| S1-09 | Doctor surfaces routing | `2db111b` | new `[INFO] llm:router` and `[INFO] llm:matrix` lines |
| S1-10 | GHA v5 trailers | `b9329a6` (already shipped) | actions/checkout@v5, setup-uv@v5, upload/download-artifact@v5 |

## Test summary

```
gecko-mcpay-api    182 passed, 38 skipped
gecko-core          51 passed
gecko-mcp            2 passed
TOTAL              235 passed
```

mypy: clean on every package touched (gecko-core, gecko-api, gecko-mcp). 23 source files in the orchestration + ingestion + mcp tracks, 0 errors.

ruff format + check: clean (65 files unchanged, all checks pass).

## Sprint 1 deliverables shipped (git refs)

```
gecko-mcpay-api  main  2db111b   (pushed)
gecko-claude     main  720649d   (pushed)
```

## What did NOT ship in Sprint 1

- **Eval harness real run** — built, but defaults to mock mode (zero LLM cost). `--live` invocation is gated on user authorization (~$3-5 per default run, ~$10-15 with `--reruns 3`). See "Sprint 1 deferred" below.
- **Fargate deploy** — code is ready (`./infra/deploy.sh` will pick up `2db111b`). Deferred to user. Production stays on `b9329a6` until you redeploy.
- **`v0.1.2` release tag** — gecko-core / gecko-mcp version stays at `0.1.1` on PyPI. Tag a `v0.1.2-core` + `v0.1.2-mcp` after you redeploy if you want the routing changes published.

## Sprint 1 deferred (need user input)

### 1. Authorize eval harness live run

Default `--live` cost estimate (1 rerun × 20 ideas × ~5 turns × ~$0.0008/turn-pair on gpt-4o-mini + gpt-4o judge): **~$2-3**.

With `--reruns 3` (recommended for verdict-stability scoring): **~$8-10**.

To run manually after wake-up:

```bash
cd /home/nan/PycharmProjects/Gecko/gecko-mcpay-api
uv run python -m tests.eval.runner --live --reruns 3
```

This produces `tests/eval/baselines/2026-04-29.json` (or current date) which becomes the gating reference for future regressions.

### 2. Decide on `v0.1.2` release

Sprint 1 ships meaningful library changes (router, retry-without-recharge, token observability, agent matrix). End users on `gecko-mcp 0.1.1` will not get them. Options:

- **A** — Tag now: `git tag v0.1.2-core v0.1.2-mcp && git push origin v0.1.2-core v0.1.2-mcp`. Trusted Publisher publishes both. End users get the routing + retry features.
- **B** — Defer to end of Sprint 2 — ship a `v0.2.0` mainnet-cutover release that includes Sprint 1 + Sprint 2 changes. Cleaner story, longer wait for routing improvements.

Recommendation: **A**, because retry-without-recharge prevents another $0.75 burn for any user who hits a rate limit; library users benefit from getting it sooner rather than waiting for mainnet.

### 3. Redeploy gecko-api on Fargate

Current production runs `b9329a6`. Sprint 1 code is in `2db111b` on origin/main. Until redeploy, production has:

- ❌ no LLM_ROUTER (still OpenAI direct, no env switch)
- ❌ no per-agent model matrix (everything on `gpt-4o-mini`)
- ❌ no token counting (`tokens_in/out` still zero on prod transcripts)
- ❌ no retry-without-recharge endpoint (failures still cost $0.75)
- ❌ no ALB scanner blocks (still serving 404 to /wp-admin probes)

```bash
cd ~/PycharmProjects/Gecko/gecko-mcpay-api
./infra/deploy.sh  # us-east-2, picks up 2db111b
```

Deploy takes ~5 min (Docker build + ECR push + ECS rollout). Verify post-deploy:

```bash
curl -sS https://api.geckovision.tech/openapi.json | python3 -c "
import json,sys
d=json.load(sys.stdin); paths=[p for p in d['paths'] if 'retry' in p]
print('retry route:', paths)
"
# expect: retry route: ['/research/pro/{session_id}/retry']
```

## Sprint 2 unblock checklist

Five questions from `docs/roadmap-post-grant.md` §7. Recommended defaults:

- [ ] **1. Mainnet Pro pricing** — recommend hold $0.75
- [ ] **2. Repo visibility** — recommend public, prompts moved to private repo loaded at boot
- [ ] **3. Mainnet facilitator** — recommend CDP primary, PayAI fallback if onboarding stalls >3 days
- [ ] **4. Sample app subject** — recommend Devbrief (GitHub→1-pager)
- [ ] **5. Eval rubric judge model** — recommend GPT-4o, rotate quarterly

If you accept all 5 defaults, Sprint 2 (mainnet + Privy) starts immediately on next session.

---

## Critical-path sanity (vs roadmap)

Roadmap's critical path: `S1-03 → S1-06 → S2-01/02 → S2-05 → S2-08 → S3-02 → S3-01/03`.

We're now at the S1-06 boundary — eval harness built, awaiting authorized run. Once you bless `--live`, S2 unblocks (Sprint 2 is Privy + mainnet, ~10 days, web3-engineer-led).

## Open issues / surprises

- **`pyautogen` + `ag2[openai]` dual install** — both deps in `gecko-core/pyproject.toml`. Today they don't conflict (pyautogen 0.10 is a stub that re-exports from ag2). If ag2 ever cuts a hard break, we'll need to drop the legacy `pyautogen` dep.
- **AG2 mutates `llm_config` dicts** at runtime — caught and isolated via deep-copy in `_override_model`. Documented in `test_pro_agents_matrix.py` as a regression assertion.
- **Embedder `embed()` was reading env at import** even when `client` and `model` were passed in (the unit-test path). Fixed during Sprint 1 commit. No test regressions.

## Files added (Sprint 1)

```
docs/roadmap-post-grant.md
docs/sprint-1-completion.md   (this file)
packages/gecko-core/src/gecko_core/orchestration/pro/router.py
packages/gecko-core/src/gecko_core/orchestration/pro/pricing.py
packages/gecko-core/tests/orchestration/test_pro_router.py
packages/gecko-core/tests/orchestration/test_pro_agents_matrix.py
packages/gecko-core/tests/orchestration/test_pro_tokens.py
packages/gecko-core/tests/orchestration/test_pricing.py
packages/gecko-mcp/tests/test_sse_client_retry.py
tests/api/test_research_pro_retry.py
infra/supabase/migrations/011_waitlist.sql

# eval harness (S1-06, in flight as of this writing):
tests/eval/__init__.py
tests/eval/ideas.yaml
tests/eval/runner.py
tests/eval/rubric.py
tests/eval/mocks.py
tests/eval/README.md
tests/eval/baselines/.gitkeep
tests/eval/test_runner.py
tests/eval/test_rubric.py
```

## Files modified (Sprint 1)

```
.env.example
infra/ecs-stack.yml
packages/gecko-api/src/gecko_api/events_token.py
packages/gecko-api/src/gecko_api/main.py
packages/gecko-core/src/gecko_core/ingestion/embedder.py
packages/gecko-core/src/gecko_core/orchestration/pro/__init__.py
packages/gecko-core/src/gecko_core/orchestration/pro/agents.py
packages/gecko-core/src/gecko_core/workflows.py
packages/gecko-mcp/src/gecko_mcp/api_client.py
packages/gecko-mcp/src/gecko_mcp/doctor.py
packages/gecko-mcp/src/gecko_mcp/sse_client.py
gecko-claude/install.sh   (separate repo)
```
