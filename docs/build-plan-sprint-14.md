# Sprint 14 — `gecko_pulse` v1 + Paragraph + publish.new artifacts

**Status:** ready to fire (drafted 2026-05-01 from Stage 3 dogfood; awaits Sprint 13 closure)
**Predecessor:** Sprint 13 (DeFi suite + Phase Primitive Seam + X402Client Protocol + creator citations + commoditization SKUs)
**Driver dogs:**
- `docs/strategy/2026-05-01-stage3-dogfood-results.md` — Stage 3 findings (qualitative; live re-run queued)
- `docs/sprint-reviews/2026-05-01-s12-retro.md` — Sprint 12 architectural lessons
- `docs/strategy/paragraph-publish-new-expansion-2026-04-30.md` — Theme 2 + 2b
- `docs/build-plan-sprint-13.md` — what S14 inherits

**Done = `gecko_pulse` v1 ships at $0.50/call with a 12-pack prepay SKU; Paragraph posts ingest as a `SourceProvider` with creator payouts surfaced in citations; every research run optionally publishes its verdict to publish.new at $0.50; the X402Client Protocol seam is the single path every consumer goes through; the 3 latent CDP issues are closed.**

---

## Track A — `gecko_pulse` v1 (user-facing) **HIGH**

The first founder-facing pulse surface. Basic only — the delta renderer ships in
S15. Sprint 14's pulse v1 lets a founder re-validate their idea at a fraction of
the full Pro price ($0.50 vs $0.75) by reusing the parent session's RAG context
and only re-firing the validation report against fresh sources.

- **S14-PULSE-01 — `gecko_pulse <session_id>` MCP tool + `bb pulse <session_id>` CLI.**
  Reads the parent session's idea + ICP + classifier signal; re-runs source discovery
  with `match_chunks_windowed(window_days=14)` from S13-PHASE-02; emits a fresh
  `ValidationReport` only (no new business plan, no new PRD). New session row with
  `phase='ongoing'` and `parent_session_id=<input>`. Charge $0.50 via the X402Client
  factory (S13-PAY-01).
  **Owner:** software-engineer (workflow + CLI) + ai-ml-engineer (validation prompt
  reuse from research)
  **Acceptance:** `bb pulse <uuid>` against an existing `complete` session produces a
  ValidationReport in <30s in stub mode; live mode lands a $0.50 charge; new row
  shows `phase=ongoing` and the FK to parent.

- **S14-PULSE-02 — 12-pack prepay SKU at $5.40 ($0.45/call, 10% bulk discount).**
  Add `pulse_credits` table (`user_wallet`, `credits_remaining`, `purchased_at`,
  `tx_signature`). `bb pulse` checks credits before firing the per-call charge;
  if credits ≥ 1, decrement and skip the x402 settle. Single `bb pulse buy-pack`
  command settles $5.40 once and credits 12 calls.
  **Owner:** web3-engineer (settle hop + credits ledger) + software-engineer (CLI)
  **Acceptance:** `bb pulse buy-pack` settles $5.40 in stub; 12 subsequent
  `bb pulse <uuid>` calls run free; the 13th re-prompts purchase or per-call $0.50.
  **Acceptance (live, optional in S14):** ditto on Base mainnet via CDP facilitator.

- **S14-PULSE-03 — Pulse renderer (basic; no delta yet).**
  Plain validation-report render with the parent session's idea inline at the top
  ("Pulse for: <idea>") and the fresh sources cited at the bottom. **Explicitly
  no delta output** — the user sees the new verdict, not the diff against the
  parent. Delta lands in S15.
  **Owner:** product-designer (Rich layout) + software-engineer
  **Acceptance:** stub-mode pulse run produces a 1-page report ≤ 60 lines; the
  parent session_id is surfaced in a footer for traceability.

