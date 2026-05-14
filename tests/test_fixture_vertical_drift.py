"""S31-#54 drift guard — every fixture's ``vertical`` must be a member of
the canonical :data:`gecko_core.knowledge.taxonomy.Vertical` Literal.

Pattern A (CLAUDE.md): one canonical Literal, every consumer imports from
there. Fixtures that drift silently poison retrieval because the
classifier downstream rejects / down-weights unknown vertical strings.

This is the START of #55 (universal fixture drift test), scoped to the
``vertical`` field only.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import get_args

from gecko_core.knowledge.taxonomy import Vertical

VALID_VERTICALS = set(get_args(Vertical))
SUITE_DIR = Path(__file__).parent / "eval" / "suites"


def _iter_fixtures(suite_obj: object):
    if isinstance(suite_obj, list):
        yield from suite_obj
    elif isinstance(suite_obj, dict):
        fixtures = suite_obj.get("fixtures")
        if isinstance(fixtures, list):
            yield from fixtures


def test_all_fixtures_have_valid_vertical() -> None:
    """Every fixture whose dict contains a ``vertical`` key must use a
    value from the canonical ``Vertical`` Literal."""
    offenders: list[str] = []
    for suite_file in sorted(SUITE_DIR.glob("*.json")):
        suite = json.loads(suite_file.read_text())
        for fixture in _iter_fixtures(suite):
            if not isinstance(fixture, dict):
                continue
            vert = fixture.get("vertical")
            if vert is None:
                continue
            if vert not in VALID_VERTICALS:
                offenders.append(
                    f"{suite_file.name} fixture {fixture.get('id')!r} "
                    f"has vertical={vert!r}"
                )
    assert not offenders, (
        f"Fixture vertical drift detected (valid: {sorted(VALID_VERTICALS)}):\n"
        + "\n".join(offenders)
    )
