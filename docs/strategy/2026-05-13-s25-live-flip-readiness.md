# S25 — x402 Live-Flip Readiness Handoff

**Date:** 2026-05-13
**Author:** `web3-engineer`
**Status:** READY FOR FOUNDER FLIP (pending the 4 operator-side blockers below)
**Branch:** `s25/wc-live-smoke`
**Memory pins:** `project_x402_stub_then_live`, `project_buyer_wallet_blocker_2026_05_08`

---

## TL;DR

Code-side and test-side are ready. Two new gates landed on `s25/wc-live-smoke`:

| Gate | Command | Status |
|---|---|---|
| Pattern B — no-spend simulation | `uv run python scripts/x402/simulate_live_flip.py` | PASS (3/3 phases) |
| Pattern C — recorded-fixture contract test | `uv run pytest tests/integration/test_x402_live_flip_contract.py -m live_x402_verdict -p no:warnings` | PASS (4/4) |
| Pattern C (sibling) — live buyer cassette | `uv run pytest tests/integration/test_live_buyer_path.py -m live_solana -p no:warnings` | PASS (already shipped on s24) |
| Runbook | `docs/runbooks/2026-05-20-x402-live-flip.md` | Updated — §1 pre-flight, §3.1 SSM read-only check, §3.2 ECS env wiring, §5.0 single-call canary, §5.1 cost cap, §7 rollback `/healthz` confirmation |

**Go/no-go call:** GO-on-code-side; NO-GO-overall until operator clears the four AWS-side blockers (§3) and founder issues explicit go-ahead (per `project_x402_stub_then_live`).

---

## What shipped this sprint

1. **`scripts/x402/simulate_live_flip.py`** — Pattern B falsification path. Runs in-process, never touches RPC. Three phases: gate-guard (allowlist + kill-switch matrix), receipt rendering (stub/leaky/live trichotomy), buyer-charge replay against the cassette. Exit 0 on full pass; exit 1 with itemised failures otherwise. Use this as the operator's "smoke before you smoke" check 5 minutes before the flip.
2. **`tests/integration/test_x402_live_flip_contract.py`** — Pattern C gate. Marker `live_x402_verdict` so it auto-deselects from the default sweep. Asserts that the cassette's full RPC method sequence (`sendTransaction` + `getSignatureStatuses`) is exercised by the dispatch branch — not just the verify path. Sprint-12 lesson encoded.
3. **Runbook update.** Pre-flight now blocks on (a) simulation pass, (b) contract test pass, (c) SSM existence check (READ-ONLY — no value retrieval), (d) `gecko-mcp doctor --live` green from operator laptop. Cost cap quantified ($5.50 for the 20-tx smoke; rollback at $7 spend if idempotency drifts). Rollback section now includes a `/healthz` confirmation step so the operator can see `effective_mode=stub` within 60s of the kill-switch flip.

---

## What is NOT done — the four operator-side blockers

Per `project_buyer_wallet_blocker_2026_05_08`, these are the steps only the founder can take. I do not run them.

1. **Create the buyer wallet** on Solana mainnet. Fund with ≥ $10 USDC. Record the pubkey.
2. **Put the keypair into SSM as SecureString** at `/gecko/prod/buyer_wallet_private_key_path` (value: the absolute path the secrets-volume init container will mount it at — recommended `/secrets/buyer.json`). Put the actual keypair JSON into a separate SecureString at `/gecko/prod/buyer_wallet_keypair` (or however the existing init container expects it). **Never paste the keypair into a Claude Code window.**
3. **Add the three companion SSM `String` parameters:**
   - `/gecko/prod/buyer_wallet_pubkey` — base58 pubkey from step 1
   - `/gecko/prod/x402_live_tools` — value: `gecko_trade_research,gecko_trade_research_pro`
   - `/gecko/prod/x402_kill_switch` — value: `false`
4. **Wire the ECS task definition** to read those five parameters via the `secrets:` block in runbook §3. Deploy via `./infra/deploy.sh --cert <ACM-ARN>` from `main` at the SHA being flipped (no local-only patches).

Until all four are done, `LiveBuyerX402Client` raises `BuyerWalletNotConfiguredError` at task start and the dispatcher falls back to `StubX402Client` — i.e. flipping `X402_MODE=live` without these steps is a no-op, but it's a confusing no-op. Do all four first.

---

## "Do not flip until" checklist — founder runs through before flipping `X402_MODE=live`

In order. Stop at the first NO.

- [ ] All four operator blockers above are complete.
- [ ] `aws ssm describe-parameters` (READ-ONLY) returns the five expected rows with the right types — see runbook §3.1.
- [ ] `uv run python scripts/x402/simulate_live_flip.py` on the SHA being deployed returns exit 0.
- [ ] `uv run pytest tests/integration/test_x402_live_flip_contract.py -m live_x402_verdict -p no:warnings` returns 4 passed.
- [ ] `uv run pytest tests/integration/test_live_buyer_path.py -m live_solana -p no:warnings` returns all passed.
- [ ] Buyer wallet shows ≥ $10 USDC on Solscan.
- [ ] `gecko-mcp doctor --live` from operator laptop pointed at prod reports healthy.
- [ ] Cofounder is online and acknowledged in DM within the last 10 minutes.
- [ ] Tuesday morning slot (09:00–11:00 BRT) is open. Not Friday. Not late evening.
- [ ] Single-call canary plan (runbook §5.0) is clear: operator runs `gecko-mcp doctor --live` in window A; separate Claude Code session in window B issues exactly one `mcp__gecko__gecko_trade_research` call with a benign prompt.
- [ ] Rollback rehearsed within the last 24h: kill-switch flip via SSM + `/healthz` shows `effective_mode=stub` within 60s.

If every box ticks: founder issues "GO" in DM, then runs `./infra/deploy.sh` with the live-mode CFN parameter overrides.

If any box is unticked: do not flip. Resolve the missing item, then redo the checklist from the top. There is no half-flip.

---

## Hard constraints honoured by this handoff

- No `X402_MODE=live` is written anywhere — not in tests, not in scripts, not in CI. The simulation passes `requested_mode="live"` as a function argument to the gate guard; the process env never carries the live value.
- No SSM SecureString value is read by any code in this PR. The runbook tells the operator what commands to run; they run them.
- No CI/CD or task-definition change has been deployed. CFN parameters are documented; the actual deploy is founder-gated.
- Branch is `s25/wc-live-smoke`, not `main` or `s24/wave-1-execution`.

---

## References

- Runbook: `docs/runbooks/2026-05-20-x402-live-flip.md`
- Simulation: `scripts/x402/simulate_live_flip.py`
- Contract test: `tests/integration/test_x402_live_flip_contract.py`
- Buyer client: `packages/gecko-core/src/gecko_core/payments/live_buyer.py`
- Gate guard: `packages/gecko-core/src/gecko_core/payments/__init__.py` (`assert_live_settlement_allowed`)
- Receipts: `packages/gecko-core/src/gecko_core/payments/receipts.py` (`build_receipt`)
- S24 close-out: `docs/strategy/2026-05-13-s24-closeout-synthesis.md`
- Memory: `project_x402_stub_then_live`, `project_buyer_wallet_blocker_2026_05_08`
