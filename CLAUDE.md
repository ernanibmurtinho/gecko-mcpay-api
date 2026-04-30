# Gecko — Builder Bootstrap Platform (Python backend)

You are working on **gecko-mcpay-api**, the Python backend for the Builder Bootstrap Platform. Distribution is via Claude Code skills (frames.ag-style: `Read app.geckovision.tech/skill.md` → bootstrap → use).

## The three repos

| Repo | Stack | What lives there |
|---|---|---|
| `gecko-mcpay-api` (this) | Python uv workspace | SDK, MCP server, FastAPI, CLI |
| `gecko-mcpay-app` | Next.js | V2 frontend at `app.geckovision.tech` |
| `gecko-mcpay-skills` | Markdown | Public skills served at `app.geckovision.tech/skill.md` |

Cross-repo work goes through `staff-engineer`. Most changes are contained in one repo.

## Communication Style

- No filler ("Great question", "Awesome, here's what I'll do")
- Direct, code-first, brief
- Admit uncertainty rather than guess
- Recommendation first, justification second

## Repository shape (this repo)

uv workspace monorepo (single repo, multiple packages — that's normal Python project layout, not a polyrepo problem).

| Path | What lives there |
|---|---|
| `packages/gecko-core` | The SDK. Pure Python business logic. **All real work happens here.** |
| `packages/gecko-mcp` | MCP server wrapping `gecko-core`. The frames-style surface for Claude Code. |
| `packages/gecko-api` | FastAPI service. What `gecko-mcpay-app` calls. Deploys independently. |
| `apps/cli` | The `bb` / `gecko` CLI. Wraps `gecko-core`. |
| `skills/` | DOES NOT EXIST HERE — skills live in the `gecko-mcpay-skills` repo |
| `apps/web/` | DOES NOT EXIST HERE — web app lives in the `gecko-mcpay-app` repo |
| `infra/supabase/migrations` | DB schema, including pgvector setup |
| `docs/` | PRD.md, product-story.md, design specs |

**Rule:** business logic in `gecko-core`. CLI, MCP, API are thin transport layers — parse input, call core, format output. If you find logic in `gecko-mcp` or `apps/cli`, move it to `gecko-core`.

## Tech stack

- **Python 3.11+**, `uv` for env + workspace management
- **Supabase** (Postgres + pgvector) for sessions, sources, chunks, embeddings
- **OpenAI** `text-embedding-3-small` and `gpt-4o-mini`; Pro tier uses **AutoGen** GroupChat
- **Tavily** for source discovery; `httpx` + `youtube-transcript-api` for extraction
- **x402** on Solana for payments — modes: `stub`, `live`, `frames` (post-hackathon)
- **Click** for CLI, **Rich** for terminal output, **FastAPI** for the API
- **MCP** for the Claude Code surface

## Mandatory workflow

```bash
uv run ruff format
uv run ruff check --fix
uv run mypy packages/ apps/
uv run pytest
# if pipeline touched:
bb research --idea "smoke test"
# if env/config touched:
gecko-mcp doctor
```

## Security non-negotiables

**NEVER**:
- Commit secrets. `.env` files are gitignored; `.env.example` ships with empty values.
- Expose `SUPABASE_SERVICE_ROLE_KEY` to any client. CLI is OK; `gecko-mcpay-app` is NOT — it uses anon key + RLS via `gecko-api`.
- Log API keys, even in errors. Redact before raising.
- Show users model names, token counts, or per-operation costs. Session pricing is the unit.
- Call OpenAI without `response_format={"type": "json_object"}` when the caller expects JSON.

**ALWAYS**:
- Validate URLs before fetching (no SSRF — block private IP ranges, file://, etc.)
- Cap LLM input tokens per request; truncate context defensively.
- Persist sessions before any expensive work so a crash doesn't lose state.
- Pass `X402_MODE` through every payment-touching code path; default to `stub` if unset.

## Subagent team

Eight specialists. Seven own work in this repo; `frontend-engineer` is a cross-repo stub that surfaces coordination notes for their full counterpart in `gecko-mcpay-app`.

**Engineering (staff-engineer arbitrates across these lanes):**

| Agent | When |
|---|---|
| `staff-engineer` | Architectural decisions, cross-package or cross-repo refactors, "should we…" questions, arbitration when work crosses lanes |
| `ai-ml-engineer` | Prompt engineering, agent persona design, eval harness + thresholds, RAG quality, AG2 5-agent debate, advisor panel reliability, verdict synthesis, "why is the model doing X" |
| `software-engineer` | Feature implementation in `packages/` and `apps/` (Python) |
| `data-engineer` | Supabase schema, pgvector, ingestion pipeline, embeddings storage |
| `web3-engineer` | x402, Solana, Base/CDP, wallet flows, frames.ag integration, on-chain settlement |

**Product:**

| Agent | When |
|---|---|
| `business-manager` | PRD updates, pricing, GTM, success metrics |
| `product-designer` | Terminal output styling, UX flows, web app screen specs |
| `frontend-engineer` | Cross-repo stub. Invoke when API or model changes here affect `gecko-mcpay-app`. Real implementation agent lives in that repo. |

Default to `staff-engineer` for any change that touches more than one package, more than one repo, or crosses engineering lanes.

**Lane boundaries to keep clean:**

- `software-engineer` owns "the code runs correctly"
- `data-engineer` owns "the data is correctly stored"
- `ai-ml-engineer` owns "the model gives the right answer"
- `web3-engineer` owns "the payment settles correctly"

When a problem touches more than one of these, route to `staff-engineer` first.

## MCP servers (development)

Configured in `.mcp.json`. Keys go in `.env` (gitignored).

- **Supabase MCP** — schema introspection during dev
- **Context7** — up-to-date library docs lookup
- **Helius** (when `web3-engineer` is active) — Solana RPC + DAS

## Done checklist

Before merging:

- [ ] `uv sync` succeeds
- [ ] Lint + format clean: `uv run ruff check && uv run ruff format --check`
- [ ] Tests pass: `uv run pytest`
- [ ] If pipeline touched: `bb research --idea "smoke test"` runs in stub mode
- [ ] If env/schema touched: `gecko-mcp doctor` passes
- [ ] If API shape changed: notify `frontend-engineer` in `gecko-mcpay-app` (the OpenAPI spec at `/openapi.json` is the contract)
- [ ] Docs updated: README quickstart works; PRD reflects scope changes
- [ ] No secrets in diff

## Self-learning

**`CLAUDE.md`** (this file, tracked):
- Emphatic user preferences/corrections
- Patterns that repeat 2+ times
- Explicit "remember this"

**`CLAUDE.local.md`** (gitignored):
- Session notes, debugging context, scratch observations

### Project conventions

(empty)

### Recurring patterns

(empty)

---

**Sister repos**: [`gecko-mcpay-app`](https://github.com/<owner>/gecko-mcpay-app) | [`gecko-mcpay-skills`](https://github.com/<owner>/gecko-mcpay-skills) | **Domain**: `app.geckovision.tech`