- **S14-PULSE-04 — Pulse Bazaar listing.**
  Declare `gecko_pulse` as a paid CDP Bazaar route at $0.50 alongside the existing
  `/research` and `/plan` listings. Same discovery extension shape as S12-BAZAAR-01.
  **Owner:** software-engineer + business-manager (description copy)
  **Acceptance:** `/.well-known/x402` advertises the pulse route; route appears in
  Bazaar discovery probe; smoke test confirms `payTo` and `assetTransferMethod` are
  network-correct (closes the Sprint 12 lesson #4 risk).

---

## Track B — Paragraph MCP integration (`S14-PARA-*`) **HIGH**

Per `paragraph-publish-new-expansion-2026-04-30.md` Surface A — drop the original
custom-extractor scope, replace with thin MCP-client wrapper. Paragraph posts
become a `SourceProvider` instance via the S12-PROVIDER-01 seam, surfacing creator
attribution into the citation footer that S13-CITE-01 pre-paid.

- **S14-PARA-01 — Paragraph MCP client wrapper as a `SourceProvider`.**
  In `packages/gecko-core/src/gecko_core/ingestion/providers/`, add a
  `ParagraphProvider` that calls `https://mcp.paragraph.com/mcp` for `search`,
  `feed`, and `post.get` tools. Conforms to the S12-PROVIDER-01 Protocol. Returns
  `SourceCandidate` rows tagged with `provider_kind="paragraph"` and
  `creator_handle` populated from the post's author. Graceful degrade: if
  `mcp.paragraph.com` returns 5xx, the provider is skipped per the
  `degraded_sources` field — no pipeline halt.
  **Owner:** software-engineer
  **Acceptance:** unit test against a stubbed Paragraph MCP fixture returns 3
  candidates with handles populated; degraded-mode test with 503 returns empty
  list + degraded marker; pipeline keeps running.

- **S14-PARA-02 — Creator payout settlement on cite.**
  When a Paragraph-sourced chunk is cited in the final validation report, fire
  a $0.05 payout to the creator's wallet via Paragraph's payout layer (per the
  Theme 2 expansion plan — reuse Paragraph's settlement path, don't re-implement).
  Receipt records the tx hash on `Citation.provenance.payment` (typed via S13-PAY-01
  PaymentReceipt). Aggregate by creator at receipt-rendering time.
  **Owner:** web3-engineer (settle hop) + software-engineer (citation aggregation)
  **Acceptance:** stub mode renders the "Creator payouts: $0.05 → @author1, $0.03
  → @author2" footer block (the surface S13-CITE-01 staged); live mode lands actual
  Paragraph payouts on a test post.

- **S14-PARA-03 — Provider doctor + observability.**
  `bb doctor` adds a Paragraph row showing MCP reachability + last-known-good
  search response time. Cost circuit breaker (S12-HARDEN-04) extended to track
  Paragraph creator-payout aggregate per session — refuses to fire if the
  per-session creator-payout total exceeds the $0.30 cap (configurable via
  `GECKO_CREATOR_PAYOUT_CAP`).
  **Owner:** software-engineer
  **Acceptance:** `bb doctor` shows green/yellow/red on Paragraph MCP; a forced
  $0.40 creator-payout aggregate triggers the cap and refuses the citation
  (graceful: cite the Paragraph post without the payout; surface a warning).

---

## Track C — publish.new artifact publishing (`S14-PUB-*`) **HIGH**

Per Theme 2 expansion Surface B. Earns the "verdict as shared trust artifact"
framing 3 sprints earlier than the Sprint 17 four-rail proof. Single ticket in
S14 because the integration is small (one POST per artifact + one wallet-bridge
decision).

- **S14-PUB-01 — Auto-publish-after-research opt-in to publish.new.**
  After a `gecko_research` run completes, if the session has `publish_artifact=true`
  set (CLI flag `--publish` or MCP arg), POST the rendered ResearchResult to
  `https://publish.new` at $0.50 (founder-overridable via `--publish-price`).
  Title format: `Gecko verdict: <idea_summary> — <KILL|REFINE|BUILD>`. Body is the
  full rendered ResearchResult markdown. Author wallet decision per the Theme 2
  expansion's risk #3:
    - **Phase 1 (S14):** publish under Gecko's wallet, founder gets revenue share
      via a `publish_credits` ledger entry settled at $0.40 per sale (Gecko keeps
      $0.10 marketplace cut). Founder's frames.ag/Solana wallet does not need a
      Base address.
    - **Phase 2 (S15+, deferred):** wallet-bridge via CDP so founders publish
      directly under their own wallet.
  **Owner:** web3-engineer (publish.new POST + ledger) + software-engineer (CLI flag + opt-in flow)
  **Acceptance:** `bb research --idea "..." --publish` produces a publish.new URL
  in the receipt; opt-out (no `--publish` flag) is the default; the receipt shows
  "Published: <url> — earnable: $0.40/sale"; ledger row written.

- **S14-PUB-02 — `bb earnings` surface for published artifacts.**
  Read the `publish_credits` ledger and surface "Total earnable / earned this
  month" alongside `bb economics`. Founder withdraws earned balance at the same
  CDP/frames.ag bridge used for inbound payments.
  **Owner:** software-engineer + product-designer (Rich layout)
  **Acceptance:** `bb earnings` returns a 4-line summary; withdraw flow stubs
  cleanly (real settlement is S15+).

---

## Track D — Sprint 14 hardening (`S14-CDP-HARDEN-*`, `S14-PAY-MIGRATE-*`, `S14-HARDEN-*`) **MED**

Folds in the 3 latent CDP issues from the web3-engineer RCA + the "every consumer
goes through the seam" pass + the dogfood-surfaced backend-down findings.

### CDP latent issues (from web3 RCA)

- **S14-CDP-HARDEN-01 — `max_timeout_seconds` on outbound CDP calls.**
  Set explicit timeouts on every CDP HTTP call in `gecko_core.payments.cdp_x402_client`.
  Default 30s; configurable via `CDP_HTTP_TIMEOUT_SECONDS`. Hang-during-settle now
  fails fast instead of silently exhausting the gecko-api worker pool.
  **Owner:** web3-engineer
  **Acceptance:** unit test asserts the httpx client carries a finite timeout;
  forced delay in test fixture surfaces a `TimeoutError`.

- **S14-CDP-HARDEN-02 — `/.well-known/x402` advertisement contract.**
  Routes only advertise networks where the resolver registry has a matching
  wallet. Startup-time assertion fails fast if any route's `payTo` doesn't pass
  the network's address-format check (closes Sprint 12 lesson #4: Solana address
  advertised on Base routes).
  **Owner:** web3-engineer + software-engineer (api startup hook)
  **Acceptance:** existing `/.well-known/x402` listing unchanged; misconfigured
  fixture (Solana payTo on eip155:8453 route) raises `ConfigurationError` at
  startup.

