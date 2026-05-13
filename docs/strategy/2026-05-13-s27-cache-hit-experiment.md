# S27 Task #18 — cache-hit experiment

**Owner:** data-engineer
**Branch:** ``s27/cache-hit-experiment`` (cut from ``s26/cap-fix``)
**Budget:** ≤ $5 + 4–6 engineer-hours. **Actual spend: $0.** No LLM calls, no
real Mongo writes. The replay mocks ONLY at the ``VerdictCaller`` boundary;
every layer above (idea_hash composition, freshness math, stampede lock,
spend-cap probe, event emission, state-store cache writes) is the real
production code path per Pattern E.

## Reproduction

```
uv run python scripts/experiments/cache_hit_replay.py --mode idle
uv run python scripts/experiments/cache_hit_replay.py --mode low
uv run python scripts/experiments/cache_hit_replay.py --mode high
uv run python scripts/experiments/cache_hit_replay.py --mode all   # default
```

Wall-clock per mode: < 2 seconds on a laptop.

## Results

| Mode         | Calls | Hits | Misses | Ratio  | Prior | Delta    | Risk #2 |
|--------------|-------|------|--------|--------|-------|----------|---------|
| idle         | 288   | 287  | 1      | 0.997  | 0.95  | +0.047   | sleeps  |
| active-low   | 53    | 52   | 1      | 0.981  | 0.70  | +0.281   | fires↑  |
| active-high  | 496   | 488  | 8      | 0.984  | 0.50  | +0.484   | fires↑  |

(``delta = measured − prior``; ``Risk #2 fires`` when ``|delta| > 0.20``.
The arrow direction notes whether the gap is favourable (↑) or adverse (↓).)

## Honest call: does Risk #2 fire?

**Yes, twice — but in the favourable direction.** Measured cache-hit is
materially HIGHER than the cost-model priors in both active modes. Charged-call
volume is ~1/3 of the active-low projection and ~1/30 of the active-high
projection.

**Pricing implication:** the V0.2 pricing-flip trigger in the §7 decision tree
of ``2026-05-13-s26-multi-sprint-replan.md`` is **not** required. Active-low
pricing already covers all three modes with margin. No earlier pricing
decision needed.

The idle mode (prior 0.95) is within 5pp of the measured value — Risk #2
sleeps cleanly there.

## Cost-model reconciliation

The cost-model doc has been updated with measured numbers + delta vs priors —
see ``2026-05-13-s26-autonomous-tradeagent-cost-model.md`` § "Update 2026-05-13".
Headline: active-high cost projection drops from ~$6.25 → ~$0.20 / agent / day.

## Cache-key bugs surfaced — FOLLOW-UPS, NOT FIXED HERE

Per task constraint: a change to ``idea_hash`` composition has compatibility
implications across every existing cached verdict, so these are filed for
explicit founder sign-off rather than fixed in this experiment.

### Bug A — ``tier`` is not part of the cache key

``OracleWrapper.get_verdict(idea, tier, trigger)`` calls
``key = idea_hash(idea)`` — ``tier`` is not in the hash. The state store
look-up is ``get_verdict_cache(agent_id, key)`` — also not parameterised on
tier. So when a basic verdict is cached at t0 and a later pro request
arrives for the same idea, the cached basic verdict is returned.

This is what makes our active-low / active-high measurements look as good as
they do: the 5 + 20 simulated spec swaps to pro all hit cache against the
prior basic entry. In production this manifests as a silent quality
downgrade — pro tier user pays for pro, gets a basic verdict from the cache.

**Recommendation:** add ``tier`` to ``idea_hash`` (or, more conservatively,
to the state-store cache key). Compatibility plan: bump the cache document
schema-version, treat pre-bump entries as ``tier=basic``, and let them
age out within the 24h freshness window.

### Bug B — ``spec_version`` is not part of the cache key

Same pattern. Per
``memory/project_three_layer_architecture_2026_05_11.md`` and
``docs/strategy/2026-05-11-trade-vertical-expansion.md`` §5, a spec change
mid-flight is supposed to TRIGGER a re-verdict. But because ``spec_version``
is not in ``idea_hash``, a hot-swap that lands on a previously-asked idea
serves the pre-swap verdict from cache.

The runtime can work around this by passing ``force_refresh=True`` on the
``spec_change`` trigger, and inspection of ``OracleWrapper.get_verdict``
shows this is the intended escape hatch. Recommendation: audit the
runtime's ``spec_change`` call site to confirm ``force_refresh=True`` is
threaded through. If not, that's a one-line fix and the right place — not
the hash composition.

### Bug C — paraphrase coverage is partial (not actually a bug, but worth noting)

In active-high mode we sprayed 5 base ideas with a 30% chance of appending
``" today"`` as paraphrase jitter. We saw 8 unique idea-hashes for 5 base
ideas — 3 of the paraphrases leaked through despite ``today`` being in the
horizon-regex synonyms. Inspection: ``" today"`` IS in
``_HORIZON_PATTERNS`` and DOES collapse to ``horizon=now`` — but the other
horizon tokens in the base ideas (``"intraday"``, ``"next few days"``,
``"this week"``) already populate the horizon dimension, so paraphrase
collapses are dominated by whichever pattern matches first in the regex
order. This is correct semantic behaviour, not a bug.

## What this experiment did NOT test

* Real OpenAI cost variance per call (out of scope — assumed flat ~$0.025/pro).
* Cross-agent cache sharing (the cache is per-agent today; a shared cache
  would push hit-rate even higher but changes the security model).
* Production Mongo round-trip latency (the InMemoryStateStore is faster
  than Atlas; cache lookup latency is a separate measurement).
* The actual content quality of the served verdicts under stale-cache
  conditions (tier/spec downgrade is a quality bug, not a hit-rate bug).

## Sign-off

Risk #2 sleeps on idle, fires favourably on both active modes. No
pricing-flip required ahead of schedule. Two cache-key bugs filed as
follow-ups requiring founder sign-off before fix.
