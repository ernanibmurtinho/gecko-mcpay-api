# Gecko — Post-Grant Roadmap (4–6 Weeks)

## 1. Executive summary

- **LLM routing call: adopt OpenRouter as the production OpenAI-compatible base URL for Pro tier; keep ClawRouter for local dev only.** Passthrough pricing (provider rates + small markup), provider-side failover across 300+ models, zero infra to operate on Fargate. Ship in Sprint 1 behind `LLM_ROUTER` env (`openai` | `openrouter` | `clawrouter`). Status quo (OpenAI direct) is the rate-limit blast-radius problem we hit yesterday; running ClawRouter on Fargate is unjustified ops weight for a 2-person team.
- **Per-agent quality is currently unverifiable** — `tokens_in/out=0` across all 5 agents means COGS attribution is fictional and we can't tell if the Critic is actually adversarial. Sprint 1 lands V11-04/05 plus a 20-idea eval harness with rubric scoring. No mainnet cutover until this is green.
- **Mainnet cutover is Sprint 2, gated on observability.** CDP facilitator (`https://api.cdp.coinbase.com/platform/v2/x402`) for Solana mainnet, new mainnet keypair (do NOT reuse devnet `jhR4np1...Xkux`), price unchanged at $0.10 basic / $0.75 pro pending business-manager review.
- **Privy v2 per-project wallet isolation rides with mainnet** — same sprint, same migration window. Cryptographic budget enforcement is the V2 differentiator over "trust me" SaaS.
- **V3 dashboard at `app.geckovision.tech` is Sprint 3** — read-only project list, economics, transcript viewer. Marketing stays on apex `geckovision.tech`. Creator attribution ships as read-only graph (no settlement) — settlement is V4.
- **Sample app `gecko-sample-devbrief` ships in Sprint 3** as the public proof-of-output, deployed to `devbrief.geckovision.tech`, generated end-to-end via a recorded Pro session.
- **Explicit cuts**: no AG2 dynamic speaker selection (cost ceiling), no mobile, no governance/token, no multi-tenant orgs, no Loops/Resend (Supabase trigger + single transactional email is enough for V2).

---

## 2. Sprint structure

### Sprint 1 — Reliability & Observability (Week 1, ~7 days)

**Goal**: After Sprint 1, every Pro session has truthful per-agent token/cost attribution, recovers gracefully from rate limits, and any developer can run `uv run pytest tests/eval/` to score the debate quality of the 5 agents against a fixed 20-idea suite.

| ID | Title | Owner | Hours | Blocked-by | Files |
|---|---|---|---|---|---|
| S1-01 | OpenRouter base-URL switch behind `LLM_ROUTER` env | software-engineer | 4 | — | `packages/gecko-core/src/gecko_core/llm/client.py`, `packages/gecko-api/.env.example`, ECS task def |
| S1-02 | Per-agent model routing matrix (cheap for scoper/analyst, larger for critic/judge) | software-engineer | 6 | S1-01 | `packages/gecko-core/src/gecko_core/orchestration/pro/agents.py` |
| S1-03 | V11-04 AG2 token counting from response usage | software-engineer | 6 | S1-01 | `packages/gecko-core/src/gecko_core/orchestration/pro/__init__.py` |
| S1-04 | V11-05 populate `session_costs` per turn | data-engineer | 3 | S1-03 | `pro/__init__.py`, `core/sessions/store.py` |
| S1-05 | V11-02 Pro retry-without-recharge (signed retry_token, 24h TTL) | software-engineer | 10 | — | `packages/gecko-api/src/.../research_pro.py`, `packages/gecko-mcp/.../client.py` |
| S1-06 | Eval harness: 20 ideas × rubric (kill-rate, novelty, source-grounding, cost variance) | software-engineer | 12 | S1-03 | `tests/eval/ideas.yaml`, `tests/eval/runner.py`, `tests/eval/rubric.py` |
| S1-07 | V11-07 `install.sh` dep-pin reinstall fix | software-engineer | 2 | — | `gecko-claude/install.sh` |
| S1-08 | V11-09 bot-scanner: ALB listener rule blocking `/wp-admin`, `/.env`, `/.git` | staff-engineer | 1 | — | `infra/ecs-stack.yml` |
| S1-09 | Doctor command surfaces `LLM_ROUTER` + active model map | software-engineer | 2 | S1-02 | `packages/gecko-mcp/.../doctor.py` |
| S1-10 | Commit lingering GHA v5 bumps (V11-08 trailers) | software-engineer | 0.5 | — | `.github/workflows/*.yml` |

