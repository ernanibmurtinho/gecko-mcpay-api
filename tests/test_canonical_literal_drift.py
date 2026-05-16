"""S31-#55 — Single CI-time guard for canonical-Literal drift.

This test catches the family of bugs that surfaced this session, all
sharing the same root cause: code-side ``Literal`` definitions drift
from sibling artifacts (eval fixtures, retrieval admit-lists, env-var
names in ad-hoc scripts).

Four dimensions covered, one test function each. Each test is its own
unit so a single drift surface fails loudly without masking the others.

Dimension 1 — Fixture ``vertical`` drift
    Every ``tests/eval/suites/*.json`` fixture that carries a
    ``vertical`` field must use a value from
    ``gecko_core.knowledge.taxonomy.Vertical``. Caught the S31-#54
    ``"infra"`` slip: the value isn't in the Literal, so retrieval
    silently can't match and the citation falls on the floor.

Dimension 2 — Fixture ``must_cite_provider_kinds`` drift
    Every value in the fixture's ``must_cite_provider_kinds`` list
    must be in ``gecko_core.sources.types.ProviderKind``. Caught the
    S31 ``"market_data"`` slip in ``defi_trade_rubric_suite.json`` —
    a provider that doesn't exist in the canonical Literal can never
    be cited, so the gate is structurally unsatisfiable.

Dimension 3 — ``$vectorSearch`` admit-list drift
    If the trade-panel retrieval pipeline filters chunks on
    ``provider_kind`` via a ``$in`` admit-list, that admit-list must
    be a superset of ``PROVIDER_KINDS``. Caught the S31-#43 bug where
    ``bazaar_manifest`` was in Mongo but missing from the admit-list,
    silently filtering it out. If no admit-list exists (current state
    — the pipeline filters on ``vertical`` only and trusts post-filter
    ``$match`` for protocol cross-cutting), the test passes; the
    instant one is added, this test enforces completeness.

Dimension 4 — MongoDB env-var alias drift
    All ``scripts/`` Python files must look up MongoDB connection
    config via the canonical env-var name set —
    ``CANONICAL_MONGO_ENV_VARS`` below. Aliases like
    ``MONGODB_DB_NAME`` (#38) or ``MONGO_URI`` fall-throughs
    (#42 / #48) are flagged with a clear file-line message.

Light-test discipline:
    * Each dimension is one test function (4 tests total).
    * Each test uses at most ~5 fixture inputs (we parametrize over
      fixture files / scripts, not over every literal value).
    * No mocks, no fixtures-files, no network. Pure AST + JSON parsing.
    * Should complete in < 5s. Runs in ``make test-fast``.

Extending for a new shared Literal:
    Replicate the dimension-1 / dimension-2 pattern: import the new
    canonical tuple at module top, add a ``test_*`` function that
    walks the artifact it should match, format an explicit
    ``"X is in <artifact> but not in <Literal>"`` message on failure.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from gecko_core.knowledge.taxonomy import VERTICALS
from gecko_core.sources.types import PROVIDER_KINDS

# Worktree-safe repo root: this file lives at <repo>/tests/.
_REPO_ROOT = Path(__file__).resolve().parents[1]
_SUITES_DIR = _REPO_ROOT / "tests" / "eval" / "suites"
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
_TRADE_PANEL = (
    _REPO_ROOT
    / "packages"
    / "gecko-core"
    / "src"
    / "gecko_core"
    / "orchestration"
    / "trade_panel"
    / "__init__.py"
)


# ---------------------------------------------------------------------------
# Canonical env-var name set — Dimension 4.
# Derived from the existing in-tree usage in scripts/mongo/ + scripts/.
# Anything outside this set in scripts/ is treated as an alias and flagged.
# ---------------------------------------------------------------------------
CANONICAL_MONGO_ENV_VARS: frozenset[str] = frozenset(
    {
        # Connection string. Single canonical name.
        "MONGODB_URI",
        # Vector-store database. Single canonical name.
        "MONGODB_CHUNK_DB",
        # Trade-agent runtime database (separate from the chunk store
        # by design — see project_mongo_cutover_no_backfill.md).
        "MONGODB_TRADE_DB",
    }
)

# Known aliases / drift values that have appeared in the codebase and
# must be explicitly flagged. The test message names each one so the
# operator knows which file to fix.
_KNOWN_MONGO_ALIASES: frozenset[str] = frozenset(
    {
        "MONGO_URI",  # #42/#48 — fall-through alias for MONGODB_URI
        "MONGODB_DB_NAME",  # #38 — alias for MONGODB_CHUNK_DB
        "MONGODB_DB",  # historical short form
        "MONGO_DB",  # historical short form
    }
)


# Drift the user has agreed to accept (cross-vertical fixtures pending
# taxonomy expansion). Each entry must document ticket reference.
KNOWN_VERTICAL_EXCEPTIONS: dict[str, str] = {
    # Pending S31-#56 — defi_trade_rubric_suite uses sub-vertical
    # tags ("lending", "perps", "lst", "infra") that the canonical
    # Vertical Literal does not yet model. The taxonomy expansion
    # decision is queued separately; until then, surface the drift
    # without blocking the gate.
    # NOTE: leave this dict empty if you want the test to FAIL on
    # those — that is the intended default per S31-#55 spec.
}

KNOWN_PROVIDER_KIND_EXCEPTIONS: dict[str, str] = {
    # Pending S31-#57 — defi_trade_rubric_suite cites "market_data"
    # which is not yet a canonical ProviderKind. The market_data
    # adapter is in flight on a sibling worktree; once it lands and
    # adds the Literal value, this exception can be removed.
}


# ---------------------------------------------------------------------------
# Fixture loader — shared by Dimensions 1 + 2.
# ---------------------------------------------------------------------------


def _iter_fixture_items() -> list[tuple[Path, int, dict]]:
    """Return ``(path, index, item)`` for every case in every suite.

    Suites are either a top-level list of dicts or a dict with a
    ``cases`` / ``items`` key. Non-dict entries are skipped silently.
    """
    out: list[tuple[Path, int, dict]] = []
    for path in sorted(_SUITES_DIR.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        items = (
            data
            if isinstance(data, list)
            else (data.get("cases") or data.get("items") or [])
        )
        for idx, item in enumerate(items):
            if isinstance(item, dict):
                out.append((path, idx, item))
    return out


# ---------------------------------------------------------------------------
# Dimension 1 — Fixture vertical drift.
# ---------------------------------------------------------------------------


def test_fixture_vertical_in_canonical_literal() -> None:
    """Every ``vertical`` field in eval suites must be in ``VERTICALS``."""
    violations: list[str] = []
    for path, idx, item in _iter_fixture_items():
        v = item.get("vertical")
        if v is None:
            continue
        if v in KNOWN_VERTICAL_EXCEPTIONS:
            continue
        if v not in VERTICALS:
            violations.append(
                f"  {path.name}[{idx}] id={item.get('id')!r}: "
                f"vertical={v!r} not in canonical Vertical Literal "
                f"(allowed: {sorted(VERTICALS)})"
            )
    assert not violations, (
        "Fixture vertical drift — values not in "
        "gecko_core.knowledge.taxonomy.Vertical:\n" + "\n".join(violations)
    )


# ---------------------------------------------------------------------------
# Dimension 2 — Fixture must_cite_provider_kinds drift.
# ---------------------------------------------------------------------------


def test_fixture_provider_kinds_in_canonical_literal() -> None:
    """Every ``must_cite_provider_kinds`` entry must be in ``PROVIDER_KINDS``."""
    violations: list[str] = []
    for path, idx, item in _iter_fixture_items():
        kinds = item.get("must_cite_provider_kinds") or []
        for pk in kinds:
            if pk in KNOWN_PROVIDER_KIND_EXCEPTIONS:
                continue
            if pk not in PROVIDER_KINDS:
                violations.append(
                    f"  {path.name}[{idx}] id={item.get('id')!r}: "
                    f"provider_kind={pk!r} not in canonical ProviderKind "
                    f"Literal (allowed: {sorted(PROVIDER_KINDS)})"
                )
    assert not violations, (
        "Fixture provider_kind drift — values not in "
        "gecko_core.sources.types.ProviderKind:\n" + "\n".join(violations)
    )


# ---------------------------------------------------------------------------
# Dimension 3 — $vectorSearch admit-list drift.
# ---------------------------------------------------------------------------


def _extract_provider_kind_admit_lists(
    source_path: Path,
) -> list[tuple[int, set[str]]]:
    """Parse a module; return ``(lineno, admit_set)`` for each
    ``$vectorSearch`` dict literal whose ``filter.provider_kind`` carries
    a ``$in: [...]`` admit list of string constants.

    Returns ``[]`` when no admit-list is present (current trade_panel
    state — the pipeline filters on ``vertical`` only and trusts
    downstream ``$match`` for cross-cutting canon). The test treats an
    empty result as a pass: no filtering means no exclusion. The instant
    an admit-list is added, this extractor surfaces it.
    """
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    results: list[tuple[int, set[str]]] = []

    def _string_keys(d: ast.Dict) -> dict[str, ast.expr]:
        out: dict[str, ast.expr] = {}
        for k, v in zip(d.keys, d.values, strict=False):
            if isinstance(k, ast.Constant) and isinstance(k.value, str):
                out[k.value] = v
        return out

    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        keys = _string_keys(node)
        vs = keys.get("$vectorSearch")
        if not isinstance(vs, ast.Dict):
            continue
        vs_keys = _string_keys(vs)
        filt = vs_keys.get("filter")
        if not isinstance(filt, ast.Dict):
            continue
        filt_keys = _string_keys(filt)
        pk = filt_keys.get("provider_kind")
        if not isinstance(pk, ast.Dict):
            continue
        pk_keys = _string_keys(pk)
        in_node = pk_keys.get("$in")
        if not isinstance(in_node, ast.List):
            continue
        admit: set[str] = set()
        for el in in_node.elts:
            if isinstance(el, ast.Constant) and isinstance(el.value, str):
                admit.add(el.value)
        results.append((node.lineno, admit))
    return results


def test_vector_search_admit_list_covers_provider_kinds() -> None:
    """Trade-panel ``$vectorSearch.filter.provider_kind.$in`` (if present)
    must be a superset of every declared ``ProviderKind``.

    No admit-list = vacuous pass (the pipeline doesn't filter on
    provider_kind, so nothing can be silently excluded). One added = it
    must cover every declared kind, or the test fails with the
    missing-value diff.
    """
    assert _TRADE_PANEL.exists(), (
        f"trade_panel module not found at {_TRADE_PANEL} — "
        "did the package layout change? Update _TRADE_PANEL in this test."
    )
    admit_lists = _extract_provider_kind_admit_lists(_TRADE_PANEL)
    if not admit_lists:
        return  # vacuous pass — no filtering, no drift possible
    canonical = set(PROVIDER_KINDS)
    violations: list[str] = []
    for lineno, admit in admit_lists:
        missing = canonical - admit
        if missing:
            violations.append(
                f"  {_TRADE_PANEL.name}:{lineno} provider_kind.$in admit-list "
                f"missing canonical kinds: {sorted(missing)} "
                f"(present: {sorted(admit)})"
            )
    assert not violations, (
        "$vectorSearch admit-list drift — every canonical ProviderKind "
        "must appear in the admit-list (silent reachability bug otherwise; "
        "see S31-#43 bazaar_manifest):\n" + "\n".join(violations)
    )


# ---------------------------------------------------------------------------
# Dimension 4 — MongoDB env-var alias drift.
# ---------------------------------------------------------------------------


def _iter_os_environ_get_keys(
    path: Path,
) -> list[tuple[int, str]]:
    """Return ``(lineno, key)`` for every ``os.environ.get("KEY", ...)``
    or ``os.getenv("KEY", ...)`` call in ``path``.

    Pure AST walk — string regex would miss keyword-arg forms and false-
    flag string literals in docstrings.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return []
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_environ_get = (
            isinstance(func, ast.Attribute)
            and func.attr == "get"
            and isinstance(func.value, ast.Attribute)
            and func.value.attr == "environ"
            and isinstance(func.value.value, ast.Name)
            and func.value.value.id == "os"
        )
        is_getenv = (
            isinstance(func, ast.Attribute)
            and func.attr == "getenv"
            and isinstance(func.value, ast.Name)
            and func.value.id == "os"
        )
        if not (is_environ_get or is_getenv):
            continue
        if not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            out.append((node.lineno, first.value))
    return out


