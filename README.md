# Gecko — Builder Bootstrap Platform (Python backend)

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-3776AB.svg)](https://www.python.org/)
[![uv](https://img.shields.io/badge/uv-workspace-DE5FE9.svg)](https://docs.astral.sh/uv/)
[![Claude Code](https://img.shields.io/badge/claude--code-MCP-D97757.svg)](https://docs.anthropic.com/claude/claude-code)
[![x402](https://img.shields.io/badge/x402-stub%20%7C%20live%20%7C%20cdp-9945FF.svg)](https://x402.org/)
[![Sprint 14](https://img.shields.io/badge/sprint-14%20in%20flight-success.svg)](docs/build-plan-sprint-14.md)

> **The budget approver above x402.** Adversarial validation as a pre-spend gate.
> Before an agent (or a founder) commits real money to building, Gecko returns a
> signed verdict — **KILL / REFINE / BUILD** — backed by a five-voice advisor
> debate, cited sources, and an on-chain receipt.

This repository is the **Python backend**: `uv` workspace housing the SDK, the
FastAPI service, the MCP server, and the `bb` / `gecko` CLI. The Next.js app
lives in [**gecko-mcpay-app**](https://github.com/<owner>/gecko-mcpay-app).
The public skill registry lives in
[**gecko-mcpay-skills**](https://github.com/<owner>/gecko-mcpay-skills) and is
served at [`app.geckovision.tech/skill.md`](https://app.geckovision.tech/skill.md).

> **Status (Sprint 14, 2026-05-01):** V1 shipped. Verdict unification (S11),
> CDP Bazaar listing (S12), DeFi vertical suite (S13), `gecko_pulse` v1 +
> Paragraph + publish.new (S14) in flight. Profile-typed contributor arc
> (S15-S17) seam-prep next sprint. See [`docs/`](docs/README.md) for the full map.

---

## Three install paths

Pick the one that matches how you'll use Gecko.

### A. Claude Code skill (founders, agent operators)

Bootstraps the MCP server pointed at `https://api.geckovision.tech` and ships
the `gecko_research` / `gecko_plan` / `gecko_pulse` tools straight into your
Claude Code session. No local env, no Supabase keys.

```bash
curl -fsSL https://app.geckovision.tech/install.sh | bash
```

Then in Claude Code:

```
Read https://app.geckovision.tech/skill.md and follow the instructions.
```

Skills, agents, and installer scripts ship from
[**gecko-mcpay-skills**](https://github.com/<owner>/gecko-mcpay-skills).

### B. `bb` CLI direct (developers, contributors, stub-mode demos)

```bash
git clone https://github.com/<owner>/gecko-mcpay-api.git
cd gecko-mcpay-api
uv sync
cp .env.example .env             # X402_MODE=stub by default — no money required
uv run bb research --idea "hotel guide for Brazil"
```

Stub mode produces a full verdict + business plan + PRD in <2 minutes with no
on-chain spend. Flip `X402_MODE=live` (Solana via frames.ag) or `X402_MODE=cdp`
(Base via Coinbase Developer Platform) to settle for real.

Walkthrough: [`docs/demo/quickstart.md`](docs/demo/quickstart.md).

### C. HTTP API (other agents, integrators)

```bash
uv run gecko-api                 # local
# OpenAPI: http://localhost:8000/openapi.json    Swagger: http://localhost:8000/docs
```

Production: `https://api.geckovision.tech/openapi.json` is the contract that
[**gecko-mcpay-app**](https://github.com/<owner>/gecko-mcpay-app), Bazaar
discovery, and third-party agents call. Routes are listed in CDP Bazaar at
`/.well-known/x402` (S12).

---

## What you get (surface area)

| Surface | MCP tool | CLI | Price (V1) |
|---|---|---|---|
| Free verdict review on existing session | `gecko_review` | `bb review <session_id>` | **Free** |
| Basic research | `gecko_research --tier basic` | `bb research --tier basic` | **$0.10** |
| Pro research (5-voice AG2 debate) | `gecko_research --tier pro` | `bb research --tier pro` | **$0.75** |
| Pulse re-validation (S14) | `gecko_pulse <session_id>` | `bb pulse <session_id>` | **$0.50** (12-pack: $5.40) |
| DeFi vertical suite (S13, gated) | `gecko_research --vertical defi` | `bb research --vertical defi` | **$9** |
| Other verticals (S14+) | `--vertical <name>` | same | **$12–$19** |
| Multi-rail orchestrator (V3+) | TBD | TBD | **$29** |
| Publish verdict to publish.new (S14) | `--publish` flag | `--publish` flag | **$0.50** (default) |
| Earnings ledger | `bb earnings` | `bb earnings` | view-only |
| Doctor / health | `gecko_doctor` | `bb doctor` | free |
| Memory + sessions | `gecko_memory_*`, `gecko_resume`, `gecko_review` | — | free |
| Economics | `gecko_project_economics` | `gecko-mcp economics <session>` | free |

**Authoritative tool list:** start `gecko-mcp serve` and inspect via your MCP
client, or hit `http://localhost:8000/openapi.json`.

---

## Wallet & facilitator neutrality

Gecko works above **any** x402-capable wallet. We never hard-code one.

| Rail | Wallet / facilitator | x402 mode |
|---|---|---|
| Solana | frames.ag | `X402_MODE=live` |
| Base / EVM | CDP (Coinbase Developer Platform) | `X402_MODE=cdp` |
| Local dev | none — synthetic receipts | `X402_MODE=stub` (default) |
| HTTP middleware (parallel) | Cloudflare, awal | resolved via factory |

The product, the verdict, and the renderer are wallet-agnostic. See
`packages/gecko-core/src/gecko_core/payments/` (X402Client Protocol, S13-PAY-01).

---

## What's in this repo

| Path | Purpose |
|---|---|
| `packages/gecko-core` | The SDK. Pure Python business logic. **All real work happens here.** |
| `packages/gecko-mcp` | MCP server (stdio); thin client to `gecko-core`. |
| `packages/gecko-api` | FastAPI service. What `gecko-mcpay-app` and Bazaar agents call. Deploys independently. |
| `apps/cli` | The `bb` / `gecko` CLI. Wraps `gecko-core`. |
| `apps/demo-agent` | Reference agent: Bazaar discovery → x402 settle → call gecko-api. |
| `infra/supabase/migrations` | DB schema, including pgvector setup. |
| `docs/` | Index at [`docs/README.md`](docs/README.md). |

**Rule:** business logic lives in `gecko-core`. CLI, MCP, API are thin
transport — parse input, call core, format output. If you find logic in
`gecko-mcp` or `apps/cli`, move it to `gecko-core`.

---

## Stack

| Layer | Tool |
|---|---|
| Language | Python 3.11+ |
| Workspace | `uv` workspaces |
| LLM routing | OpenAI / OpenRouter / ClawRouter via `LLM_ROUTER` (`.env.example`) |
| Embeddings | `text-embedding-3-small` |
| Pro orchestration | AutoGen / AG2 5-agent GroupChat (CEO, CTO, PM, Staff, Designer + judge) |
| Database | Supabase Postgres + pgvector |
| Source discovery | Tavily (Web), youtube-transcript-api, Paragraph MCP (S14), twit.sh (S14), Deepgram |
| Payments | x402; modes `stub`, `live` (Solana), `cdp` (Base); `frames` post-hackathon |
| Bazaar listing | CDP Bazaar via `/.well-known/x402` (S12) |
| CLI | Click + Rich |
| MCP | `mcp` package |
| API | FastAPI |

---

## Environment variables

**Source of truth:** [`.env.example`](.env.example).

| Variable | Required | Default | Notes |
|---|---|---|---|
| `OPENAI_API_KEY` | yes (Path A) | — | LLM + embeddings on direct OpenAI path |
| `OPENROUTER_API_KEY` | yes (when `LLM_ROUTER=openrouter`) | — | Multi-model routing |
| `LLM_ROUTER` | no | `openai` | `openai` / `openrouter` / `clawrouter` |
| `SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY` | yes | — | Server-only; never expose to web app |
| `TAVILY_API_KEY` | yes | — | Source discovery |
| `X402_MODE` | no | `stub` | `stub` / `live` (Solana) / `cdp` (Base) |
| `X402_FACILITATOR_URL` | optional | network default | Override facilitator |
| `CDP_API_KEY_ID` / `CDP_API_KEY_SECRET` | when `X402_MODE=cdp` | — | Mainnet CDP x402 |
| `GECKO_DEFAULT_TIER` | no | `basic` | `basic` or `pro` |

`SUPABASE_SERVICE_ROLE_KEY` is server-only. The Next.js app uses anon key + RLS
via `gecko-api`. Never bundle it into a client.

---

## Run services

```bash
uv run gecko-api                 # FastAPI: http://localhost:8000/docs
uv run gecko-mcp doctor          # health gate (use as pre-flight)
uv run gecko-mcp serve           # MCP over stdio (points at GECKO_API_URL)
```

---

## Pricing & economics (operators)

Retail is **per session**, not per token. We do not surface model names or
per-operation costs to end users. After a run, inspect unit economics:

```bash
uv run gecko-mcp economics <session_id>
uv run gecko-mcp project <project_id>
```

The full ladder lives in the surface table above; tunable via the price env
vars in `.env.example` (`RESEARCH_BASIC_PRICE`, `RESEARCH_PRO_PRICE`,
`PULSE_PRICE`, `PUBLISH_NEW_PRICE`, …).

---

## Development

```bash
uv run ruff format
uv run ruff check --fix
uv run mypy packages/ apps/
uv run pytest
# pipeline touched:
uv run bb research --idea "smoke test"
# env/config touched:
uv run gecko-mcp doctor
```

See [`CLAUDE.md`](CLAUDE.md) for the full agent workflow + project conventions
(canonical `Literal` types, bucketed reputation bands, wallet neutrality).

---

## Documentation

Full map at [`docs/README.md`](docs/README.md). Highlights:

| Doc | When to read it |
|---|---|
| [`docs/PRD.md`](docs/PRD.md) | Product requirements (V1 / V2 / V3) |
| [`docs/product-story.md`](docs/product-story.md) | How we got here; Gecko → Builder Bootstrap pivot |
| [`docs/build-plan-sprint-14.md`](docs/build-plan-sprint-14.md) | Current sprint |
| [`docs/build-plan-sprint-15.md`](docs/build-plan-sprint-15.md) | Next sprint (profile-thesis seam-prep) |
| [`docs/strategy/bazaar-deeper-thesis-2026-04-30.md`](docs/strategy/bazaar-deeper-thesis-2026-04-30.md) | Macro thesis: judgment > capability |
| [`docs/strategy/profile-thesis-synthesis-2026-05-01.md`](docs/strategy/profile-thesis-synthesis-2026-05-01.md) | S15-S17 arc rationale |
| [`docs/runbooks/`](docs/README.md#runbooks-operational) | Operational |
| [`docs/audits/`](docs/README.md#diagnostics--audits) | Audits, dogfood findings, deltas |

---

## Contributing

This repo is in active hackathon-mode build. Cross-repo work routes through
`staff-engineer` (see [`CLAUDE.md`](CLAUDE.md) → Subagent team). Most changes
are contained in one of the three repos.

When opening a PR:

- `uv sync` succeeds
- `uv run ruff check && uv run ruff format --check` clean
- `uv run pytest` passes
- If pipeline touched: `bb research --idea "smoke test"` runs in stub mode
- If API shape changed: notify `frontend-engineer` in `gecko-mcpay-app`
  (`/openapi.json` is the contract)
- No secrets in diff

---

## Sister repos

- [**gecko-mcpay-app**](https://github.com/<owner>/gecko-mcpay-app) — Next.js frontend (`app.geckovision.tech`)
- [**gecko-mcpay-skills**](https://github.com/<owner>/gecko-mcpay-skills) — Public skills; `app.geckovision.tech/skill.md`

---

## License

`LICENSE` not yet checked in (tracked as **S15-DOCS-03**). Treat licensing as
**repository-owner default** until the file lands.

---

*Builder Bootstrap Platform · [geckovision.tech](https://geckovision.tech)*
