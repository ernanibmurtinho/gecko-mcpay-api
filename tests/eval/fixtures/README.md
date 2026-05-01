# Phase-aware eval fixtures

S13-PHASE-03. The eval gate now dispatches fixtures by `SessionPhase` so
S14 (`gecko_pulse`) and S15 (ongoing) can land their suites without a
loader refactor.

```
tests/eval/fixtures/
  pre_product/    — research-tier suites (general, crypto, saas, holdout, …)
  during_build/   — pulse-tier suites (S14)
  ongoing/        — operational suites (S15+)
```

## Where the actual JSON lives today

For `pre_product`, the JSON files still sit under `tests/eval/suites/`
(unchanged). The phase loader (`tests/eval/runner.py::_phase_dir`) treats
`pre_product` as an alias for that directory so existing CI commands keep
working byte-for-byte.

`during_build/` and `ongoing/` are stubs: empty directories with a README
each. The first real fixture in either lives in S14 / S15.

## Rubric v2

The rubric v2 row schema (`IdeaResult` in `runner.py`) carries an
optional `phase` field defaulting to `pre_product`. Live runs from
non-`pre_product` suites populate it on the way through `_evaluate_one`
so downstream gate diffs can slice by phase.
