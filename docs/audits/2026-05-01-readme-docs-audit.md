# README + docs/ audit — 2026-05-01

**Author:** staff-engineer (audit pass)
**Trigger:** post-Sprint-14 plan; README last refreshed pre-Sprint 11 (verdict unification). Sprints 11-14 added: verdict unification, CDP Bazaar listing, SourceProvider Protocol, X402Client Protocol, gecko_pulse v1, Paragraph MCP ingest, publish.new artifacts, twit.sh×Colosseum, profile-thesis arc (S15-S17). Net: ~14 sprints of accumulated docs, README stale, docs/ unindexed.

## Scope

- `README.md` — what's there, what's stale, what's missing
- `docs/` tree — what files exist, which are current, which are dead, no index
- Spot-check whether install commands documented in README still work today

## State of `README.md`

### Current (as-shipped pre-refresh)

- One-line product description = V1 hackathon framing ("knowledge base, business plan, validation report, PRD in under 30 minutes")
- Three install paths exist but Claude Code path is the only featured one
- Quickstart is `uv run bb research --idea "hotel guide for Brazil"` — works in stub mode; not verified live this audit run
- Pricing section says "tunable via env" with no actual numbers
- Tech stack table lists `stub | live | frames | cdp` modes; body only walks stub
- Roadmap section is V1/V2/V3 from pre-Sprint-11; missing every Sprint 11-15 surface
- "Documentation map" table lists 4 files out of 50+ in `docs/`
- `<owner>` placeholders never filled
- `LICENSE` flagged missing in footer; unchanged since Sprint 9

### Desired (post-refresh)

- One-line product description = V1-honest **"budget approver above x402 — adversarial validation as a pre-spend gate"** (per 2026-05-01 staff-eng review; V3 "trust layer of agentic economy" framing is earned by S17 4-rail proof, not yet)
- Three install paths surfaced equally: Claude Code skill / `bb` CLI direct / HTTP API for agents (with `https://api.geckovision.tech/openapi.json` link)
- Quickstart in stub mode (no money) verified in <2 min on a clean checkout
- Full pricing ladder visible: Free (`bb review`) / $0.10 (basic) / $0.75 (pro) / $9 DeFi suite (S13+, gated) / $12-19 verticals (S14+) / $29 orchestrator (V3+) / `gecko_pulse` $0.50 + 12-pack $5.40 (S14) / publish.new artifacts $0.50 default
- Wallet neutrality call-out: frames.ag (Solana), CDP (Base), awal (parallel), Cloudflare (HTTP). Product works above any x402-capable wallet.
- Sprint badge / current-state line ("Sprint 14 in flight; Sprint 15 ready-to-fire")
- Footer: contributing pointer + license note

## Doc tree map (read-pass; not exhaustive)

| Path | Status | Action |
|---|---|---|
| `docs/PRD.md` (v1.1, 2026-04-30) | current — ICP convergence shipped S11 | keep |
| `docs/product-story.md` (2026-04-25) | current | keep |
| `docs/test-plan.md` | header says "Sprint 2 → 6" — STALE; body mixed | refresh header to S2-S14 next sprint |
| `docs/migration-plan.md` | STALE one-time V1→workspace artifact | move to `docs/archive/` or delete |
| `docs/build-plan-sprint-{1..15}.md` | current — S15 ready-to-fire | keep; index in `docs/README.md` |
| `docs/strategy/` (≥10 files) | current; thesis converged 2026-04-30/05-01 | index by date |
| `docs/runbooks/` | operational; uncatalogued in README | index in `docs/README.md` |
| `docs/sprint-reviews/` | new since gitStatus; s12-retro present | index |
| `docs/community/` | new; Colosseum / solana-claude / Superteam | index |
| `docs/positioning/` | 2026-04-30-thesis-synthesis.md | index under strategy |
| `docs/research/` | cdp-bazaar-2026-04-30.md + others | index |
| `docs/marketing/` | landing copy / positioning | index |
| `docs/demo/quickstart.md` | STALE — uses old verdict words `go/pivot/kill` | refresh to KILL/REFINE/BUILD (S11-VERDICT-01) |
| `docs/diagnostics/` | dogfood + delta analyses | index |