- **S14-CDP-HARDEN-03 — `payTo` checksum encoding.**
  All ERC-20 `payTo` addresses are checksum-encoded (EIP-55) before they appear
  in `PaymentRequirements`. Mixed-case Solana base58 unaffected.
  **Owner:** web3-engineer
  **Acceptance:** unit test confirms checksum on a known-mixed-case address;
  /.well-known/x402 output is byte-stable across deploys.

### Every consumer goes through the seam (Sprint 12 lesson #5)

- **S14-PAY-MIGRATE-01 — `gecko_api` migrates payment router to `resolve_client(intent)`.**
  Drop the inline `if X402_NETWORK == ...` dispatch in `packages/gecko-api`.
  All payment work goes through `resolve_client` from `gecko_core.payments.factory`.
  **Owner:** web3-engineer + software-engineer
  **Acceptance:** existing tests pass; no `os.environ["X402_NETWORK"]` reads
  remain in `gecko-api` payment paths.

- **S14-PAY-MIGRATE-02 — `bb doctor` reads from `client.facilitator_id` + `client.supported_networks`.**
  Drop the env-derived display logic in `apps/cli/src/gecko_cli/commands/doctor.py`;
  doctor instantiates the resolved client (or factory) and reads from it.
  **Owner:** software-engineer
  **Acceptance:** `bb doctor` payment row shows the same info but is sourced from
  the Protocol, not `os.environ`.

- **S14-PAY-MIGRATE-03 — `gecko_mcp` payment introspection through the seam.**
  Any MCP tool that surfaces wallet/payment info (`gecko_economics`, `gecko_doctor`)
  reads from the Protocol.
  **Owner:** software-engineer
  **Acceptance:** MCP smoke test confirms doctor and economics responses are
  byte-stable post-migration.

### Backend-down resilience (from dogfood findings F-S14-1 + F-S14-2)

- **S14-HARDEN-01 — Local source-corpus cache for degraded mode.**
  Cache the last-known-good `chunks` rows for the most recent 50 sessions to a
  local SQLite file (`~/.gecko/cache.db`). When Supabase is unreachable,
  `bb research --degraded` runs the pipeline against the cache only — no fresh
  sources, but the validation report still emits with a `mode: degraded` warning
  in the receipt.
  **Owner:** software-engineer + data-engineer (cache schema mirroring chunks)
  **Acceptance:** `bb research --idea "..." --degraded` succeeds with Supabase
  unreachable (mocked) and produces a report with the degraded warning; live mode
  unaffected.

