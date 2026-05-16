# S32-#58 — `Vertical` Literal expansion for DeFi-trade fixtures

**Branch:** `s32/vertical-literal-expansion` (worktree-isolated)
**Date:** 2026-05-14
**Cost:** $0 (Literal + JSON edits + drift test verification)

## Why

The 11-cell `Vertical` Literal in `gecko_core.knowledge.taxonomy` was seeded
for consumer-app shapes (`neobank`, `marketplace`, `prediction_market`,
…). When #54 audited the trade-rubric suite, four DeFi fixtures had no
correct cell and fell back to `unknown`. Per Pattern E, `unknown` silently
filters out canon + protocol chunks at retrieval time, depressing
`pkCov` / `citRel` in the rubric harness.

The motivating result that drove this ticket:
`tests/eval/live_runs/2026-05-14-s24-defi-rubric-s32-post-vertical-fix-9.json`
showed `unknown`-tagged fixtures lagging the cohort on both metrics.

## What

Per Pattern A — one canonical Literal edit, every consumer reads from
`gecko_core.knowledge.taxonomy`:

### Literal additions (`taxonomy.py`)

| Value | DeFi primitive | Example protocols |
|---|---|---|
| `lending` | Collateralized debt positions (CDPs); borrow against collateral | Kamino Multiply, Marginfi, Save, Solend |
| `liquid_staking` | LST routing + native staking | Sanctum, Marinade, Jito stake |
| `perps` | Perpetual futures (funding-rate, no expiry) | Drift, Hyperliquid |

`yield_aggregator` was deliberately NOT added — no fixture in the current
suite needs it. Per the spec: over-expansion is Pattern A drift in
reverse. Add it the day a fixture actually demands it.

### Fixture re-tags (`defi_trade_rubric_suite.json` + `defi_trade_suite.json`)

| Fixture id | Before | After | Reasoning |
|---|---|---|---|
| `kamino-2024Q4-sol-leverage-entry` | `unknown` | `lending` | Kamino Multiply opens a collateralized SOL-borrow position. That is a lending-protocol primitive, not a DEX swap. |
| `kamino-2025Q3-usdc-pyusd-exit` | `dex` | `dex` (verified, unchanged) | AMM-style LP vault on Kamino — DEX-LP cell, no borrow leg. Spec said "probably already `dex`, verify"; confirmed. |
| `drift-2024Q4-sol-perp-long` | `dex` (placeholder from #54) | `perps` | SOL-PERP is a perpetual future, not a spot swap. |
| `drift-2025Q2-jto-perp-short` | `dex` (placeholder from #54) | `perps` | JTO-PERP perpetual short. |
| `jito-2025Q2-mev-tip-band` | `infra_devtool` | `infra_devtool` (unchanged) | MEV tip-band tuning is infra-developer-tool work, not a trading-cell primitive. Spec said "stays — infra is correct for MEV"; confirmed. |
| `sanctum-2025Q3-unstake-vs-hold` | `unknown` | `liquid_staking` | Sanctum INF is an LST-router product. |

The `kamino-2024Q3-jlpusdc-entry` and `kamino-2025Q1-jitosol-vault`
fixtures kept their existing `dex` tag — they are LP vault deposits
without a borrow leg, closer to a DEX-LP than to `lending`. If a future
audit decides Kamino LP vaults belong in a new `yield_aggregator` cell,
that's a follow-up Literal edit, not part of this ticket.

Each re-tagged fixture carries a `notes` field documenting the reason, so
the drift test failure mode is loud and the diff is self-explanatory.

## Drift-test verification

Three drift tests exercised (worktree-local):

- `packages/gecko-core/tests/knowledge/test_taxonomy_consistency.py` —
  updated `test_canonical_verticals_value` to assert the 14-tuple +
  renamed `test_all_29_…` → `test_all_32_…`. Both pass.
- `packages/gecko-core/tests/test_provider_kind_consistency.py` — passes
  unchanged (uses `typing.get_args` and doesn't care about `Vertical`).
- `tests/test_fixture_vertical_drift.py` — copied in from the #54
  worktree; passes because it reads `typing.get_args(Vertical)` (so the
  three new values auto-validate the re-tagged fixtures).

Final run:

```
uv run --python 3.12 --all-packages pytest \
  packages/gecko-core/tests/knowledge/test_taxonomy_consistency.py \
  tests/test_fixture_vertical_drift.py \
  packages/gecko-core/tests/test_provider_kind_consistency.py
  → 24 passed in 0.54s
```

## Pattern E probe — deferred

The spec asked for an end-to-end probe confirming a `lending` fixture now
retrieves canon_marks + kamino chunks vs the prior `unknown`-filtered
state. This requires the trade-panel retrieval path with live (or seeded)
Mongo chunk data, which is out of scope for the worktree-isolated $0
edit. **Recommendation:** the rubric N=10 re-run on the post-merge
branch IS the Pattern E probe — if `lending` / `perps` / `liquid_staking`
fixtures now show non-zero `canon_marks` + protocol citations in the
verdict envelope, the wiring is verified. Cost ≈ $1.50.

## Coordination with #54

The drift test `tests/test_fixture_vertical_drift.py` and the two
fixture-suite files originated in the parallel `agent-a530bb6e729064a76`
worktree. This ticket copies them into the `s32/...` worktree and applies
the retags on top. When #54 merges first, the apply order is:

1. Land #54 — adds the drift test and the rubric suite with #54's
   placeholder tags (`unknown` / `dex`).
2. Land #58 (this ticket) — Literal expansion + fixture retags.

If #58 lands first, #54's drift test passes against the expanded Literal
automatically (it reads `get_args(Vertical)`).

## Recommendation

Re-run the rubric suite N=10 against the post-#58 branch
(`bb research --rubric defi_trade_rubric --n 10`, ≈$1.50) to confirm:

- `kamino-2024Q4-sol-leverage-entry` now retrieves canon_marks /
  canon_damodaran / paysh_live chunks on the `lending` cell
- `sanctum-2025Q3-unstake-vs-hold` now retrieves canon + protocol chunks
  on the `liquid_staking` cell
- `drift-2024Q4-sol-perp-long` and `drift-2025Q2-jto-perp-short` retrieve
  canon_macro / canon_marks chunks on the `perps` cell

If `pkCov` / `citRel` lift on those four fixtures, the wedge is verified
empirically. If they don't, the bug is downstream of the taxonomy (likely
the retrieval `$match` doesn't dispatch on `vertical` at all, or the
classifier doesn't propagate the new values — both are separate
follow-ups).
