"""Repo-root conftest — path-based auto-marking for the pytest suite.

S24 W3 Task #10: classify every collected test into one of:

    unit         — default. No marker. Runs in <60s laptop sweep.
    slow         — >2s tests (eval scripts, panel runs, big fixtures).
    network      — outbound HTTP to OpenAI/Anthropic/Tavily/Pyth/etc.
    integration  — anything under tests/integration/.
    mongo        — hits a (real or stub) MongoDB instance.
    live         — hits a production-or-live external service.
    live_solana  — already declared (web3-engineer); kept here for completeness.

`pyproject.toml [tool.pytest.ini_options]` deselects `slow / network /
integration / mongo / live*` from the default `uv run pytest` invocation.
Operators flip them back on with explicit `-m` selectors or the Makefile
recipes (`make test-mongo`, `make test-live`, `make test-full`).

We auto-mark by PATH + FILENAME rather than annotating every file in-tree.
Rationale: 332+ tests across two test roots; in-file decorators bloat
diffs and require constant maintenance as tests move around. Path
heuristics are also reversible — flip a directory and the markers
update across the whole tree.

Tests that don't fit the heuristic cleanly can still add explicit
`@pytest.mark.slow` / `@pytest.mark.network` decorators; the auto-marker
is additive and never strips a marker that's already present.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# --- path → marker rules (evaluated in order; all matches apply) ---------

# Directories whose entire contents get a marker.
_DIR_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("tests/integration/", ("integration",)),
    ("tests/e2e/", ("slow", "integration")),
    ("tests/eval/scripts/", ("slow", "network")),
    ("tests/smoke/", ("slow",)),
]

# Filename-substring → marker rules. Matches the basename only so a file
# can live under any package and still be classified.
_FILENAME_RULES: list[tuple[str, tuple[str, ...]]] = [
    # Mongo-touching tests. `mongomock`-only tests are still marked
    # `mongo` so the operator can opt out when iterating fast.
    ("test_cache_mongo", ("mongo",)),
    ("test_verdict_persistence", ("mongo",)),
    ("test_transcripts_mongo", ("mongo",)),
    ("test_doctor_mongo", ("mongo",)),
    ("test_mongo_reads", ("mongo",)),
    ("test_mongo_chunks", ("mongo",)),
    ("test_chunk_store", ("mongo",)),
    ("test_s20_rag02_filterable_index", ("mongo",)),
    # Live-wire tests — already have `live_*` markers via decorator,
    # but we add `live` so `-m "not live"` picks them all up.
    ("test_paysh_live", ("live", "network")),
    ("test_bazaar_live", ("live", "network")),
    ("test_cdp_live_verify", ("live", "network")),
    ("test_live_buyer_path", ("live", "network")),
    ("test_live_x402_client", ("live", "network")),
    ("test_verdict_settle_contract", ("network",)),
    ("test_bazaar_consumer_contract", ("network",)),
    ("test_trade_oracle_smoke", ("slow", "network")),
    # Slow tests (panel runs, distribution critics with real LLM call paths,
    # parallel-debate end-to-end). Most use respx but still take >2s.
    ("test_parallel_debate_e2e", ("slow",)),
    ("test_verdict_render_e2e", ("slow",)),
    ("test_e2e_user_flow", ("slow",)),
    ("test_pulse_engine", ("slow",)),
    ("test_load_smoke", ("slow",)),
    ("test_pro_sse", ("slow",)),
    ("test_research_pro_retry", ("slow",)),
    ("test_pro_budget", ("slow",)),
    ("test_dispatch_wires_to_chunks", ("slow",)),
    ("test_calibration_corpus", ("slow",)),
    ("test_judges", ("slow",)),  # multiple synth runs
    # Embedder/voyage contract tests touch big-vector fixtures.
    ("test_voyage_embedder_contract", ("slow",)),
    ("test_voyage_rerank_batch", ("slow",)),
]


def _path_str(item: pytest.Item) -> str:
    """Return a forward-slash repo-relative path for `item`."""
    try:
        repo_root = Path(__file__).resolve().parent
        rel = Path(str(item.fspath)).resolve().relative_to(repo_root)
        return rel.as_posix()
    except (ValueError, OSError):
        return str(item.fspath).replace("\\", "/")


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Apply path-based markers to every collected test item."""
    for item in items:
        path = _path_str(item)
        basename = Path(path).stem

        applied: set[str] = set()
        for prefix, marks in _DIR_RULES:
            if prefix in path:
                applied.update(marks)
        for needle, marks in _FILENAME_RULES:
            if needle in basename:
                applied.update(marks)

        existing = {m.name for m in item.iter_markers()}
        for mark in applied - existing:
            item.add_marker(getattr(pytest.mark, mark))
