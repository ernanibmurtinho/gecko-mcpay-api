# Sprint 13+ — software-engineer memo

**Date:** 2026-04-30  **Lens:** Python implementation in `packages/` and `apps/cli`. Code-correctness owner. Defers payments to web3-engineer, schema to data-engineer, prompts/eval to ai-ml-engineer.

---

## Theme 1 — Lifecycle monetization (research / plan / pulse)

**Sequencing.** Sprint 13 wedge, full lifecycle Sprint 14-15. Implementation-readiness is high: `gecko_research` and `gecko_plan` already cohabit in `packages/gecko-mcp/src/gecko_mcp/server.py` and share a `session_id` UUID anchored in `packages/gecko-core/src/gecko_core/sessions/store.py`. The seam exists; what's missing is *recurrence*.

**Smallest wedge.** Add a `lifecycle_phase` enum (`research|plan|pulse`) on the existing session row and a thin `LifecycleSnapshot` Pydantic model in `gecko_core/sessions/` that captures `{phase, parent_session_id, delta_summary, created_at}`. Ship `gecko_pulse` as an MCP tool that takes a parent `session_id`, reads the prior snapshot, re-runs discovery against a *delta window* (sources newer than `parent.created_at`), and writes a new snapshot row. No new orchestration engine, no new debate loop — same `workflows.py` path with a phase flag.

**Shared-state primitive — yes, but minimal.** Don't build a state machine. The `session_id` already is the primitive; we extend it with a self-referential `parent_session_id` FK and a phase tag. Pulse state = N rows in the same table linked by parent. This keeps `gecko-core` the single source of truth and lets MCP/CLI/API stay thin.

**Risks in my lane.** (a) Cache key collisions: `cache/` keys today encode `session_id` + idea hash; pulse re-runs the same idea and will hit stale cache unless we add `phase` + `as_of_ts` to the key. F18-style stale-result bug waiting to happen. (b) `workflows.py` will balloon past 300 lines if I bolt phases on inline; split into `workflows/research.py`, `workflows/plan.py`, `workflows/pulse.py` before adding pulse. (c) Concurrent pulse calls on the same parent — need a row-level lock or idempotency key.

**Cross-lane deps.** data-engineer first (migration: `lifecycle_phase`, `parent_session_id`, partial index on `(parent_session_id, created_at)`). ai-ml-engineer for pulse-specific prompt + fixture. business-manager for per-pulse pricing.

---

## Theme 2 — Paragraph connector

**Sequencing.** Sprint 14. Blocked on Sprint 12 Track F `SourceProvider` Protocol landing. Premature otherwise — we'd build a one-off and rewrite it.

**Smallest wedge.** A `ParagraphProvider` implementing `SourceProvider` in `packages/gecko-core/src/gecko_core/sources/paragraph.py` that fetches public posts only (no payment hop yet), wired into `sources/dispatcher.py`. Prove the ingestion shape works end-to-end before adding the creator-payment hop.

**Where the payment hop fits.** Not inside the existing pipeline (`ingestion/pipeline.py`) — that path is sync and side-effect-free for testability. Instead, introduce a `PayingProvider(SourceProvider)` Protocol subtype in `sources/__init__.py` that adds one method: `async def settle(chunk_ids: list[UUID], session_id: UUID) -> SettlementReceipt`. The dispatcher detects the subtype after chunks are persisted and *before* RAG selection finalizes (so we only pay for chunks that actually enter the verdict context, not everything we crawled). Settlement itself goes through `gecko_core/payments/` — web3-engineer owns that surface; I just call it.

**Risks.** (a) Double-payment if pulse re-ingests the same Paragraph post — need an idempotency table keyed on `(provider, source_url, session_id)`. (b) Partial-failure: chunks persisted, settlement fails — need a `settlement_status` enum and a retry job, otherwise we owe creators silently. (c) SSRF/URL validation on creator-controlled redirects.

