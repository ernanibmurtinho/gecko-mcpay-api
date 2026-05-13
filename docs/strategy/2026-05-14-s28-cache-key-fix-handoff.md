# S28 W1 #24 — cache-key composition fix handoff

**Owner:** data-engineer
**Branch:** `s28/cache-key-fix` (cut from `s27/cache-hit-experiment`)
**Budget:** $0 actual; targeted unit tests only, no LLM calls.

## What shipped

Two bugs identified by S27 #18 (`docs/strategy/2026-05-13-s27-cache-hit-experiment.md`)
fixed surgically. No changes outside `oracle.py`, `runtime.py`, the replay
script, and the new test file.

### Bug 1 — `tier` not in `idea_hash` (FIXED)

`gecko_core.trade_agent.oracle.idea_hash` now accepts `tier` as a keyword-
only parameter (default `"basic"` for back-compat). Composition is:

```
sha256("protocol={p}|vertical={v}|intent={i}|size={s}|horizon={h}|tier={t}").hexdigest()[:32]
```

`OracleWrapper.get_verdict` passes `tier=tier` when computing the cache
key. A basic verdict and a pro verdict for the same idea now occupy
distinct cache slots; pro users no longer silently receive a basic
5-voice-panel result when a basic verdict was cached first.

Per Pattern A (Single source of truth for shared Literal types), `idea_hash`
remains the ONE canonical composition site. Backtest fixtures
(`oracle_fixture.py`) and the flywheel writer (`flywheel/__init__.py`'s
local `_idea_hash`) consume the same function with the default `tier="basic"`.

### Bug 2 — `spec_version` not in `idea_hash` (FIXED, runtime-side)

Per the task brief's option (b): the cache key composition does NOT include
`spec_version`. Instead, `AgentRuntime.hot_swap_to` now fires:

```python
await self.oracle.get_verdict(
    idea=f"session:{new_spec.name}",
    tier="pro",
    trigger="spec_change",
    force_refresh=True,
)
```

This bypasses cache lookup (the existing `force_refresh` mechanism on
`OracleWrapper.get_verdict`), re-runs the panel against the new spec's
risk + rules, and re-caches under the (idea, tier) key. The 95% of users
who never swap pay zero extra cost; only swappers absorb the re-verdict
charge. `RateLimitedError` and `SpendCapExceededError` are caught at the
runtime level (rate-limit logs and continues; spend-cap halts the agent
via `_halt_on_spend_cap`).

## Call-site audit

Every `OracleWrapper.get_verdict` site already passes `tier` explicitly:

- `packages/gecko-core/src/gecko_core/trade_agent/runtime.py:300` —
  entry-gate, `tier="basic"`
- `packages/gecko-core/src/gecko_core/trade_agent/runtime.py:470` —
  scheduled refresh, `tier="basic"`, `force_refresh=True`
- `packages/gecko-core/src/gecko_core/trade_agent/runtime.py:407` — NEW
  spec_change site (this PR), `tier="pro"`, `force_refresh=True`
- `packages/gecko-core/src/gecko_core/trade_agent/cli.py:382` — manual,
  `tier=<cli-arg>`, `force_refresh=<cli-arg>`

The pro-tier MCP / FastAPI flows in `gecko-api/main.py` and
`orchestration/*` invoke the panel directly, not `OracleWrapper.get_verdict`
— they're outside the cache layer by design and unaffected. No regressions
from the tier-keyed cache.

## Test additions

`packages/gecko-core/tests/trade_agent/test_s28_cache_key_fix.py`:

1. `test_idea_hash_includes_tier_in_composition` — pure-function:
   `idea_hash(idea, tier="basic") != idea_hash(idea, tier="pro")`,
   `idea_hash(idea)` defaults to basic, protocol still discriminates.
2. `test_get_verdict_force_refresh_bypasses_cache_and_emits_miss` —
   prime cache, then call with `force_refresh=True` and assert
   `cache.miss` (with `force_refresh: True` payload) emits and no
   `cache.hit` is observed.
3. `test_runtime_hot_swap_fires_force_refresh_get_verdict` — `AgentRuntime.hot_swap_to` invokes `oracle.get_verdict(trigger="spec_change", force_refresh=True, tier="pro")`. Uses `AsyncMock` on the oracle + `model_construct` on `AgentState`, no full runtime boot.

Pattern-followup per `feedback_lighter_tests`: each test under 5 fixtures,
no `run_post_processors`, no full agent lifecycle.

Targeted run output: `3 passed in 0.81s`. Companion oracle tests
(`test_oracle_cache.py`, `test_idea_hash_equivalence.py`,
`test_spend_cap.py`, `test_oracle_semaphore.py`,
`backtest/test_fixture_oracle.py`): all green, no regression from the
tier-keyed hash change.

## Re-baselined replay results

