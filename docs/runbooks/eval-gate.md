# Eval Gate Runbook (S2X-15)

**Audience:** Ernani (founder, on-call). Run this once V1 sources have landed
and you're ready to authorize ~$42 of real spend to validate prompts v5 +
V1 sources before mainnet cutover.

**Decision context:** [`docs/decisions/0001-mainnet-after-v1-sources.md`](../decisions/0001-mainnet-after-v1-sources.md)
— mainnet cutover is gated on this gate passing.

**One-liner:**

```bash
./scripts/run_eval_gate.sh
```

---

## 1. Prereqs

Set in your shell **before** running:

```bash
export OPENAI_API_KEY=sk-...                    # 5 AG2 agents (gpt-4o-mini)
export ANTHROPIC_API_KEY=sk-ant-...             # Sonnet 4.6 rubric judge
                                                # CLAUDE_API_KEY also accepted
export GECKO_API_BASE=https://api.geckovision.tech   # default if unset
```

Repo state:

- On branch `main`.
- Working tree clean (`git status` shows nothing).
- Latest `main` deployed on devnet (the API base above must respond on `/healthz`).
- Prompts v5 active (the default; do **not** set `GECKO_PRO_PROMPTS_VERSION=v4`).

Sanity-check the non-paid scaffolding first:

```bash
uv run pytest tests/eval/test_gate.py -q
uv run ruff check
```

Both must be green before you spend a dollar.

---

## 2. What the gate does

1. Asserts env + repo preconditions; aborts on any miss.
2. Prints expected spend and prompts `y/N` to proceed.
3. Runs the three sub-suites sequentially in `--live` mode:
   - `general` (20 ideas) → `tests/eval/live_runs/<date>-general.json`
   - `crypto`  (15 ideas) → `tests/eval/live_runs/<date>-crypto.json`
   - `saas`    (15 ideas) → `tests/eval/live_runs/<date>-saas.json`
4. Reads `aggregate.verdict_accuracy` from each saved JSON.
5. Prints a pass/fail table. Exits 0 only if **all three ≥ 0.85**.

The runs are sequential by design — the API rate-limits aggressively under
parallel `/research` calls.

---

## 3. Expected runtime + spend

| Item | Estimate |
|---|---|
| Runtime (50 ideas, sequential, no reruns) | **30-45 min** |
| x402 devnet spend (50 × $0.75) | **$37.50** |
| AG2 agent tokens (gpt-4o-mini) | ~$3 |
| Rubric judge tokens (Sonnet 4.6) | ~$2 |
| **Total** | **~$42.50** |

Devnet x402 spend goes back to your devnet recipient wallet. The OpenAI +
Anthropic spend is real cash.

---

## 4. On failure (any sub-suite < 0.85)

The script exits non-zero and prints the failing sub-suites. **Do not
proceed to mainnet.** Recovery sequence:

1. **Roll prompts back to v4** for the live API while you investigate:

   ```bash
   aws ssm put-parameter --name /gecko-api/GECKO_PRO_PROMPTS_VERSION \
     --value v4 --type String --overwrite --region us-east-2
   aws ecs update-service --cluster gecko-api --service gecko-api \
     --force-new-deployment --region us-east-2
   ```

2. **Inspect the failing run JSON** under `tests/eval/live_runs/`:

   ```bash
   jq '.ideas[] | select(.actual_verdict != .expected_verdict) | {id, expected_verdict, actual_verdict, scores}' \
     tests/eval/live_runs/<date>-<suite>.json
   ```

   Look for patterns: which expected-`ship` cases are getting killed (false
   negatives) vs. which expected-`kill` cases are getting shipped (false
   positives). Cluster by `expected_categories`.

3. **File a prompt-rework follow-up ticket** (`S2X-15-followup-prompts-vN`)
   tagging the failing sub-suite and 2-3 representative idea IDs.

4. **Loop:** edit prompts in `packages/gecko-core/src/gecko_core/orchestration/pro/prompts/`,
   bump `GECKO_PRO_PROMPTS_VERSION` to `v6`, run mock-mode regression
   (`uv run python -m tests.eval.runner --suite all`), then re-run
   `./scripts/run_eval_gate.sh` once the mock baseline holds.

The mainnet cutover stays blocked until the gate clears.

---

## 5. On full pass (all three ≥ 0.85)

The script exits 0 and prints `S2X-15 GATE: PASS`.

1. Commit the new live-run JSONs:

   ```bash
   git add tests/eval/live_runs/<date>-*.json
   git commit -m "S2X-15: eval gate pass — verdict_accuracy >= 0.85 across all sub-suites"
   git push origin main
   ```

2. **Notify `web3-engineer`** to begin mainnet cutover per
   [`docs/runbooks/mainnet-cutover.md`](mainnet-cutover.md). Include the
   three live-run filenames + per-suite `verdict_accuracy` from the gate
   table.

3. The runbook's §1.7 ("Eval baseline exists") should be updated to point
   at the `<date>-general.json` run as the new canonical baseline.

---

## 6. FAQ

**Can I re-run a single sub-suite?**
Yes:

```bash
uv run python -m tests.eval.runner --suite crypto --live
```

But the gate decision (≥ 0.85 on **each** of the three) requires fresh runs
of all three on the same `main` SHA — don't mix and match across commits.

