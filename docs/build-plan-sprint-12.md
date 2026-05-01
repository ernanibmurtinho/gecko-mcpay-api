# Sprint 12 — CDP Bazaar listing + Base settlement parallel

**Status:** draft (proposed)
**Predecessor:** Sprint 11 (in-flight: verdict unification, F18 fix, ICP convergence, landing v2)
**Driver:** `docs/research/cdp-bazaar-2026-04-30.md` — landscape probe found the "founder validation" semantic slot in CDP Bazaar is uncontested at quality scale. Gecko would land as the highest-quality entry day one. Listing requires CDP Facilitator settlement, which also unlocks Base/EVM payer reach for free.

**Done = Gecko's `gecko_research` (basic + pro) and `gecko_plan` are listed in CDP Bazaar with full input/output schemas, settled at least once on Base mainnet through the CDP Facilitator, and discoverable via `/discovery/search?query=founder+validation` ranked at or above the OrbisAPI proxies.**

---

## Why this sprint, why now

Three things converged:

1. **Sprint 11 ships verdict unification.** Once `KILL/REFINE/BUILD` is the canonical output shape, the API surface is stable enough to declare in a JSON Schema for Bazaar.
2. **The thesis sub-fold ("validation layer above frames.ag") is launching with Sprint 11's landing v2.** Bazaar listing extends that claim from "above frames.ag" to "above any x402 facilitator" — which is a stronger position.
3. **The slot is empty now.** OrbisAPI is scaffolding generic proxies but has no real users. First-mover advantage in semantic ranking compounds; waiting another sprint risks losing the empty-slot moment.

---

## Tracks

### Track A — CDP Facilitator settlement plumbing (S12-CDP-01..03) **CRITICAL**

The unblocker. Bazaar listing requires settlement through the CDP Facilitator at `api.cdp.coinbase.com/platform/v2/x402`. Today gecko-api settles through frames.ag's Solana flow only.

- **S12-CDP-01 — Add CDP Facilitator client to `packages/gecko-core/payments/`.**
  Parallel to existing `LiveX402Client`. New module `cdp_x402_client.py`. Wraps the x402 v2 SDK Python equivalent (or hand-rolled if SDK is JS-only — likely needs investigation). Configurable via `X402_MODE=cdp` (alongside existing `stub | live | frames`). Settlement target: Base mainnet USDC. Requires CDP API keys in `.env` (`CDP_API_KEY_ID`, `CDP_API_KEY_SECRET`).

- **S12-CDP-02 — Network-aware payment client factory.**
  In `packages/gecko-core/payments/__init__.py`, factory chooses CDP vs frames.ag based on `X402_NETWORK` resolution: Solana → frames.ag, Base/EVM → CDP. Add `NetworkKind.BASE_MAINNET` to the existing enum (Sprint 8 S10-LIVE-03 added Solana variants). Update `bb doctor` to show which facilitator is active for each network.

- **S12-CDP-03 — Per-call confirmation + reconcile for CDP path.**
  CDP returns settlement confirmations differently than frames.ag. Map both into the existing `PaymentReceipt` shape so `bb economics <session_id> --verify` works uniformly. Add tests with both fixture flows.

**Owner:** web3-engineer
**Acceptance:** `X402_MODE=cdp X402_NETWORK=eip155:8453 bb research --idea "test" --tier basic` settles a real $0.10 USDC payment on Base mainnet through CDP and returns a verifiable receipt.

### Track B — Bazaar extension declarations (S12-BAZAAR-01..03) **CRITICAL**

- **S12-BAZAAR-01 — Decorate `gecko-api` routes with Bazaar metadata.**
  Identify the routes that should be listed: `POST /research`, `POST /plan`, possibly `POST /ask`. For each, add the `bazaarResourceServerExtension` registration + `declareDiscoveryExtension()` call with full `inputSchema` + `output.example`. Best-practice descriptions per the Bazaar docs (semantic-search-friendly, e.g. "Adversarial multi-agent product validation: emits KILL/REFINE/BUILD verdict + cited evidence + 5-voice advisor panel" not just "/research").

