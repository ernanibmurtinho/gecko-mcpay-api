# Sprint 10 — Positioning validation + livenet readiness

**Status:** ready to fire
**Predecessors:** Sprint 8 (10 commits) + Sprint 9 (6 commits) → 16 commits closing the dogfood loop. `bb doctor` is now all-green. Research pipeline runs end-to-end with `gap_classification` + grouped precedents.
**Driver:** post-Sprint-9 dogfood surfaced **F17** (`bb` CLI doesn't load `.env` for non-doctor commands) plus the standing readiness gaps for going live: positioning, eval gate live-V1, demo runbook.

**Done = `bb` works zero-config from a fresh clone (assuming `.env` exists), one paid mainnet call has settled and reconciled, the research output for the Gecko pitch matches the landing page copy, and a one-page runbook walks a stranger from `curl install.sh` to first paid call.**

---

## Tracks

### Track A — `bb` zero-config UX (S10-CLI-01..02) **CRITICAL**

The unblocker. Without these, every dogfood + every demo requires manual `source .env` shell gymnastics.

- **S10-CLI-01 — Universal `.env` loading in `bb` entrypoint.**
  Move `dotenv.load_dotenv()` from `bb doctor` only into the top-level `bb` Click group (`apps/cli/src/gecko_cli/main.py`). Load BEFORE Click parses subcommand args so all commands inherit. Walk-up search: cwd → cwd parent → repo root (use `python-dotenv`'s `find_dotenv()`). Honor explicit `--env-file PATH` flag for testing/CI. Do not override values already set in shell (`override=False`). Add tests in `tests/cli/test_dotenv_loading.py`: cwd `.env`, parent `.env`, missing `.env`, shell-overrides-file precedence.

- **S10-CLI-02 — Fix the pre-existing `test_check_llm_resolution_warns_on_clawrouter_default` failure** in `tests/cli/test_doctor_command.py`. Carry-over from Sprint 8 Track B+F. Likely the test asserts an old behavior (`LLM_ROUTER` unset + clawrouter default → WARN) but Sprint 9 S9-CONFIG-03 changed the precedence rule. Update the assertion to match the new contract or drop it if obsolete.

**Owner:** software-engineer
**Acceptance:** running `bb plan <session>` from a fresh shell with no `source .env` works. `bb doctor`'s LLM ping AND `bb plan` agree on which router/model they hit.

### Track B — Live mainnet smoke (S10-LIVE-01..03) **CRITICAL**

The first real-USDC end-to-end run. Validates everything Sprint 8 Track C and Sprint 9 Tracks A/B/C built, on mainnet.

- **S10-LIVE-01 — Pre-flight checklist.**
  New script `scripts/live_preflight.sh`:
  - Confirms `X402_MODE=live` is OK to flip
  - Confirms client wallet has ≥ $1 SOL + ≥ $5 USDC (Helius RPC balance check)
  - Confirms `GECKO_WALLET_ADDRESS` resolves to user's own treasury (interactive Y/N)
  - Confirms `bb doctor` is all-green
  - Prints estimated cost ($0.05 plan + $0.10 research = $0.15 USDC roundtrip + ~$0.001 SOL gas + ~$0.05 OpenRouter)
  - Exit non-zero if any check fails
  No code changes to gecko-core/api — this is a runbook, not product code.

- **S10-LIVE-02 — One-call mainnet smoke + `bb economics --verify`.**
  Runbook in `docs/runbooks/live-mainnet-smoke.md`:
  ```
  X402_MODE=live X402_NETWORK=solana:<MAINNET_USDC_MINT> \
    bb --yes research --idea "Live mainnet smoke from Gecko" --tier basic
  ```
  Acceptance: real `tx_signature` (not `stub_`-prefixed); `bb economics <session_id> --verify` returns status `confirmed`; treasury balance increases by exactly the charge amount; reconcile webhook (if configured) fires. Document each step + expected output.

- **S10-LIVE-03 — Mainnet network ID resolution.**
  In `packages/gecko-core/src/gecko_core/payments/x402_client.py`, the network ID format is `solana:<MINT_ADDRESS>`. Add a small helper `_resolve_network_kind(network_id)` that maps known mainnet/devnet USDC mint addresses to a typed `NetworkKind` enum so error messages can say "you're on devnet, server expected mainnet" clearly. Unit test against the 2 known mints.

**Owner:** web3-engineer
**Acceptance:** one paid mainnet call lands a real Solana sig + reconcile passes.

### Track C — Positioning validation (S10-POSITION-01..02)

Now that the research pipeline produces real output, compare it to our landing copy. If the synthesizer doesn't say what the landing says, one of them is wrong.

- **S10-POSITION-01 — Run the 5-idea Gecko stress matrix; capture verdicts + gap_classifications + advisor closing lines.**
  Bash script `scripts/positioning_check.sh`:
  - Runs `bb research --idea "<variant>"` for each of the 5 Gecko-flavored ideas (the matrix from prior dogfood)
  - For each session: `bb plan` (5 voices)
  - Aggregates output to `docs/positioning/2026-04-30-gecko-self-research.md` with table: idea | verdict | gap_class | gap_summary | top 3 advisor closing lines
  - Stub mode by default, live mode behind `--live` flag

- **S10-POSITION-02 — Compare landing copy to positioning output.**
  Read `gecko-mcpay-landing` repo (sibling repo, path likely `../gecko-mcpay-app/` or similar — find via Glob). Extract hero headline, subhead, value bullets from the landing page. Cross-check against the consensus from the 5-idea positioning run. Output `docs/positioning/landing-vs-research-delta.md` flagging:
  - Claims on the landing page that the research disagrees with
  - Claims the research surfaces that aren't on the landing page
  - Vocabulary drift (we say "AI co-founder" — does the research say that, or "research agent"?)
  Surface deltas as a list of "consider updating landing copy" + "consider updating prompt persona".

**Owner:** product-designer (for landing read) + software-engineer (script + diff)

### Track D — Eval gate live-V1 (S10-EVAL-01)

The standing $5–7 spend that's been deferred since Sprint 4. Now unblocked because ingestion works and the gap_classification gives the eval suite a structured field to grade.

- **S10-EVAL-01 — Run `scripts/run_eval_gate_live.sh` against the holdout-live suite.**
  Live LLM + live Tavily ingestion. Pass thresholds: general ≥ 0.95, crypto ≥ 0.95, saas ≥ 0.95, holdout 1.0, **holdout-live ≥ 0.80** (matching `scripts/run_eval_gate_live.sh` PASS_THRESHOLD; live V1 signal is noisier than curated fixtures — re-tighten in Sprint 11 once we have 2–3 baseline runs).
  Update threshold table in `docs/test-plan.md`. Capture the run cost in `docs/eval/live-v1-results-2026-04-30.md` with per-suite breakdown.

**Owner:** staff-engineer (review) + run by software-engineer

### Track E — Demo runbook (S10-DEMO-01)

The thing a stranger reads to go from zero to first paid call. Required for shipathon submission, demo videos, and any onboarding partner.

- **S10-DEMO-01 — `docs/demo/quickstart.md`:**
  10-minute path from `curl ... install.sh | bash` → `claude code` → `Read app.geckovision.tech/skill.md` → frames OTP → fund (devnet faucet OR mainnet wallet) → first `gecko_research` → see verdict + gap classification + advisor panel → `bb economics` shows receipt.
  Each step: command + expected output (truncated). Include common errors + fixes (e.g. `LiveX402Client.charge` errors, network mismatch, OpenRouter 429).
  Cross-link from main `README.md` and from `gecko-claude` repo's README.

**Owner:** product-designer

---

## Out of scope

- Mainnet hard-flip in `.env` (still per-call override only)
- V3 dashboard (cross-repo)
- Auto-labeling of precedents (Sprint 11)
- Colosseum API as a source (deferred — interesting but low leverage right now)
- Sprint 9 follow-ups: `bb sprint-review` rendering with no project_id is acceptable; the auto-discover landed in S8-REVIEW-01 — not a blocker

## Acceptance (sprint-level)

- [ ] `bb plan <session>` works from a fresh shell with no env override
- [ ] One mainnet `bb research` call lands a real `tx_signature` and reconciles green
- [ ] `docs/positioning/landing-vs-research-delta.md` published with concrete deltas
- [ ] Live-V1 eval gate published with all suite thresholds met
- [ ] `docs/demo/quickstart.md` walks a stranger end-to-end in ≤ 10 minutes
- [ ] Pre-existing `test_check_llm_resolution_warns_on_clawrouter_default` no longer failing

## Test plan

After all tracks land:
1. **From a fresh shell** (no `source .env`): `bb doctor` → all green; `bb research --idea "test"` → success; `bb plan <session>` → 5/5 voices.
2. **Mainnet smoke** following the runbook → real sig + reconcile.
3. **Eval gate** runs to completion under thresholds.
4. **Quickstart walkthrough** done by someone who didn't write it (informal test).

## Reference

- `docs/audits/integration-audit-2026-04-30.md`
- `docs/community/colosseum-research-deep-dive.md`
- Findings F13–F17 (this dogfood arc)
- Sprint 9 commits: `0f82c25`, `dc9d120`, `5884669`, `953c73f`, `8a81306`, `ca93a06`