**Acceptance demo**:
```bash
LLM_ROUTER=openrouter bb research --pro --idea "habit tracker that pays in stablecoins"
gecko-mcp economics <session_id>   # shows non-zero per-agent tokens + costs
uv run pytest tests/eval/ -v       # 20 ideas, rubric scores logged, baseline saved
```

**Risks**:
- OpenRouter Solana-region latency adds 100–200ms per turn → measurable in eval harness; back out by flipping `LLM_ROUTER=openai`.
- Token-count attribute path differs between AG2 versions → S1-03 captures one canonical response and pins.
- Eval harness drift (rubric judge is itself an LLM) → use deterministic temperature=0 + version-pinned judge model in `rubric.py`.

---

### Sprint 2 — Mainnet & Wallet Isolation (Weeks 2–3, ~10 days)

**Goal**: After Sprint 2, anyone can run a Pro session paid in real USDC on Solana mainnet, against a per-project Privy wallet that cryptographically caps spend.

| ID | Title | Owner | Hours | Blocked-by | Files |
|---|---|---|---|---|---|
| S2-01 | Generate mainnet keypair, fund with $20 USDC float, store in SSM | web3-engineer | 2 | — | SSM `/gecko/prod/x402/mainnet_recipient` |
| S2-02 | CDP facilitator integration (`api.cdp.coinbase.com/platform/v2/x402`) + CDP API keys in SSM | web3-engineer | 6 | S2-01 | `packages/gecko-core/src/gecko_core/payments/x402.py` |
| S2-03 | `X402_NETWORK` env (`solana-devnet` \| `solana-mainnet`), CAIP-2 fix-up | web3-engineer | 4 | S2-02 | `payments/x402.py`, `gecko-api` config |
| S2-04 | Migration 012: add `network` column to `x402_settlements` | data-engineer | 1 | — | `infra/supabase/migrations/012_x402_network.sql` |
| S2-05 | Privy v2 per-project wallet provisioning on `POST /projects` | web3-engineer | 14 | S2-03 | `gecko-api/.../projects.py`, new `gecko-core/wallets/privy.py` |
| S2-06 | Per-project budget cap enforced server-side (signed quote → wallet allowance) | web3-engineer | 8 | S2-05 | `wallets/privy.py`, `payments/x402.py` |
| S2-07 | Migration 013: `projects.privy_wallet_id`, `projects.budget_cap_usd`, `projects.spent_usd` | data-engineer | 2 | S2-05 | `infra/supabase/migrations/013_project_wallets.sql` |
| S2-08 | Mainnet cutover runbook + canary (route 10% of `/research` to mainnet for 24h) | staff-engineer | 3 | S2-04 | `docs/runbooks/mainnet-cutover.md` |
| S2-09 | MCP client: surface project wallet balance + remaining budget in `gecko-mcp economics` | software-engineer | 4 | S2-07 | `gecko-mcp/.../economics.py` |
| S2-10 | Pricing review on mainnet (basic $0.10, pro $0.75 — confirm vs. mainnet COGS) | business-manager | 2 | S1-04 | `docs/pricing.md` |

**Acceptance demo**:
```bash
gecko project create --name "real-money-test" --budget 5
# Returns Privy wallet address, requires user fund with 5 USDC mainnet
bb research --pro --project real-money-test --idea "..."
# 200 OK, settlement on https://solscan.io (mainnet), session_costs.network='solana-mainnet'
gecko project show real-money-test  # spent_usd=0.75, budget_remaining=4.25
```