- **S14-HARDEN-02 — gecko-api maps Supabase 5xx to structured error.**
  Catch `postgrest.exceptions.APIError` with code in (502, 503, 522, 525, 526)
  in the `gecko_api` middleware; return `503 BACKEND_STORE_UNAVAILABLE` with
  `retry_after: 60` and a clear message ("session store temporarily unavailable").
  Frames.ag bridge surfaces this distinctly from `UPSTREAM_ERROR`.
  **Owner:** software-engineer + web3-engineer (frames.ag mapping)
  **Acceptance:** mocked Supabase 522 produces a 503 from gecko-api, not a 500.

### Dogfood-loop hygiene (from F-S14-4 + F-S14-5)

- **S14-DOGFOOD-01 — `bb sprint-review --write-to <path>`.**
  Add a `--write-to` flag (default `docs/sprint-reviews/<YYYY-MM-DD>.md`) so the
  meta-tool fully closes the dogfood loop without manual capture.
  **Owner:** software-engineer
  **Acceptance:** `bb sprint-review --since 14d` writes the canonical retro doc.

- **S14-DOGFOOD-02 — Parameterize `scripts/positioning_check.sh`.**
  Take an ideas file (one idea per line) instead of the hardcoded 5-idea array.
  Default to `docs/positioning/ideas/<latest>.txt` if it exists.
  **Owner:** software-engineer
  **Acceptance:** the script produces matrices for arbitrary idea sets;
  2026-05-01 deeper-thesis ideas re-run cleanly when Supabase recovers.

- **S14-DOGFOOD-03 — Live re-run of the 2026-05-01 deeper-thesis matrix.**
  Once Supabase is healthy, fire the 5-idea matrix per the dogfood brief; publish
  to `docs/positioning/2026-05-01-gecko-self-research.md`. Compare against the
  qualitative predictions in `docs/strategy/2026-05-01-stage3-dogfood-results.md`
  Step 2.
  **Owner:** staff-engineer (one-shot; no engineering)
  **Acceptance:** matrix doc has 5 populated rows; delta vs. predictions captured
  in a 1-paragraph note.

---

## Carry-over from Sprint 13

If Sprint 13 ends and any of these didn't land, they roll forward into S14
without re-prioritizing:

- **`S13-PAY-01`** — X402Client Protocol formalization. **Hard prereq for S14
  Track D.** If S13-PAY-01 doesn't ship, S14-PAY-MIGRATE-01..03 don't fire and
  S14-PULSE-02's settle hop falls back to inline if/elif. Capture this risk on
  the S14 daily standup.
