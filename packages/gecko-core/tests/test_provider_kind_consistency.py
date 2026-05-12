"""S17-WEDGE-DATA-01 — schema-drift guard for ``ProviderKind``.

Mirrors the shape of ``test_payment_mode_consistency.py`` (Pattern A).
Adding a new provider kind now requires updating both
``gecko_core.sources.types.ProviderKind`` AND a SQL migration that
extends ``chunks_provider_kind_check`` + ``sources_provider_kind_check``.
If any side drifts, the drift test fails with an explicit "X is in
PROVIDER_KINDS but not Y" message.

What we assert:

  1. ``get_args(ProviderKind)`` matches ``PROVIDER_KINDS`` (intra-module
     consistency — the static type alias and runtime tuple cannot drift).
  2. The latest SQL CHECK on ``chunks.provider_kind`` matches.
  3. The latest SQL CHECK on ``sources.provider_kind`` matches.
  4. Provider modules that previously declared their own provider_kind
     values do not import a parallel ``ProviderKind`` Literal — they
     either route through ``gecko_core.sources.types`` or carry an
     adapter-internal string with a different shape (the namespaced
     ``"bazaar:<resource_type>"`` / ``"free:arxiv"`` strings — see the
     ``sources/types.py`` module docstring).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import get_args

from gecko_core.sources.types import PROVIDER_KINDS, ProviderKind

# <repo>/infra/supabase/migrations/.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_MIGRATIONS_DIR = _REPO_ROOT / "infra" / "supabase" / "migrations"


def _latest_check_values(column: str) -> tuple[str, ...]:
    """Walk migrations in name order; the last file that sets a CHECK on
    ``<column> IN (...)`` wins. Returns the parsed value tuple.

    Same scanner shape as ``_latest_payment_mode_check_values`` in
    ``test_payment_mode_consistency.py`` — but with a slightly broader
    pattern to allow underscore-bearing values like ``gecko_precedent``.
    """
    pattern = re.compile(
        rf"\b{re.escape(column)}\s+IN\s*\(\s*((?:'[a-z_]+'(?:\s*,\s*)?)+)\s*\)",
        re.IGNORECASE,
    )
    last_values: tuple[str, ...] | None = None
    for path in sorted(_MIGRATIONS_DIR.glob("*.sql")):
        sql = path.read_text(encoding="utf-8")
        matches = pattern.findall(sql)
        for raw in matches:
            values = tuple(v.strip().strip("'") for v in raw.split(","))
            last_values = values
    if last_values is None:  # pragma: no cover — would mean migrations broke
        raise RuntimeError(
            f"no CHECK constraint found in {_MIGRATIONS_DIR} for column "
            f"{column!r}; either the column was renamed or the migration "
            "that defines it doesn't use a CHECK (column IN (...)) form."
        )
    return last_values


def test_canonical_provider_kinds_value() -> None:
    """Lock the canonical list. Adding a kind is a deliberate change."""
    assert PROVIDER_KINDS == (
        "web",
        "youtube",
        "bazaar",
        "arxiv",
        "twitsh",
        "hn",
        "reddit",
        "gecko_precedent",
        "judge_corpus",
        # S23-FIX-12 (T1) — marketplace providers.
        "paysh_manifest",
        "paysh_live",
        "bazaar_manifest",
        "bazaar_live",
        # Investor-canon corpus (free + public-domain). See
        # docs/strategy/2026-05-11-trade-vertical-expansion.md §6.
        "canon_marks",
        "canon_damodaran",
        "canon_mauboussin",
        "canon_youtube",
        "canon_berkshire",
        "canon_macro",
        # S24 WS-A — market-data grounding (Pyth + DefiLlama).
        "market_data",
    )


def test_marketplace_provider_kinds_present() -> None:
    """S23-FIX-12 (T1) — explicit guard that the four marketplace kinds
    are members of ProviderKind. Without this, the seed corpus chunks
    can't validate through ``RagChunk.model_validate`` at retrieval time
    and the wedge claim ("paysh/bazaar shapes the verdict") is unreachable.
    """
    for kind in (
        "paysh_manifest",
        "paysh_live",
        "bazaar_manifest",
        "bazaar_live",
    ):
        assert kind in PROVIDER_KINDS, (
            f"marketplace provider kind {kind!r} missing from ProviderKind. "
            "S23-FIX-12 requires these to surface seeded chunks at query time."
        )


def test_provider_kind_literal_matches_runtime_tuple() -> None:
    """Static type alias and runtime tuple cannot drift inside types.py."""
    assert get_args(ProviderKind) == PROVIDER_KINDS


def test_chunks_provider_kind_sql_check_matches() -> None:
    """The active CHECK on ``chunks.provider_kind`` must match the Literal.

    THIS is the drift this ticket exists to prevent — adding 'reddit' to
    the Literal without a migration would silently fail every reddit
    chunk insert with PostgreSQL error 23514.
    """
    sql_values = _latest_check_values("provider_kind")
    py_set = set(PROVIDER_KINDS)
    sql_set = set(sql_values)
    only_python = py_set - sql_set
    only_sql = sql_set - py_set
    assert not only_python and not only_sql, (
        "ProviderKind drift detected (Python Literal vs SQL CHECK on "
        "provider_kind):\n"
        f"  python:  {sorted(py_set)}\n"
        f"  sql:     {sorted(sql_set)}\n"
        f"  only in python: {sorted(only_python)}\n"
        f"  only in sql:    {sorted(only_sql)}\n"
        "Add a new migration extending chunks_provider_kind_check + "
        "sources_provider_kind_check OR remove from the Literal."
    )


def test_sources_type_check_includes_provider() -> None:
    """``sources.type`` CHECK must allow 'provider' for synthetic source rows.

    The 20260502 migration extends the original init.sql CHECK from
    ``IN ('youtube','web')`` to ``IN ('youtube','web','provider')`` so
    Bazaar/Arxiv/twit.sh synthetic source rows can be inserted (design
    memo §1.4).
    """
    sql_values = _latest_check_values("type")
    assert "provider" in set(sql_values), (
        "sources.type CHECK must include 'provider'. Latest CHECK values: "
        f"{sql_values}. The 20260502_provider_kind migration is supposed to "
        "extend this; if it didn't take, re-apply migrations."
    )


def test_provider_modules_do_not_redeclare_provider_kind_literal() -> None:
    """Pattern A enforcement — no parallel ``ProviderKind`` Literal.

    Walks every Python file under ``gecko_core/sources/`` (excluding
    ``types.py`` itself) and asserts none of them declare their own
    ``ProviderKind = Literal[...]`` — they must import from
    ``gecko_core.sources.types``.

    Provider-internal strings like ``BazaarChunk.provider_kind: str``
    (a *field type annotation*, namespaced ``"bazaar:<resource_type>"``)
    are a different concept and not flagged here. Only a redeclaration of
    a ``ProviderKind`` Literal would be drift.
    """
    sources_dir = _REPO_ROOT / "packages" / "gecko-core" / "src" / "gecko_core" / "sources"
    redecl_pattern = re.compile(
        r"^\s*ProviderKind\s*(?::\s*\S+\s*)?=\s*Literal\b",
        re.MULTILINE,
    )
    offenders: list[str] = []
    for path in sources_dir.rglob("*.py"):
        if path.name == "types.py":
            continue
        text = path.read_text(encoding="utf-8")
        if redecl_pattern.search(text):
            offenders.append(str(path.relative_to(_REPO_ROOT)))
    assert not offenders, (
        "Pattern A violation: the following modules redeclare a "
        "ProviderKind Literal instead of importing from "
        "gecko_core.sources.types:\n  " + "\n  ".join(offenders)
    )


def test_freshness_tier_values_match_sql_check() -> None:
    """Pattern A: Python literal must match SQL CHECK constraint exactly.

    Scans every migration that touches ``chunks_freshness_tier_check``;
    each Python-side value must appear in at least one of them. The
    original 20260508 migration introduced static/daily/live_only;
    20260512 extended with 'hot' (S24 WS-A market-data).
    """
    from pathlib import Path

    from gecko_core.sources.types import FRESHNESS_TIER_VALUES

    migrations_dir = (
        Path(__file__).parent.parent.parent.parent
        / "infra"
        / "supabase"
        / "migrations"
    )
    sql_combined = "\n".join(
        p.read_text() for p in sorted(migrations_dir.glob("*.sql"))
        if "freshness_tier" in p.read_text()
    )
    for value in FRESHNESS_TIER_VALUES:
        assert f"'{value}'" in sql_combined, (
            f"freshness tier {value!r} missing from SQL CHECK migrations"
        )


def test_content_kind_values_match_sql_check() -> None:
    """Pattern A: ContentKind literal must match SQL CHECK exactly."""
    from pathlib import Path

    from gecko_core.sources.types import CONTENT_KIND_VALUES

    migration = (
        Path(__file__).parent.parent.parent.parent
        / "infra"
        / "supabase"
        / "migrations"
        / "20260508140000_chunk_protocol_content_kind.sql"
    )
    sql = migration.read_text()
    for value in CONTENT_KIND_VALUES:
        assert f"'{value}'" in sql, f"content kind {value!r} missing from SQL CHECK"