**Risks**:
- CDP API approval delay → start S2-02 with API key request on day 1 of Sprint 2; if blocked, fall back to PayAI facilitator.
- Privy v2 per-project wallet rate limits during burst project creation → cache provisioning, lazy-create on first paid request.
- Mainnet float burn during canary → cap canary at $5 total exposure, alarm on `spent_usd > 5` via CloudWatch.
- Back-out: `X402_NETWORK=solana-devnet` env flip restores devnet within 5 minutes.

---

### Sprint 3 — Surface Expansion (Weeks 4–5, ~10 days)

**Goal**: After Sprint 3, a non-CLI user can visit `app.geckovision.tech`, see their projects + economics + transcripts, and the world has one public, deployed sample app proving Gecko's output.

| ID | Title | Owner | Hours | Blocked-by | Files |
|---|---|---|---|---|---|
| S3-01 | V3 dashboard scaffold (Next.js App Router on `app.geckovision.tech`) | frontend-engineer-stub → cross-repo `gecko-mcpay-app` | — | S2-07 | `gecko-mcpay-app` repo |
| S3-02 | API: `GET /projects/:id/sessions`, `GET /sessions/:id/transcript` | software-engineer | 6 | — | `gecko-api/.../projects.py` |
| S3-03 | Dashboard screens: project list, project detail (economics), transcript viewer | frontend-engineer-stub | — | S3-02 | cross-repo |
| S3-04 | Dashboard auth: frames.ag bearer token round-trip | software-engineer | 4 | S3-02 | `gecko-api/.../auth.py` |
| S3-05 | UX spec for transcript viewer (5-agent debate as chat thread, kill/pivot verdict banner) | product-designer | 4 | — | `docs/design/transcript-viewer.md` |
| S3-06 | Creator attribution v0: `sources` table → `attribution_view` aggregating cited URL → session count + nominal earned | data-engineer | 6 | — | `infra/supabase/migrations/014_attribution_view.sql` |
| S3-07 | API: `GET /attribution?url=...` (read-only, no settlement) | software-engineer | 3 | S3-06 | `gecko-api/.../attribution.py` |
| S3-08 | `gecko-sample-devbrief` repo: generated via recorded Pro session, deployed to `devbrief.geckovision.tech` | software-engineer + product-designer | 12 | S2-03 | new public repo |
| S3-09 | Marketing apex update: link to `app.` dashboard + sample app | product-designer | 2 | S3-08 | `gecko-mcpay-landing` repo |
| S3-10 | Supabase trigger → single Resend transactional welcome email on waitlist insert | data-engineer | 4 | — | `infra/supabase/migrations/015_waitlist_email_trigger.sql` |
| S3-11 | Pro tier prompt review pass based on Sprint 1 eval results (only if rubric flagged issues) | product-designer + software-engineer | 6 | S1-06 | `pro/agents.py` |

**Acceptance demo**:
- Visit `https://app.geckovision.tech` → log in with frames.ag → see project, click into transcript, watch 5-agent debate render.
- Visit `https://devbrief.geckovision.tech` → live sample app with footer "generated by Gecko Pro session `<id>`".
- `curl https://api.geckovision.tech/attribution?url=https://newsletter.pragmaticengineer.com/...` returns `{"sessions": 12, "nominal_earned_usd": 1.80}`.

---

## 3. Agent assignments

### staff-engineer
- Sprint 1: S1-08 (WAF rules)
- Sprint 2: S2-08 (cutover runbook)
- Sprint 3: cross-repo coordination for S3-01/S3-03

### software-engineer
- Sprint 1: S1-01, S1-02, S1-03, S1-05, S1-06, S1-07, S1-09, S1-10
- Sprint 2: S2-09
- Sprint 3: S3-02, S3-04, S3-07, S3-08, S3-11

### data-engineer
- Sprint 1: S1-04
- Sprint 2: S2-04, S2-07
- Sprint 3: S3-06, S3-10

### web3-engineer
- Sprint 2: S2-01, S2-02, S2-03, S2-05, S2-06

