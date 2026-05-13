# Gecko — pytest convenience recipes.
#
# S24 W3 Task #10: the default `uv run pytest` invocation deselects
# slow / network / integration / mongo / live markers via pyproject's
# addopts. Operators reach for the slices below to opt into heavier
# runs. CI uses `make test-full` so we never ship un-tested code.
#
# Marker semantics live in conftest.py at the repo root. Tests are
# auto-marked by path + filename so individual files do not need
# decorator churn.

.PHONY: test-fast test-full test-changed test-mongo test-live test-network test-integration test-canary

# S26 Tier-1 eval canary. Live panel call, ~$0.05/run, <30s. Asserts
# D1 (answered_back) + D2 (context_overflow) + D3 (citations_grounded)
# on the single-fixture canary suite. Exits non-zero on any failure.
# Plan doc: docs/strategy/2026-05-13-s26-eval-redesign-plan.md
# Use `make test-canary CANARY_ARGS=--dry-run` to scaffold-test without spend.
test-canary:
	uv run python -m tests.eval.scripts.canary_eval $(CANARY_ARGS)

# Default laptop checkpoint. <60s. Skips slow / network / integration /
# mongo / live markers. Run this after every change.
test-fast:
	uv run pytest -m "not slow and not network and not integration and not mongo and not live and not live_solana and not live_cdp and not live_bazaar and not live_paysh and not live_x402_verdict and not e2e_smoke"

# Pre-merge / CI gate. Runs every collected test, including the
# expensive slices. Expect 5-10 minutes locally; longer in CI.
test-full:
	uv run pytest -m ""

# Only the tests for files changed since main. Heuristic: take the
# diff list, map *.py → test file candidates, dedupe, hand to pytest.
# Falls back to `pytest --picked` if the plugin is installed.
test-changed:
	@if uv run python -c "import pytest_picked" 2>/dev/null; then \
		uv run pytest --picked; \
	else \
		changed=$$(git diff --name-only main...HEAD | grep -E '\.py$$' || true); \
		tests=$$(echo "$$changed" | grep -E '^(tests|packages/.*/tests)/' || true); \
		if [ -z "$$tests" ]; then \
			echo "no test files changed since main; running fast suite"; \
			$(MAKE) test-fast; \
		else \
			echo "$$tests" | xargs uv run pytest; \
		fi; \
	fi

# Schema / store changes. Mongo-marked tests only.
test-mongo:
	uv run pytest -m mongo

# Production smoke. Hits live external services — costs money on x402
# providers. Operator opt-in only.
test-live:
	uv run pytest -m "live or live_solana or live_cdp or live_bazaar or live_paysh or live_x402_verdict or e2e_smoke"

# Outbound-HTTP tests (recorded cassettes mostly; still useful as a
# pre-deploy sanity).
test-network:
	uv run pytest -m network

# Full-stack integration suite under tests/integration/.
test-integration:
	uv run pytest -m integration