**Cross-lane deps.** staff-engineer for the `PayingProvider` Protocol shape (it's a cross-package contract). web3-engineer for the x402 settle call. data-engineer for the settlement-status table.

---

## Theme 3 — Cloudflare x402

**Sequencing.** Consumer-side: Sprint 14, trivial once `SourceProvider` lands — a `CloudflareGatedProvider(PayingProvider)` reusing the same hop as Paragraph. Edge-deployed gecko-api on Workers: defer to Sprint 16+ option set. FastAPI → Workers is a port, not a feature, and we'd lose AutoGen GroupChat (no Python on Workers).

**Smallest wedge.** One concrete Cloudflare-gated test source behind the Provider Protocol with `X402_MODE=stub` round-trip recorded as a fixture. Zero new architecture — it's a config + one provider class.

**Risks.** Mostly none in my lane if we treat it as "another paying provider." The trap is letting "deploy on Workers" sneak in.

**Cross-lane deps.** web3-engineer (Cloudflare x402 client), staff-engineer (defer the Workers question explicitly).

---

## Theme 4 — App-launching template

**Sequencing.** Sprint 15 earliest. Big package, needs the lifecycle work landed first so the scaffold can include real verdict artifacts.

**Generator vs hosted scaffolder — pick generator.** Argument: (a) we already ship Cookiecutter-shaped output via `apps/cli` quickstart (`packages/gecko-mcp/src/gecko_mcp/quickstart.py` and `project.py`) — the muscle exists. (b) A generator has zero ongoing ops cost; a hosted scaffolder makes us a PaaS and pulls us off the validation thesis. (c) Distribution matches: Claude Code skill says `Read app.geckovision.tech/skill.md → bootstrap` — that's a generator pattern. (d) "Lock for agents" is config the builder owns; we shouldn't custody it. Hosted is a Sprint 18+ upsell once 100+ generated apps prove demand.

**Smallest wedge.** New `packages/gecko-launcher` with one template (`content-api`) and a `gecko launch app --template=content-api --domain=<x>` CLI subcommand that emits a directory containing: `x402` middleware stub, `bazaar.toml` registration metadata, frames.ag wallet config placeholder, README. No Gecko verdict pre-roll yet — that's iteration 2.

**Risks.** (a) Template drift: shipped templates rot when x402/Bazaar SDKs version-bump — need a `gecko launch doctor` that re-validates an emitted scaffold. (b) Logic leaking out of `gecko-core`: scaffold templates are *data*, not logic, but it's tempting to put generation rules in `gecko-launcher`. Keep generation rules in `gecko-core`, templates as inert files. (c) Secrets in emitted code — must validate no env values leak into the scaffold output.

**Cross-lane deps.** web3-engineer for the x402 middleware stub shape. product-designer for README/walkthrough copy. staff-engineer for the package boundary call.

---

## Sprint 13 ticket I commit to

**S13-CORE-01 — Lifecycle phase primitive on Session**  (~4 days)

Unlocks: lifecycle monetization (Theme 1), Paragraph connector (Theme 2 needs `parent_session_id` for re-ingestion idempotency), every downstream pulse-related ticket from ai-ml/business-manager.

**Scope.**
1. Split `packages/gecko-core/src/gecko_core/workflows.py` into `workflows/research.py` and `workflows/plan.py` (no behavior change).
2. Add `LifecycleSnapshot` Pydantic model in `gecko_core/sessions/` and `phase: Literal["research","plan","pulse"]` + `parent_session_id: UUID | None` fields on the existing session model.
3. Wire MCP `gecko_research` to set `phase="research"`, `gecko_plan` to set `phase="plan"` and require a parent.
4. Add `phase` and `as_of_ts` to cache keys in `gecko_core/cache/` (prevent F18-class staleness).
5. Stub `gecko_pulse` MCP tool that returns 501 with a structured payload — proves the surface, defers behavior to S14.

**Acceptance.**
- `uv run mypy --strict packages/gecko-core` passes.
- `uv run pytest` green; new tests cover (a) parent-child session linkage, (b) cache-key isolation across phases, (c) stub `gecko_pulse` returns the documented 501 shape.
- `bb research --idea "smoke test"` end-to-end in `X402_MODE=stub` sets `phase="research"` on the persisted session row.
- No file in `gecko-core` exceeds 300 lines after the workflows split.
- Migration coordinated with data-engineer (column add only; backfill `phase="research"` for existing rows).

---

## Files referenced

- `/home/nan/PycharmProjects/Gecko/gecko-mcpay-api/packages/gecko-core/src/gecko_core/workflows.py`
- `/home/nan/PycharmProjects/Gecko/gecko-mcpay-api/packages/gecko-core/src/gecko_core/sessions/store.py`
- `/home/nan/PycharmProjects/Gecko/gecko-mcpay-api/packages/gecko-core/src/gecko_core/sources/dispatcher.py`
- `/home/nan/PycharmProjects/Gecko/gecko-mcpay-api/packages/gecko-core/src/gecko_core/ingestion/pipeline.py`
- `/home/nan/PycharmProjects/Gecko/gecko-mcpay-api/packages/gecko-mcp/src/gecko_mcp/server.py`
- `/home/nan/PycharmProjects/Gecko/gecko-mcpay-api/packages/gecko-mcp/src/gecko_mcp/quickstart.py`