def test_scripts_mongo_env_vars_are_canonical() -> None:
    """``scripts/`` must reference only canonical MongoDB env-var names.

    Aliases like ``MONGO_URI`` (a fall-through next to ``MONGODB_URI``)
    or ``MONGODB_DB_NAME`` cause Pattern A drift: the script wires up
    to a different DB than production and writes to / reads from the
    wrong place. The fix is always one canonical name per concept.
    """
    if not _SCRIPTS_DIR.exists():
        pytest.skip("scripts/ directory not present in this checkout")
    violations: list[str] = []
    for py in sorted(_SCRIPTS_DIR.rglob("*.py")):
        for lineno, key in _iter_os_environ_get_keys(py):
            # Only police MONGO* / MONGODB* keys here. Other env-vars
            # have their own canonicality regime.
            if not (key.startswith("MONGO_") or key.startswith("MONGODB_")):
                continue
            if key in CANONICAL_MONGO_ENV_VARS:
                continue
            rel = py.relative_to(_REPO_ROOT)
            if key in _KNOWN_MONGO_ALIASES:
                violations.append(
                    f"  {rel}:{lineno} uses known alias {key!r} — "
                    f"replace with a canonical name: "
                    f"{sorted(CANONICAL_MONGO_ENV_VARS)}"
                )
            else:
                violations.append(
                    f"  {rel}:{lineno} uses unknown Mongo env-var {key!r} — "
                    f"if this is a new canonical name, add it to "
                    f"CANONICAL_MONGO_ENV_VARS in this test; "
                    f"otherwise replace with one of: "
                    f"{sorted(CANONICAL_MONGO_ENV_VARS)}"
                )
    assert not violations, (
        "Mongo env-var alias drift in scripts/ — Pattern A violation:\n"
        + "\n".join(violations)
    )