### frontend-engineer (cross-repo stub)
- Sprint 2: OpenAPI contract review notice for S2-09 changes
- Sprint 3: S3-01, S3-03 (handed to `gecko-mcpay-app` repo's frontend-engineer)

### business-manager
- Sprint 2: S2-10 (mainnet pricing review)
- Sprint 3: grant follow-up reporting, sample-app announcement copy

### product-designer
- Sprint 3: S3-05, S3-08 (collab), S3-09, S3-11 (collab)

---

## 4. Cross-cutting concerns

### 4.1 LLM routing decision

**Decision: OpenRouter, behind `LLM_ROUTER` env, ship Sprint 1.**

Per-agent routing matrix (S1-02 starting point — calibrate against eval harness):
- `scoper`, `analyst` → `openai/gpt-4o-mini`
- `proponent`, `critic` → `openai/gpt-4o-mini` initially, promote to `gpt-4o` if eval shows insufficient adversarial bite
- `judge` → `openai/gpt-4o`

Embedding rate limits stay on OpenAI direct (already mitigated by V11-01 semaphore + retry); OpenRouter doesn't add value for embeddings.

### 4.2 Are the agents working as expected?

**Honest answer: we don't know.** Eval harness is the answer — 20 ideas × 3 reruns × rubric judge.

- `tests/eval/ideas.yaml` — 20 ideas, half "should kill" (saturated markets), half "should pivot/build" (real wedges)
- `tests/eval/runner.py` — runs each idea 3×, captures full transcript
- `tests/eval/rubric.py` — LLM-judge (GPT-4o, temp=0, pinned model version), 4 axes: agent-voice, source-grounding, verdict-justification, cost-variance
- Baseline saved to `tests/eval/baselines/<date>.json`; CI fails on >15% regression
- Run nightly via GitHub Action against staging

**Fixed-order driver question**: keep it. AG2 `auto` speaker selection costs +0.7% COGS and gives unpredictable transcripts that hurt eval determinism.

### 4.3 Mainnet payment recipient

Generate fresh keypair in S2-01. Don't reuse devnet keypair. Cutover sequence: deploy with `X402_NETWORK=solana-devnet`, set up CDP keys, deploy with mainnet for canary 10% via ALB weighted target groups for 24h, full cutover.

### 4.4 Frontend split: `app.` vs apex

- **`geckovision.tech`** (apex) — marketing, hero, waitlist, install
- **`app.geckovision.tech`** — authenticated dashboard (project list, economics, transcripts)
- **`app.geckovision.tech/skill.md`** — public skill registry, served from `app.`
- **`devbrief.geckovision.tech`** — sample app showcase

---

## 5. Out of scope (V4+)

- On-chain creator attribution settlement (graph only in Sprint 3)
- Privy DAO governance / multisig admin
- Multi-tenant org accounts
- Mobile, iOS, Android, native apps
- Token launch, governance token, points program
- Loops / Mailchimp / multi-stage email sequences
- ECS cluster autoscaling
- Custom WAF beyond simple ALB listener rules
- AG2 `auto` dynamic speaker selection
- New language SDKs (TypeScript, Go) — Python only
- Self-hosted facilitator
- Voice / audio interface to agents

---

## 6. Critical path

`S1-03 (token counting) → S1-06 (eval harness) → S2-01/02 (CDP keys) → S2-05 (Privy wallets) → S2-08 (cutover) → S3-02 (dashboard API) → S3-01/03 (dashboard UI)`. ~21 working days end-to-end. Single biggest risk: S1-06 eval harness producing a regression that forces Sprint 3's S3-11 prompt rework.

---

## 7. Open questions for the user

1. **Mainnet Pro tier pricing**: hold at $0.75 USDC, or raise to $0.99 to cover OpenRouter markup + mainnet gas + CDP per-tx fee?
2. **Repo visibility**: now that the grant is submitted, do we make `gecko-mcpay-api` public? Recommend: yes, with `pro/agents.py` prompts moved to a separate private repo loaded at boot.
3. **CDP vs PayAI facilitator on mainnet**: CDP is the official path. PayAI is free, no keys. Recommend CDP, fall back to PayAI if onboarding takes >3 days.
4. **Sample app subject**: `gecko-sample-devbrief` — is "developer brief generator" the right showcase, or something more visually striking?
5. **Eval rubric judge model**: comfortable using GPT-4o as the rubric judge? Alternatives: Claude Opus or 2-of-3 panel.
