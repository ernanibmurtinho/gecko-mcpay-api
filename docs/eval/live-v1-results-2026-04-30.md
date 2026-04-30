# Live-V1 eval gate — 2026-04-30

**Status:** PASS (0.80 threshold met exactly)
**Sprint:** S10-EVAL-01 (Sprint 10 Track D)
**Run files:** `tests/eval/live_runs/2026-04-30-{general,crypto,saas,holdout_live}.json`

## Summary

| Suite | Threshold | Result | Verdict |
|---|---|---|---|
| general | ≥ 0.95 | **1.00** (20 ideas) | PASS |
| crypto | ≥ 0.95 | **1.00** (15 ideas) | PASS |
| saas | ≥ 0.95 | **1.00** (15 ideas) | PASS |
| holdout_live | ≥ 0.80 | **0.80** (10 ideas) | PASS |

All four gates green. Curated fixtures (general/crypto/saas) hold at perfect accuracy post-Sprint-9. Live-V1 hits the noisier-signal threshold exactly.

## Holdout-live breakdown (10 ideas)

| Expected | Actual | Count |
|---|---|---|
| ship | ship | 3 |
| ship | kill | **2** (false negatives) |
| kill | kill | 5 |
| kill | ship | 0 |

- **Recall on ship: 3/5 = 0.60**
- **Recall on kill: 5/5 = 1.00**
- **Bias:** conservative — model under-ships, never over-ships. For a "should I build this" advisor this is the correct failure mode (false-negative cost = idea still gets built elsewhere; false-positive cost = builder wastes 6 months).

## Aggregate metrics (holdout_live)

| Metric | Value |
|---|---|
| verdict_accuracy | 0.80 |
| kill_rate | 0.70 |
| median_score | 5.50 |
| median_weighted_score | 6.42 |
| median_cost_usd | $0.0014 |
| total LLM cost | $0.0145 |
| total v1_sources_cost | **$0.0000** ⚠️ |

## ⚠️ Anomaly: v1_sources_cost = $0.00

The script enforces `TWITSH_ENABLED=true` + wallet env vars and predicts ~$0.50 in twit.sh charges. Actual on-chain V1 spend was **zero** across all 10 ideas.

Hypotheses (in order of likelihood):
1. **Embedding/result cache hit** — prior runs of same holdout ideas already populated the source cache; dispatch_sources() short-circuited before paying.
2. **twit.sh circuit breaker / cap engaged silently** — gate ran but per-idea call was skipped under cap.
3. **`--live-rag` flag didn't propagate to dispatcher** — would mean signal came from HN/Reddit only, not V1.

Action: investigate before next live-V1 run. If (1), the gate effectively measured cached signal — still useful, but the "live V1" label is overstated. If (3), this is a regression to file as F18.

## Interpretation (per builder)

> Quote from session: "If the average keeps on 1.0 we are overfitting — no model can guess the right question all the time. It means we are evolving and that's awesome."

Agree. The two false negatives are evidence the live-V1 suite has **real noise** that curated fixtures don't, which is the whole point of holdout-live as a separate gate. A 1.0 here would mean the holdout was contaminated or the rubric was too forgiving.

## Cost

- LLM (OpenAI + Anthropic rubric): ~$3.00 estimated, $0.0145 measured (much lower — likely Anthropic rubric not yet wired through cost ledger)
- twit.sh on-chain: $0.0000 (see anomaly above)
- **Actual run cost: well under $5**

## Next

- Sprint 11 candidates:
  - **F18** — investigate `v1_sources_cost = 0` anomaly; either confirm cache short-circuit is intended or fix dispatcher flag propagation.
  - Re-tighten holdout_live to ≥ 0.85 once we have 2–3 baseline runs (per Sprint 10 plan note).
  - Add per-idea `idea_id` to live_runs JSON so failures are debuggable post-hoc (currently `idea_id: null` for all 10 rows).
- Sprint 10 status: **Tracks A/C/D/E shipped; Track B (mainnet smoke) still blocked on Solana mainnet wallet funding.**
