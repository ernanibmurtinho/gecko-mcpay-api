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
