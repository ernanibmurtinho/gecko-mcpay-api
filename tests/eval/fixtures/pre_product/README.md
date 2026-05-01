# pre_product fixtures

The research-tier suites the V1 eval gate runs against. JSON lives under
`tests/eval/suites/` (legacy path); this directory is the phase-aware
alias the loader resolves to.

If you're adding a NEW pre-product fixture, drop it under
`tests/eval/suites/` for parity with the existing suites. If/when we
collapse the two paths, that'll be a bulk move + a single SUITES_DIR
rename.