- **S12-BAZAAR-02 — Resolve route consolidation risk.**
  Bazaar collapses bare-UUID path segments into one entry. `POST /research` is fine (no UUID), but any session-keyed route (`GET /research/{session_id}`) would consolidate. Audit current routes; for any route with bare ID segments, add a prefix (`/research/session-{uuid}`) before listing. Document in `docs/runbooks/cdp-bazaar.md`.

- **S12-BAZAAR-03 — Validation harness.**
  Add a CI check: parse each decorated route, confirm `extension.input` validates against `extension.schema.properties.input`. Fail build if any extension is malformed. Catches the "rejected" Bazaar-extension-validation case before deploy.

**Owner:** software-engineer
**Acceptance:** API serves up Bazaar metadata in payment responses; harness validates locally before any settle attempt.

### Track C — First-settle smoke + listing verification (S12-LIST-01) **CRITICAL**

- **S12-LIST-01 — End-to-end smoke: list Gecko in CDP Bazaar.**
  Steps (manual runbook, document in `docs/runbooks/cdp-bazaar-listing.md`):
  1. Deploy gecko-api to staging with CDP Facilitator wired (Track A) + Bazaar extensions declared (Track B).
  2. From a test client wallet (Base Sepolia USDC), pay one `gecko_research` call. Confirm `EXTENSION-RESPONSES` header on settle response shows Bazaar status `processing`.
  3. Wait for indexing (docs say async; budget 5-30 min).
  4. Query `https://api.cdp.coinbase.com/platform/v2/x402/discovery/search?query=founder+validation` and grep for our `payTo` address.
  5. Repeat for `gecko_plan` and any other listed routes.
  6. Once mainnet payment lands, verify all routes appear in the mainnet catalog too.

  Acceptance: ≥3 Gecko routes appear in `discovery/search` results, ranked above OrbisAPI proxies for queries: "founder validation", "product research MCP", "adversarial PRD".

**Owner:** web3-engineer (settle), software-engineer (verify)

### Track D — Wallet-neutrality positioning + Coinbase Agentic Wallet docs (S12-DOCS-01) **MED**

