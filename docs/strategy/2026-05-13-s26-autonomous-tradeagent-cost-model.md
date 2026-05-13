# Autonomous trade-agent cost model — S26 baseline

**Status:** baseline reference. Cache-hit priors are pre-S27 assumptions; the measured
numbers from S27 Task #18 are appended at the bottom under
"Update 2026-05-13 — measured cache-hit rates".

## Priors (assumed, pre-measurement)

Monthly oracle burn per agent depends on call volume and cache-hit ratio.

| Mode         | Oracle calls / 24h | Assumed cache-hit | Implied charged calls / 24h |
|--------------|--------------------|--------------------|------------------------------|
| idle         | 288                | 0.95               | ~14                          |
| active-low   | 50                 | 0.70               | ~15                          |
| active-high  | 500                | 0.50               | ~250                         |

Charged-call cost is dominated by the pro-tier panel (~$0.025 / call). Idle and active-low
project to ~$0.35–$0.40 / agent / day, ~$10–12 / month. active-high projects to
~$6.25 / agent / day, ~$190 / month — the only mode where unit economics could break.

## Risk #2 (cost-model accuracy)

The three priors above were never measured. If true cache-hit in any mode is
materially lower (>20% gap), V0.2 trader-mode unit economics break and pricing
decisions move earlier per the §7 decision tree.

S27 Task #18 measures the priors against the real ``OracleWrapper`` cache-then-
charge path. See ``2026-05-13-s27-cache-hit-experiment.md``.

---

## Update 2026-05-13 — measured cache-hit rates

S27 Task #18 measured cache-hit using ``scripts/experiments/cache_hit_replay.py``
against the real ``OracleWrapper`` (in-memory state store + stub caller; same code
path as production minus the LLM and Mongo I/O).

| Mode         | Prior  | Measured | Delta    | Risk #2 |
|--------------|--------|----------|----------|---------|
| idle         | 0.95   | 0.997    | +0.047   | sleeps  |
| active-low   | 0.70   | 0.981    | +0.281   | fires↑  |
| active-high  | 0.50   | 0.984    | +0.484   | fires↑  |

Risk #2 fires in active-low and active-high — but **in the favourable direction**.
True cache-hit is materially higher than projected; charged-call volume is roughly
1/3 of the active-low projection and 1/30 of the active-high projection.

**Pricing implication:** active-high cost projection drops from ~$6.25 / agent / day
to ~$0.20 / agent / day. The §7 pricing-flip trigger (V0.2 priced to cover
active-high) is **not** required; the active-low pricing band already covers all
three modes with room.

**Cache-key bugs surfaced** (flagged as follow-ups, NOT fixed in this experiment
per task constraints):

* ``tier`` is NOT part of ``idea_hash`` — a basic verdict cached at t0 serves a
  later pro request without re-fetching. This *inflates* measured cache-hit and
  silently downgrades pro-tier quality. Filed as follow-up.
* ``spec_version`` is NOT part of ``idea_hash`` — spec hot-swaps (per
  ``project_three_layer_architecture_2026_05_11``) hit cache and serve the
  pre-swap verdict. Filed as follow-up.

Both bugs have compatibility implications (existing cached verdicts would all
invalidate on a fix) and need explicit founder sign-off before changing the hash
composition.

---

## Update 2026-05-14 — re-baseline after S28 cache-key fix

The two follow-up bugs from S27 #18 were fixed on `s28/cache-key-fix`:

- **Bug 1 (tier):** `idea_hash(idea, *, tier="basic")` now composes
  `sha256(protocol|vertical|intent|size|horizon|tier)`. Same idea + different
  tier => different key.
- **Bug 2 (spec_version):** runtime-side. `AgentRuntime.hot_swap_to` now fires
  `oracle.get_verdict(trigger="spec_change", force_refresh=True)`. The cache
  key composition is unchanged — only swapping users pay re-verdict cost.

Re-ran `scripts/experiments/cache_hit_replay.py --mode all` against the fixed
code. Results stored at `tests/experiments/2026-05-14-cache-hit-replay-fixed.json`.

| Mode         | Pre-S28 measured | Post-S28 measured | Delta vs pre | Original prior |
|--------------|------------------|-------------------|--------------|----------------|
| idle         | 0.997            | 0.997             | +0.000       | 0.95           |
| active-low   | 0.981            | 0.962             | -0.019       | 0.70           |
| active-high  | 0.984            | 0.982             | -0.002       | 0.50           |

The drops are smaller than the S28 brief anticipated. Why: the brief assumed
the pre-fix inflation was driven by mixed-tier collision across the whole
stream. In practice the basic-tier semantic-hash collapse was already
correct, and the only pro-tier calls in each stream are the spec_change
tail (5 in low, 20 in high). Once those are correctly missing against the
basic cache, the headline ratio barely moves because base-idea spray
dominates volume.

### Re-baselined cost projection (per agent per day)

Charged-call cost at ~$0.025/call (pro-tier panel):

| Mode         | Charged calls / 24h | Cost / day | Cost / month (30d) |
|--------------|---------------------|------------|--------------------|
| idle         | 1                   | $0.025     | $0.75              |
| active-low   | 2                   | $0.050     | $1.50              |
| active-high  | 9                   | $0.225     | $6.75              |

Compare to pre-S28 projection (which was already favourable):
active-high dropped from a projected $0.20/day (pre-S28 measurement) to
$0.225/day (post-S28). Effectively unchanged at the headline level.
**No pricing-band change required.** Risk #2 continues to sleep favourably.

### Migration note

Bug 1's fix changes `idea_hash` composition. Every existing cached verdict
invalidates implicitly on first deploy: lookup of the old key returns None,
the oracle charges fresh, the new (tier-keyed) entry is cached, and
subsequent calls hit normally. No DDL, no data migration, no manual cache
purge. Warm-up cost on day one is bounded by
`unique_idea_hashes × ~$0.025` per agent — for the active-high replay
that's 9 × $0.025 = $0.23 worst-case. Per `project_x402_stub_then_live`
the production endpoint is currently in stub mode for user testing, so the
warm-up has $0 real cost until the live flip. Recommend deploying during a
low-traffic window (weekend morning) regardless, to keep the first-day
cache-miss spike observable in `oracle.cost_usd` events without contention.