**What if a single idea blows up mid-run?**
The runner currently propagates exceptions. If a 402 fails or the API
times out on idea 23 of `crypto`, the suite aborts and you've burned the
spend on ideas 1-22 of crypto. Re-run that suite from scratch:
`uv run python -m tests.eval.runner --suite crypto --live`.

**Where do live runs go?**
`tests/eval/live_runs/YYYY-MM-DD-<suite>.json`. Same-day re-runs append
`-2`, `-3`, ... to avoid clobbering. The gate script auto-picks the newest
matching file via `ls -1t`.

**Do mock baselines need updating after a pass?**
No. Mock baselines (`tests/eval/baselines/<suite>_baseline.json`) are
deterministic and decoupled from prompt content; they exist to catch
rubric-itself bugs. The live-run JSON is the new artifact of record for
mainnet eligibility.

---

## 7. Live-V1 gate (S4-TWITSH-03)

A second, smaller gate runs the **holdout-live** suite (10 ideas) with the
twit.sh / HN / Reddit dispatcher *actually firing*. The gate above (§2-§5)
uses the curated `rag_context` fixtures from the suite JSONs, which is
right for prompt-stability comparisons but doesn't tell us whether v5.4
holds up against real V1-source noise. This gate fills that hole.

**One-liner:**

```bash
./scripts/run_eval_gate_live.sh
```

### 7.1 Prereqs

In addition to `OPENAI_API_KEY` + `ANTHROPIC_API_KEY`/`CLAUDE_API_KEY`:

```bash
export TWITSH_ENABLED=true
export TWITSH_WALLET_PRIVATE_KEY=0x...                 # Base mainnet signer
export TWITSH_WALLET_ADDRESS=0x7cc33a7BbA8409374f754f1f811BC63D1ea5bCFC
```

The gate script aborts (exit 2) if any of these are unset — without them,
the V1 dispatcher silently no-ops and the result is meaningless.

**Cache bypass (S11-F18-01).** `TwitshSource` writes a 6h Mongo result
cache keyed on `sha256("twit_sh:" + idea + "|" + categories_csv)`. The
key intentionally does **not** include a "live signal required" flag, so
two back-to-back live-V1 gate runs on the same suite would have the
second one served entirely from cache (`v1_sources_cost_usd = $0.00`).
That is exactly what happened on 2026-04-30 (see
`docs/eval/live-v1-results-2026-04-30.md`). To force a true cold run,
the gate script now exports `TWITSH_BYPASS_CACHE=true` by default.
Override with `TWITSH_BYPASS_CACHE=false` if you explicitly want to
exercise the cache path (e.g. cost-rehearsal without burning Base
mainnet ETH).

**Zero-spend WARN.** The runner prints
`WARN: --live-rag produced $0.00 V1 spend for idea=...` per idea when
the V1 dispatch returns zero. Treat it as a signal-quality alarm:
either the cache slipped through, the wallet is mis-configured, or the
idea didn't classify into any twit.sh-eligible category
(`crypto`/`defi`/`hackathon-team`).

### 7.2 What the gate does

1. Loads `tests/eval/suites/general_holdout_live.json` (10 ideas, 5
   ship + 5 kill, **no `mock_precedents` and no `rag_context`** — forces
   real retrieval).
2. For each idea, calls
   `gecko_core.sources.v1_block.dispatch_and_render(...)` to build a real
   V1 Source Signal block (twit.sh + HN + Reddit; precedents skipped —
   the harness has no SessionStore).
3. Hands that block to the live AG2 debate as `rag_context`.
4. Records per-idea `v1_sources_cost_usd` in the run JSON (sum across the
   gate is printed at the end).
5. Reads `aggregate.verdict_accuracy`; passes iff **≥ 0.80**.

### 7.3 Spend + runtime

| Item | Estimate |
|---|---|
| twit.sh (mainnet, $0.05 cap × 10 ideas) | ~$0.50 |
| OpenAI agent tokens (10 ideas, gpt-4o-mini + gpt-4o judge) | ~$2.00 |
| Anthropic rubric (10 × Sonnet 4.6) | ~$1.00 |
| **Total** | **~$3.50** |
| Runtime, sequential | ~8-12 minutes |

### 7.4 Why 0.80 (not 0.85)?

Real V1-source signal is noisier than the curated fixtures the cutover
gate uses. The hand-tuned `rag_context` strings in `general_suite.json`
were authored *to* the v5.4 judge prompt; the live-V1 gate breaks that
loop. We accept 5 percentage points of accuracy loss as the cost of
measuring true performance.

### 7.5 On failure

The script exits 1 with the run-file path. Inspection:

```bash
jq '.ideas[] | select(.actual_verdict != .expected_verdict) | {id, expected_verdict, actual_verdict, v1_sources_cost_usd, scores}' \
  tests/eval/live_runs/<date>-holdout_live.json
```

If failures cluster on idea categories where twit.sh dominates (crypto /
defi / hackathon-team), the v5.4 prompts likely over-trust noisy X
signal — `software-engineer` (Track A) owns prompt re-tuning. If failures
cluster elsewhere, suspect a regression in the dispatcher or render
shape and re-run with the fixture-mode gate (§2-§5) to confirm prompts
are still good.

### 7.6 Cadence

Run after any of:

- Track A ships new prompt versions (`v5.5+`).
- Sources catalog changes (new V1 source, twit.sh API drift).
- Before mainnet cutover, in addition to the curated-rag gate.

Same-day re-runs append `-2`, `-3`, ... to
`tests/eval/live_runs/YYYY-MM-DD-holdout_live.json`.