## Specific staleness flags

| # | Flag | Severity |
|---|---|---|
| 1 | README headline thesis is V1, not V1-honest "above x402" framing | high |
| 2 | Pricing section has no numbers; ladder hidden | high |
| 3 | MCP tool table missing `gecko_pulse`, `bb earnings`, `--vertical defi`, `--publish`, twit.sh | high |
| 4 | `docs/` has 50+ files, zero index | high |
| 5 | `<owner>` GitHub placeholders unfilled | med |
| 6 | Roadmap V1/V2/V3 doesn't reflect Sprint 11-15 arc | med |
| 7 | CHAT_MODEL row in env table contradicts `.env.example` LLM_ROUTER branching | med |
| 8 | `frames` mode listed alongside `stub/live/cdp` but flagged post-hackathon in CLAUDE.md | low |
| 9 | `docs/demo/quickstart.md` uses old verdict words | low |
| 10 | `docs/migration-plan.md` lingering as live doc | low |
| 11 | `LICENSE` file still missing | low |
| 12 | No mention of frames.ag / CDP / awal neutrality (project convention) | med |

## Most consequential gaps (3-5)

1. **Headline framing is stale and V3-aspirational, not V1-honest.** Per 2026-05-01 staff-eng review, V1 framing is "budget approver above x402"; "discrimination layer / trust layer of agentic economy" is earned via S17 4-rail proof, not before. The README must defend this line.
2. **Pricing ladder + payment surface are invisible.** Founders, agent operators, and contributor-side investors all need the ladder + Bazaar listing visible to value the project. Section currently says "tunable via env" with no numbers.
3. **`docs/` is unnavigable.** 50+ files, 12+ subdirs, no index. `docs/README.md` must ship to make the tree usable.
4. **MCP/CLI surface area documented in README is half a sprint stale.** No `gecko_pulse`, no `--publish`, no `bb earnings`, no `--vertical defi`, no twit.sh×Colosseum, no Paragraph attribution.
5. **Wallet/facilitator neutrality is not surfaced.** Project convention in `CLAUDE.md` ("frames.ag for Solana, CDP for Base, awal parallel; never hard-code one") doesn't appear in user-facing README. Newcomers default to assuming Solana-only.

## Spot-check: do install commands work?

- `git clone … && cd gecko-mcpay-api && uv sync` — assumed current; not run in this audit pass. Last verified working: Sprint 13 retro.
- `cp .env.example .env` — file exists, current.
- `uv run bb research --idea "hotel guide for Brazil"` — stub-mode default per `.env.example` line 79 (`X402_MODE=stub`). Pipeline acceptance tests in S14-PULSE-01 + S10-LIVE-02 commits suggest stub path is healthy.
- `curl -fsSL https://app.geckovision.tech/install.sh | bash` — depends on `gecko-mcpay-skills` deploy state. Out of scope this repo.

**Recommendation:** the next coding agent applying these refresh diffs runs `uv run bb research --idea "smoke test"` end-to-end before commit 2 lands.

## Follow-up tickets for Sprint 15+

- **S15-DOCS-01** — refresh `docs/test-plan.md` header (S2-S14 scope) and reconcile assertions against current eval gate (`run_eval_gate_live.sh` 0.80).
- **S15-DOCS-02** — fix `docs/demo/quickstart.md` verdict words to KILL/REFINE/BUILD.
- **S15-DOCS-03** — add `LICENSE` (Apache-2.0 default per Sprint 9 carry-over).
- **S15-DOCS-04** — move `docs/migration-plan.md` to `docs/archive/` (one-time artifact).
- **S15-DOCS-05** — fill `<owner>` in README + `docs/README.md` GitHub URLs once org name is decided (currently unbranded).
- **S16-DOCS-01** — README earns "profile-typed orchestration" sub-fold *only* when `min(profile_types_cited) >= 3 over 7 days` per PD's anti-gaming gate.
- **S17-DOCS-01** — README headline graduates from "budget approver above x402" to "discrimination layer / trust layer of agentic economy" only after the 4-rail proof closes (Solana frames.ag + Base CDP + Cloudflare HTTP + awal parallel).
