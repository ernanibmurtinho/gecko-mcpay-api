# Builder Bootstrap Platform — PRD

**Version:** 1.1
**Date:** April 30, 2026
**Author:** Ernani Britto
**Status:** Active — V1 shipped

---

## Vision

> Turn a plain-language startup idea into a searchable knowledge base, a business plan, a validation report, and a PRD — in under 30 minutes — billed as a single session via x402 on Solana.

---

## Problem

Every builder hits the same wall before writing a single line of code: hours of manual research, unstructured notes, and no clear signal on whether the market is real. The current workflow — find videos, manually transcribe, drown in notes, attempt a business plan from intuition — takes 20+ hours and still produces uncertain output. No tool solves the whole workflow. Tools exist for pieces of it; none deliver structured, cited, actionable output from a plain-language idea.

---

## User Personas

### Persona 1 — Claude Code / Cursor power user with founder ambition (V1 Primary)

> **V1 ICP:** Claude Code / Cursor power users with founder ambition — technical or technical-adjacent.

| Attribute | Detail |
|-----------|--------|
| **Who** | Claude Code / Cursor power users with founder ambition — technical or technical-adjacent. Senior engineers, technical founders, AI-native builders, and indie hackers who already live in the terminal and treat Claude Code skills as a native install surface. |
| **Has** | Fluency with CLI tooling, AI-pair-programming workflow, idea backlog, willingness to pay per session |
| **Lacks** | Market validation, structured research, fundable PRD before writing code |
| **Goal** | Know if the idea is worth building — and walk away with the three docs that prove it — before opening the editor |
| **Pain** | Research takes longer than building; output is unstructured and uncited; "vibe-coded" specs collapse on contact with the market |
| **Interface** | CLI-first via `bb` / `gecko` skill; comfortable with terminal output, session IDs, and on-chain receipts |
| **Distribution fit** | Lands inside Claude Code via `Read app.geckovision.tech/skill.md` — by definition, this audience is CC-literate |

### Future expansion — Non-Technical Founder (Sprint 12+ / V2 audience)

The thesis-research "Non-developer founders" dimension surfaces a real, underserved audience (~18% startup success rate without a technical co-founder). They are **not** a V1 target because the CLI is the wrong surface for them. They become reachable when the `app.geckovision.tech` web app ships in V2.

| Attribute | Detail |
|-----------|--------|
| **Who** | Domain expert, operator, or solo founder with a business idea but no technical co-founder |
| **Has** | Market instinct, lived pain, domain knowledge |
| **Lacks** | Technical ability to build, structured data, validated market signal |
| **Goal** | Get a business plan, validation report, and PRD without hiring a consultant or finding a CTO |
| **Pain** | Tools require technical assembly; output is generic without domain context; CLI is a hard wall |
| **Interface** | GUI-first via `app.geckovision.tech`; expects progress feedback and a polished document reveal moment |
| **Unlock condition** | V2 web app ships with Privy embedded wallet + session sharing (Sprint 12+) |

---

## V1 — Hackathon Scope

> **Status: Shipped.** All V1 requirements are implemented.

### Core Requirements

| Feature | Description | Acceptance Criteria |
|---------|-------------|---------------------|
| **CLI entry point** | `bb` command with three subcommands: `research`, `ask`, `sources` | `bb --help` lists all commands; each has `--help` with argument docs |
| **`bb research`** | Main workflow: discover → index → pay → generate → output | Given a valid `--idea` and env vars, produces all three documents in terminal |
| **`--tier basic\|pro`** | Selects orchestration and pricing tier | `basic` runs single-pass LLM; `pro` runs AutoGen GroupChat; default is `basic` |
| **`--urls`** | User-provided seed URLs for indexing | Provided URLs are indexed alongside or instead of auto-discovered sources |
| **Tavily source discovery** | Auto-discovers top sources when no URLs provided | Given an idea string, returns 5–10 relevant source URLs ranked by relevance |
| **YouTube adapter** | Extracts transcript from YouTube URLs | Returns cleaned plain text from `youtube-transcript-api`; handles missing captions gracefully |
| **Web adapter** | Extracts text from article/blog URLs | Returns main body text via `httpx`; strips nav, footer, boilerplate |
| **Chunker** | Splits raw text into 512-token chunks with 50-token overlap | All chunks are ≤ 512 tokens; no chunk drops content between boundaries |
| **Embedder** | Generates vector embeddings per chunk | Uses `text-embedding-3-small`; stores in Supabase pgvector |
| **Ingestion pipeline** | Orchestrates extract → chunk → embed → store per source | Session, sources, and chunks are persisted; duplicate URLs are skipped |
| **x402 payment gate** | Charges session fee before indexing starts | No indexing runs without a completed payment; `stub` mode passes gate for dev/testing |
| **Basic orchestration** | Single GPT-4o-mini pass → three JSON documents | Returns valid JSON with `business_plan`, `validation_report`, `prd` keys |
| **Pro orchestration** | AutoGen GroupChat with 5 specialist agents | Orchestrator, Research, Market Analyst, Technical Architect, Validator agents produce structured output; agents stay alive 72h post-session |
| **RAG tool for Pro agents** | `rag_query()` available to Pro agents during GroupChat | Agents can pull context from the session knowledge base via pgvector similarity search |
| **Document renderer** | Formats and prints all three documents to terminal | Output uses `rich` formatting; sections are clearly separated; sources are cited |
| **`bb ask`** | Follow-up question against an existing session's knowledge base | Returns a grounded answer with citations from the session corpus |
| **`bb sources`** | Lists all indexed sources for a session | Returns source URL, type (youtube/web), chunk count, and indexed timestamp |
| **Session persistence** | Every workflow is stored as a session in Supabase | Session ID, tier, status, timestamps, and linked sources/chunks persist across restarts |