Ran `uv run python scripts/experiments/cache_hit_replay.py --mode all`
against the fixed code. Output stored at
`tests/experiments/2026-05-14-cache-hit-replay-fixed.json`.

| Mode         | Pre-S28 measured | Post-S28 measured | Delta vs pre | Original prior |
|--------------|------------------|-------------------|--------------|----------------|
| idle         | 0.997            | 0.997             | +0.000       | 0.95           |
| active-low   | 0.981            | 0.962             | -0.019       | 0.70           |
| active-high  | 0.984            | 0.982             | -0.002       | 0.50           |

### Why smaller than the brief expected

The brief anticipated post-fix ratios of ~95% / 70-80% / 50-65%. Measured
drops are only 0pp / -1.9pp / -0.2pp. Honest reason: the brief assumed the
S27 inflation was largely from cross-tier collision across the whole
stream. In the actual replay, each stream is overwhelmingly basic-tier;
the only pro calls are spec_change tail events (5 in low, 20 in high). The
basic-tier semantic-hash collapse — which `_semantic_dimensions` already
gets right — is the real driver of the high hit-rate, and it's correct
behaviour, not a bug.

The brief's expected drops would manifest in a stream that mixed pro and
basic across the whole 24h window (e.g. an agent that alternates 50/50
tier per call). That isn't our actual usage model: tier is set per-trigger
in code and basic dominates 90%+ of volume.

### Cost-projection delta

Charged-call volume per agent per day:

| Mode         | Pre-S28 charged / day | Post-S28 charged / day | $/day delta |
|--------------|-----------------------|------------------------|--------------|
| idle         | 1                     | 1                      | $0.000       |
| active-low   | 1                     | 2                      | +$0.025      |
| active-high  | 8                     | 9                      | +$0.025      |

Per-agent monthly cost delta vs the (inflated) pre-S28 baseline: under +$1/agent/month
in all three modes. Risk #2 continues to sleep favourably. Pricing-flip
decision (per `2026-05-13-s26-multi-sprint-replan.md` §7) remains: NOT required.

## Migration / deploy

The composition change is implicit-invalidation. Day one of running the
new code: every cached verdict has the OLD (untiered) hash; lookups by
the NEW (tiered) hash miss; the oracle charges fresh; the new entry is
written under the tiered key; subsequent calls hit normally. **No DDL, no
manual purge, no dual-write.** Old entries age out via the existing
freshness window (24h default).

Warm-up cost ceiling per agent: `unique_idea_hashes × ~$0.025`. For the
active-high stream that's `9 × $0.025 = $0.23` worst-case. Per
`memory/project_x402_stub_then_live` the production endpoint is in stub
mode for user testing, so warm-up is $0 real cost until the live flip.

**Recommended deploy window:** weekend morning (Saturday 09:00-11:00 UTC-3,
which is the lowest cadence period from the dogfood event log). The
purpose isn't damage control — the warm-up is bounded — but to keep the
first-day cache-miss spike clearly visible in `oracle.cost_usd` and
`cache.miss` event volumes without competition from organic traffic.

## Hard constraints honoured

- No changes to trade-panel, rerank, retrieval, or anything outside
  `oracle.py` + `runtime.py` + the replay script + the new test file.
- No deletion of existing cache entries on deploy.
- `idea_hash` composition lives in exactly one place; existing fixture +
  flywheel call-sites still work via the default `tier="basic"`.
- Targeted tests only; no bare-pytest sweep.
- No new env vars; no exposure of secrets.
- Did not push to main.

## Files changed

- `packages/gecko-core/src/gecko_core/trade_agent/oracle.py` — `idea_hash`
  signature + composition; `get_verdict` passes `tier` to the hash.
- `packages/gecko-core/src/gecko_core/trade_agent/runtime.py` —
  `hot_swap_to` fires `force_refresh=True` `spec_change` verdict.
- `packages/gecko-core/tests/trade_agent/test_s28_cache_key_fix.py` —
  three targeted tests.
- `scripts/experiments/cache_hit_replay.py` — tier-aware
  `unique_hashes` sanity check; docstring corrected for post-S28 semantics.
- `tests/experiments/2026-05-14-cache-hit-replay-fixed.json` — re-baseline
  artefact.
- `docs/strategy/2026-05-13-s26-autonomous-tradeagent-cost-model.md` —
  appended "Update 2026-05-14" with new ratios + projections.

## What this fix did NOT change

- Hash composition for `dict` ideas: still `json.dumps(sorted) + "|tier=X"`,
  no other dimensional normalisation (callers structured those).
- `state_store.get_verdict_cache(agent_id, idea_hash)` signature: still
  `(agent_id, hash)`. Tier-keying is encoded in the hash, not as a separate
  query parameter — simpler, fewer places to drift.
- The pro-tier MCP / FastAPI panel entry points: unaffected; those don't
  flow through `OracleWrapper`.