- **`S13-WALLET-01`** — `bb wallet` panel implementation. If it slips, Track C
  (publish.new) does not depend on it (Phase 1 publishes under Gecko's wallet),
  but S15's wallet-bridge for direct founder publishing does.
- **`S13-COMMO-01..03`** — standalone advisor voice / session knowledge / classify
  pricing. Roll forward as-is; not a S14 dependency.
- **`S13-SUITE-01..05`** (DeFi vertical, gate-conditional) — if the gate failed
  in S12 retro, this becomes irrelevant. If the gate passed and the DeFi suite
  is still in flight, S14 does **not** absorb it; it stays in S13 to closure.

---

## Out of scope (S14 explicitly defers)

- **Pulse delta renderer** — the diff between parent session's verdict and pulse
  verdict. Ships S15 (Track A1: `S15-PULSE-DELTA-01`).
- **Wallet bridging for direct publish.new founder publishing.** Phase 2 of
  S14-PUB-01; ships S15.
- **Cloudflare Worker x402 integration** — uses the reserved S13-PAY-01 slot.
  S15.
- **Gecko Paragraph publication (Surface C).** Spinning up `paragraph.com/@gecko`
  with weekly trend reports. Theme 2 expansion explicitly schedules this for S15.
- **Surface D: Gecko earns x402 on Paragraph-MCP-to-MCP traffic.** Speculative,
  no S14 engineering.
- **App-launching template (S16+).**
- **Marketplace cut on hosted-scaffold apps.**
- **DeFi vertical suite live charging.** S13 territory; S14 inherits only if
  S13 ends without it landing (see carry-over).

---

## Acceptance (sprint-level)

- [ ] `bb pulse <session_id>` runs in <30s (stub) and lands $0.50 charge (live)
- [ ] `bb pulse buy-pack` credits 12 calls; subsequent pulses decrement without
      re-charging
- [ ] Paragraph posts ingest as a `SourceProvider`; creator payouts surface in
      the receipt footer
- [ ] `bb research --publish` produces a publish.new artifact URL; opt-out is
      default
- [ ] `bb earnings` shows ledger summary
- [ ] All 3 latent CDP issues closed: timeouts on outbound calls, /.well-known/x402
      advertisement passes startup assertion, payTo checksum-encoded
- [ ] No `X402_NETWORK` env reads remain in `gecko-api` or `apps/cli` payment
      paths (PAY-MIGRATE complete)
- [ ] `bb research --degraded` works with Supabase mocked-unreachable
- [ ] gecko-api maps Supabase 5xx to structured 503; frames.ag distinguishes
      `BACKEND_STORE_UNAVAILABLE` from `UPSTREAM_ERROR`
- [ ] `bb sprint-review --write-to <path>` closes the dogfood loop; the
      2026-05-01 deeper-thesis matrix is re-fired live and published
- [ ] No regression on holdout-live ≥ 0.80 under rubric v2 (carry-forward acceptance
      from S12 + S13)

---

## Test plan

After all tracks land:

1. **Pulse flow stub:** create a session via `bb research`, then `bb pulse <uuid>`.
   Verify session row has `phase=ongoing` and `parent_session_id` set; verify the
   ValidationReport is fresh (chunk window 14d) and not a copy of parent.
2. **Pulse 12-pack:** `bb pulse buy-pack` (stub charge); 12 subsequent pulses
   succeed without per-call charge; the 13th re-prompts.
3. **Paragraph degraded:** with `mcp.paragraph.com` mocked to 503,
   `bb research --idea "..."` succeeds with a `degraded_sources: [paragraph]`
   marker; no Paragraph citation in the report.
4. **Paragraph creator payout:** with the Paragraph MCP returning a real fixture
   post, `bb research` produces a citation with the creator handle inline and
   the receipt footer shows the aggregated payout.
5. **publish.new opt-in:** `bb research --idea "..." --publish` produces a
   publish.new URL; opt-out (no flag) does not.
6. **CDP hardening:** unit + integration tests for HTTP timeout, /.well-known/x402
   startup assertion, payTo checksum.
7. **PAY-MIGRATE smoke:** existing live-V1 holdout suite runs unchanged;
   `bb doctor` payment row sourced from `client.facilitator_id`.
8. **Degraded-mode research:** `bb research --idea "..." --degraded` with
   Supabase fixture-mocked to 522 produces a 1-page report with the degraded
   warning.
9. **Dogfood meta-tool:** `bb sprint-review --since 7d --write-to /tmp/r.md`
   writes a parseable retro doc.
10. **Live re-run of the deeper-thesis matrix:** S14-DOGFOOD-03 — produces a
    populated matrix at `docs/positioning/2026-05-01-gecko-self-research.md`.

---

## Spend / cost notes

- Stub mode: $0 across the sprint
- Live re-run of the matrix (S14-DOGFOOD-03): ~$0.50 OpenRouter (5 stub research
  calls; plan is free in stub if the LLM fails per F17 defensive logic)
- Live pulse smoke: ~$0.50 USDC settled via CDP on Base mainnet
- Live publish.new artifact smoke: ~$0.50 USDC + the artifact's $0.50 listing
  price (= $1 round-trip including buy-back-our-own-test-artifact)
- Total Sprint 14 live-validation budget: **≤ $5 USDC** across all smoke runs
  — well within Sprint 12 cost-circuit-breaker caps

---

## Reference

- `docs/strategy/2026-05-01-stage3-dogfood-results.md` — Stage 3 dogfood (this plan's primary input)
- `docs/sprint-reviews/2026-05-01-s12-retro.md` — Sprint 12 retro + architectural lessons
- `docs/build-plan-sprint-13.md` — what S14 inherits
- `docs/strategy/paragraph-publish-new-expansion-2026-04-30.md` — Theme 2 + 2b
- `docs/strategy/bazaar-deeper-thesis-2026-04-30.md` — "trust layer of the agentic economy"
- `docs/strategy/sprint-13+-web3-engineer-memo-2026-04-30.md` — X402Client Protocol shape
- `docs/strategy/wallet-panel-spec-2026-04-30.md` — for S14-PUB-02 ledger UX precedent