### Pricing (V1)

| Tier | Price | Mode |
|------|-------|------|
| Basic | $10–20 / session | x402, before indexing starts |
| Pro | $50–100 / session | x402, before indexing starts |
| `X402_MODE=stub` | $0 | Dev/demo — gate passes without charge |

---

## V2 — Post-Hackathon

> **Status: Planned.** Target: Months 1–3 post-hackathon.

- **Next.js web app** — GUI for non-technical founders; same workflow as CLI with progress indicators and a polished document reveal moment
- **Creator attribution graph** — Every indexed source stores creator handle + platform from day one; graph accumulates which creators' content produces the most valuable research across domains
- **Creator OAuth claim flow** — Creator logs in, sees "Your content was cited 47 times — $32 pending"; claims earnings without a cold pitch
- **Creator earnings settlement** — 70% of Pro query fees flow to cited creators; batch-settled when ≥ $15 accumulated on-chain
- **Web app auth** — Privy embedded wallet for non-technical founders; no seed phrase required
- **Session sharing** — Session link shareable with co-founders; read-only knowledge base access

---

## V3 — Marketplace

> **Status: Roadmap.** Target: Months 3–6 post-hackathon.

- **Creator marketplace** — Curated directory of high-signal creators by domain; builders can pay to access creator-specific knowledge bases
- **Subscription Pro tier** — Monthly subscription unlocks unlimited sessions + persistent agent team
- **Public Knowledge API** — Programmatic access to session knowledge bases; enables other tools to query indexed domain research
- **Oracle social integrations** — Automated quality scoring for indexed sources (engagement, authority, recency)
- **Multi-language support** — Spanish and Portuguese for SuperteamBrasil and LATAM communities

---

## Non-Functional Requirements

| Requirement | Target |
|-------------|--------|
| **Indexing latency** | Full ingestion pipeline (5 sources) completes in < 3 minutes |
| **Document generation latency (Basic)** | Single LLM pass returns documents in < 60 seconds |
| **Document generation latency (Pro)** | AutoGen GroupChat completes in < 5 minutes |
| **Per-session cost ceiling** | Total infra cost (embeddings + LLM + Tavily + storage) ≤ $5 at Pro tier |
| **Data retention** | Session knowledge bases retained for 90 days minimum; Pro agent context lives 72h post-session |
| **Security** | No user private keys stored; `SUPABASE_SERVICE_ROLE_KEY` server-only; no model branding exposed to end users |
| **Cost-per-query transparency** | Never shown to the user — session pricing is the unit, not per-operation cost |
| **LLM output format** | All LLM responses use `response_format={"type": "json_object"}`; no free-form text parsing |

---

## Success Metrics

### V1 — Hackathon

| Metric | Target |
|--------|--------|
| Hackathon placement | Top 3, Stablecoins track |
| Live sessions run during demo | ≥ 1 end-to-end session on stage |
| Demo command | `bb research --idea "hotel guide in Brazil"` produces all three documents without error |
| Judge comprehension | Judges can explain the product back in one sentence |

### V2 — Post-Hackathon (Month 3)

| Metric | Target |
|--------|--------|
| Monthly active sessions | ≥ 100 sessions/month |
| Creator attribution claims | ≥ 10 creators claim profiles |
| Web app conversion | ≥ 30% of web visitors start a session |
| Pro tier adoption | ≥ 20% of sessions on Pro |

### V3 — Marketplace (Month 6)

| Metric | Target |
|--------|--------|
| Marketplace GMV | $5,000/month in creator earnings settled |
| Knowledge API integrations | ≥ 3 external tools querying session knowledge bases |
| SuperteamBrasil sessions | ≥ 50 sessions from LATAM developer community |

---

## Explicitly Out of Scope

| Item | Reason |
|------|--------|
| Per-query micropayments ($0.005/query) | Creates friction on every action; session pricing is the right unit |
| "Powered by GPT-4o" or model branding | Erodes product identity; implementation detail, not value |
| Raw embeddings or vector scores in output | Plumbing; not product |
| Gecko on-chain deal layer | Separate protocol play; 2027+ timing |
| Oracle-driven social media attestation | V3 item; manual sponsor review for V1 |
| Multi-tenant SaaS with team workspaces | V3 item; single-user sessions for V1/V2 |
| On-chain program source | Deployed via x402 infrastructure; not in this repo |

---

*Builder Bootstrap Platform · Ernani Britto · April 30, 2026 (v1.1 — S11-PRD-01 ICP convergence)*