- **S12-DOCS-01 — Document wallet neutrality.**
  Add `docs/runbooks/wallet-options.md`: explain that any x402-capable wallet works — frames.ag (default for Claude Code skill installs), Coinbase Agentic Wallet (`awal`, for users who prefer Coinbase), custom (advanced). Cross-link from main README and from `gecko-claude` skill repo.

  Update landing copy v2 anti-positioning: change "Not crypto-first software" to remain accurate, and drop any frames.ag-exclusive language. Keep the frames.ag relationship visible (the Sprint 11 sub-fold "validation layer above frames.ag" is correct because that's our distribution path); but the **product** works above any wallet.

**Owner:** product-designer + business-manager (PRD update)

### Track E — Bazaar-as-source feasibility note (S12-RESEARCH-01) **LOW**

- **S12-RESEARCH-01 — Decide whether to consume Bazaar APIs as V2/V3 sources.**
  Probe top-quality entries in adjacent slots (Reddit, GitHub, news). Compare to Gecko's free sources today. Output: `docs/research/bazaar-as-source.md` with verdict — likely "wait, free works" but worth a structured look. **No code in this ticket.**

**Owner:** staff-engineer

### Track I — Production hardening before Bazaar listing (S12-HARDEN-01..04) **CRITICAL — gates Track C**

Per user decision 2026-04-30 (option B from `live-cdp-bazaar-smoke.md` pre-flight): listing in CDP Bazaar surfaces our 402 endpoint to public discovery. Risk is traffic-shaped (DDoS, abuse, prompt injection) not security-shaped (payment gate covers that), but worth a 4-item harden pass before flipping the listing.

- **S12-HARDEN-01 — Rate limit on `/research` and `/plan`.**
  Tighten slowapi config in `packages/gecko-api/src/gecko_api/main.py` so unauthenticated 402-path requests are bounded per IP (e.g. 30/minute/IP — generous enough for legitimate browsing, tight enough to make scraping unappealing). Test: hit endpoint 50× from one IP without payment, expect 429 well before 50. **Owner:** software-engineer.

- **S12-HARDEN-02 — Input size cap on `idea` field.**
  Pydantic field constraint: `idea: str = Field(..., min_length=10, max_length=2000)`. Body-size middleware: reject POST > 10KB at ASGI layer before route handler. Test: 100KB `idea` payload → 400, not OOM. **Owner:** software-engineer.

- **S12-HARDEN-03 — Production judge transcript capture.**
  S12-EVAL-01 archives transcripts for eval runs only. Add equivalent capture for **production** `/research` and `/plan` calls so every public verdict has an audit trail (compliance + post-hoc review of misranked verdicts surfaced by Bazaar). Storage: `judge_transcripts` Supabase table or filesystem with same schema as eval transcripts (`idea_id`, `judge_prose`, `parsed_verdict`, `agent_turns`, `actual_verdict`). **Owner:** ai-ml-engineer (schema + capture logic) + data-engineer (table migration if filesystem path is rejected).

- **S12-HARDEN-04 — Cost circuit breaker.**
  Track in-flight LLM spend per minute (in-memory counter or Redis). When > $5/min cumulative across all sessions, return 503 with `Retry-After` header for new POST `/research` and `/plan` requests until the rolling window cools. Existing in-flight calls complete normally. **Owner:** software-engineer.

**Acceptance:** all 4 sub-tickets green; one synthetic load test (50 rps for 60s with mix of valid/invalid payloads) shows expected 429s, 400s, 503s without OOM or crash.

**Track C (live smoke) gates on Track I.** Until I-04 lands, do not fire the smoke from a deployed environment.

### Track G — Eval transcripts + rubric v2 migration (S12-EVAL-01..02) **HIGH**

Per `docs/diagnostics/2026-04-30-verdict-regression-analysis.md` + `docs/diagnostics/2026-04-30-rubric-native-verdict-proposal.md`. Sprint 11 closed with the live-V1 gate at 0.60 (live-LLM noise + Track A nudge effect). Diagnosis is blind without raw judge transcripts; rubric still grades legacy `ship/kill/pivot` while pipeline emits `KILL/REFINE/BUILD`.

- **S12-EVAL-01 — Archive raw judge transcripts on every live run.**
  ~30 min, free. In `tests/eval/runner.py::_evaluate_one`, capture the judge's full prose response + the parsed verdict + per-voice closing lines. Write to `tests/eval/live_runs/<date>-<suite>-transcripts/<idea_id>.json`. Hard prerequisite for any future regression diagnosis. **Owner:** software-engineer.

- **S12-EVAL-02 — Rubric v2: native KILL/REFINE/BUILD grading.**
  ~3 days. Per ai-ml-engineer's proposal:
  - Add `expected_verdict_v2: KILL | REFINE | BUILD` to fixture schema (additive, backwards-compat with `expected_verdict`)
  - Relabel each fixture in `tests/eval/fixtures/` with v2 (REFINE is its own outcome, not collapsed into pivot)
  - Update rubric scoring in `tests/eval/rubric.py` (or wherever) to grade v2 if present, fallback v1
  - Re-baseline: run general/crypto/saas under v2 grading, expect ≥ 0.95 each
  - Re-baseline holdout_live with `--reruns 3` (~$10-15) for the structural threshold answer
  **Owner:** ai-ml-engineer (relabel + scoring) + software-engineer (schema + runner wire)

**Acceptance:** every live run dumps transcripts; rubric v2 grades all suites; holdout_live reruns-3 baseline ≥ 0.80 published.

### Track F — `SourceProvider` Protocol seam (S12-PROVIDER-01) **MED**

Per `docs/strategy/bazaar-composer-staff-eng-review-2026-04-30.md` §1a + §"Concrete asks". Pre-pay the architectural debt that turns Sprint 13's vertical-suite work (if the gate opens — see decision doc) from a 3-5 day refactor into a 3-day add. Saves us from breaking the eval gate during Sprint 13 if we go.

- **S12-PROVIDER-01 — Define `SourceProvider` Protocol; refactor existing dispatch through it.**
  - Add `packages/gecko-core/src/gecko_core/ingestion/providers/__init__.py` with the `SourceProvider` Protocol: `name`, `kind: Literal["free", "x402-bazaar", "first-party"]`, `cost_estimate()`, `health()`, async `fetch(query) -> list[SourceChunk]`.
  - Refactor existing `discover()` + `pipeline.ingest()` to dispatch through it. Existing Tavily/web/youtube paths become the default `FreeProvider` subclasses. **No new provider implementations this sprint.**
  - Add `Provenance` field on `Citation` so the verdict-renderer can surface evidence source without changing the verdict shape.
  - Add `IngestionResult.degraded_sources: list[str]` so the critic agent can surface gaps in-debate (per design memo's "turn failure into feature").
  - Tests confirm existing eval-gate suites still pass under the new dispatch.

**Owner:** software-engineer
**Cost:** ~1 day
**Acceptance:** existing live-V1 eval gate still passes at ≥ 0.80 under the new dispatch; Provenance visible on at least one citation in `bb research` output; no new providers wired.

---

## Out of scope

- Migrating Solana settlement off frames.ag (keep both; user picks per-call via `X402_NETWORK`)
- Submitting to Bazaar's MCP server as a co-listed MCP — that would require building Bazaar-flavored versions of our MCP tools; defer until ranking signals show whether it's worth the duplication
- Auto-detection of route consolidation collisions (ad hoc audit in S12-BAZAAR-02 is enough for V1)
- Implementing the V3 "Gecko provisions downstream agent budgets" surface (the thesis macro vision; multi-sprint arc)

## Acceptance (sprint-level)

- [ ] Real Base mainnet USDC payment settles through CDP Facilitator and returns a verifiable receipt
- [ ] At least 3 Gecko routes (`research`, `plan`, one more) appear in `/discovery/search?query=founder+validation`
- [ ] Gecko ranks at or above OrbisAPI proxies for the "founder validation" semantic slot (likely true day one given quality signal data)
- [ ] `bb doctor` shows correct facilitator per network; `bb economics` works for both Solana (frames.ag) and Base (CDP) settled sessions
- [ ] PRD + landing copy reflect wallet neutrality
- [ ] Pre-existing frames.ag flow on Solana mainnet still works (no regression)
- [ ] `SourceProvider` Protocol landed; live-V1 eval gate still ≥ 0.80 under new dispatch (Track F)

## Test plan

1. Stub mode regression: full pytest suite passes
2. CDP devnet smoke: pay a $0.001 Base Sepolia call, confirm extension status `processing`, see entry appear in sandbox catalog
3. CDP mainnet smoke: pay $0.10 Base mainnet call, confirm receipt + listing
4. Frames.ag mainnet smoke: pay $0.10 Solana mainnet call (Track 10-B carry-over, requires user-funded wallet) — confirm frames.ag path is unbroken
5. Bazaar discovery: run the 3 named search queries, verify Gecko in top results

## Reference

- `docs/research/cdp-bazaar-2026-04-30.md` — landscape probe driving this sprint
- `docs/positioning/2026-04-30-thesis-synthesis.md` — sub-fold positioning that Bazaar listing extends
- CDP Bazaar docs (user-supplied): semantic-search endpoint structure, quality-ranking signals, MCP server surface
- `packages/gecko-core/payments/x402_client.py` — current LiveX402Client (frames.ag); Track A parallels its structure
