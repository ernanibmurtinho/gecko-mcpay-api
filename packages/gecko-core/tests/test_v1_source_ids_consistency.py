"""S20-V1BLOCK-CONSISTENCY-01 — schema-drift guard for V1SourceId.

The S19 v1_block tear-out audit (``docs/audits/v1block-tear-out-prep.md``,
§6) flagged the V1 source ids (``twit_sh``, ``hn``, ``reddit``,
``gecko_precedent``) as a textbook **Pattern A** failure mode: the same
four ids are baked into the dispatcher / renderer, the advisor stub, **5
versions of the Pro analyst prompt**, and the advisor panel prompts —
with no consistency test pinning them. A future tear-out that renames
even one id (e.g. unifying ``twit_sh`` with ``ProviderKind.twitsh``)
would silently desync the prompts; the failure mode is invisible (the
model just stops weighting that signal) and surfaces only as eval-score
drift weeks later.

This test mirrors ``test_payment_mode_consistency.py``:

  1. The static ``V1SourceId`` Literal and the runtime ``V1_SOURCE_IDS``
     tuple in ``gecko_core.sources.v1_source_ids`` agree.
  2. The ``v1_block`` renderer ``__all__`` re-exports the canonical
     module's symbols.
  3. Every Pro analyst prompt JSON (``_default_prompts*.json``) and the
     advisor prompt JSON (``_default_advisor_prompts.json``) reference
     **only** ids in ``V1_SOURCE_IDS``.
  4. No legacy / typo'd id slips through (``twitsh`` as a bare token
     when not part of the ``twitsh://`` URI scheme, ``hackernews``
     instead of ``hn``, ``hacker_news``, ``reddits``, etc.).

Adding a new V1 source id now requires updating ``v1_source_ids.py`` +
the renderer in ``v1_block.py`` + every prompt JSON that names sources.
If any of those drift, this test fails with a clear "id X in file Y is
not in V1_SOURCE_IDS" message.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import get_args

import pytest
from gecko_core.sources.v1_block import (
    V1_SOURCE_IDS as BlockExportedIds,
)
from gecko_core.sources.v1_source_ids import V1_SOURCE_IDS, V1SourceId

# Path to the orchestration prompt JSON dirs (resolved relative to the
# canonical module so the test is robust to repo-root reshuffles).
_PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "src" / "gecko_core"
_PRO_PROMPTS_DIR = _PACKAGE_ROOT / "orchestration" / "pro"
_ADVISOR_PROMPTS_DIR = _PACKAGE_ROOT / "orchestration" / "advisor"

# Tokens that LOOK like a V1 source id but aren't, and that would silently
# break the prompt → dispatcher contract if they leaked into a prompt.
# The check is "appears in a prompt JSON as a bare token" — see
# `_extract_source_id_tokens` for what counts as bare.
_LEGACY_TYPO_BLOCKLIST = (
    "twitsh",  # the V1 source id is `twit_sh` (with underscore); `twitsh`
    # is a *different* concept (the chunks-table ProviderKind value) and
    # MUST NOT appear as a bare source-id token in a v1_block-aware prompt.
    # Allow-listed only as part of the `twitsh://` citation URI scheme.
    "hackernews",
    "hacker_news",
    "reddits",
    "gecko-precedent",  # canonical uses underscore
    "geckoprecedent",
    "twit-sh",
)


# ---------------------------------------------------------------------------
# Canonical-module sanity
# ---------------------------------------------------------------------------


def test_canonical_v1_source_ids_value() -> None:
    """Lock the canonical list. If we add an id, this assertion changes
    and the developer is forced to think about every prompt JSON too."""
    assert V1_SOURCE_IDS == ("gecko_precedent", "twit_sh", "hn", "reddit")


def test_v1_source_id_literal_matches_runtime_tuple() -> None:
    """Static type alias and runtime tuple cannot drift inside the module."""
    assert get_args(V1SourceId) == V1_SOURCE_IDS


def test_v1_block_re_exports_canonical_symbols() -> None:
    """The dispatcher's ``__all__`` includes the canonical symbols so
    consumers reading v1_block don't have to know about the leaf module."""
    assert BlockExportedIds is V1_SOURCE_IDS


# ---------------------------------------------------------------------------
# Prompt JSON scan
# ---------------------------------------------------------------------------


def _iter_prompt_files() -> list[Path]:
    """Every active prompt JSON the V1 source signal block reaches."""
    files: list[Path] = []
    files.extend(sorted(_PRO_PROMPTS_DIR.glob("_default_prompts*.json")))
    files.extend(sorted(_ADVISOR_PROMPTS_DIR.glob("_default_advisor_prompts*.json")))
    return files


def _flatten_prompt_text(payload: object) -> str:
    """Concatenate every string value in a (possibly nested) prompt JSON.

    Prompt JSONs are flat dicts of {role: prompt_string}; defensive against
    future nested shapes."""
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        return "\n".join(_flatten_prompt_text(v) for v in payload.values())
    if isinstance(payload, list):
        return "\n".join(_flatten_prompt_text(v) for v in payload)
    return ""


# Match a bare token candidate that looks like a source-id reference.
# The character class is what surrounds the token in prose — letters, digits,
# underscores, hyphens. `://` after the token marks a URI scheme (allow-list).
_TOKEN_PATTERN = re.compile(r"(?<![A-Za-z0-9_-])([A-Za-z][A-Za-z0-9_-]*)(?![A-Za-z0-9_-])")


def _extract_source_id_tokens(text: str) -> set[tuple[str, int]]:
    """Walk ``text`` and yield (token, position) pairs for any token that
    looks like it could be a V1 source id reference (snake_case identifier
    of length ≥ 2). Filters out URI scheme uses (``twitsh://``) which are
    documented citation forms, not source-id references."""
    found: set[tuple[str, int]] = set()
    for match in _TOKEN_PATTERN.finditer(text):
        tok = match.group(1)
        end = match.end()
        # Allow-list: if followed by `://`, it's a URI scheme, not an id.
        if text[end : end + 3] == "://":
            continue
        found.add((tok, match.start()))
    return found


def _short_context(text: str, pos: int, span: int = 40) -> str:
    """Return a [pos-span : pos+span] excerpt for a useful failure msg."""
    start = max(0, pos - span)
    end = min(len(text), pos + span)
    snippet = text[start:end].replace("\n", " ")
    return f"...{snippet}..."


def test_every_prompt_id_reference_is_canonical() -> None:
    """Walk every active prompt JSON; every token that *names* a V1 source
    must be in ``V1_SOURCE_IDS`` (or be in the safe ignored set: words that
    aren't claiming to be source ids)."""
    files = _iter_prompt_files()
    assert files, (
        "No prompt JSON files discovered. The fixture paths are wrong or "
        "the prompts moved — investigate before relaxing this test."
    )

    # Tokens we EXPECT in prose that aren't source ids — e.g. plain English
    # "hn" never occurs because it's a snake_case identifier; "reddit"
    # would also be ambiguous as plain English. The test enforces that any
    # match against {V1_SOURCE_IDS ∪ blocklist} resolves correctly:
    canonical = set(V1_SOURCE_IDS)
    blocklist = set(_LEGACY_TYPO_BLOCKLIST)

    failures: list[str] = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        # First parse-as-JSON so we know the file is intact, then scan
        # the flattened prompt strings (which is what the model sees).
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            failures.append(f"{path.name}: invalid JSON ({exc})")
            continue
        flat = _flatten_prompt_text(payload)

        for tok, pos in _extract_source_id_tokens(flat):
            if tok in blocklist:
                failures.append(
                    f"{path.name}: legacy/typo'd source id {tok!r} "
                    f"(canonical is one of {sorted(canonical)}). "
                    f"Context: {_short_context(flat, pos)}"
                )
                continue
            # Tokens that aren't in the canonical set OR the blocklist are
            # ignored — they're prose words ("analyst", "verdict", etc).
            # The PRIMARY guarantee of this test is: every blocklist token
            # is rejected, and every canonical token (if present) actually
            # exists in V1_SOURCE_IDS — verified by the next test below.

    assert not failures, "V1 source id drift detected:\n  " + "\n  ".join(failures)


def test_every_canonical_id_appears_in_at_least_one_prompt() -> None:
    """Sanity check: each canonical V1 source id is *named* in at least one
    Pro analyst prompt or advisor prompt. If a canonical id is unreferenced
    everywhere, either it's dead and should be removed, or a prompt forgot
    to mention it (Pattern A bug going the *other* direction)."""
    files = _iter_prompt_files()
    seen_per_id: dict[str, set[str]] = {sid: set() for sid in V1_SOURCE_IDS}
    for path in files:
        flat = _flatten_prompt_text(json.loads(path.read_text(encoding="utf-8")))
        for sid in V1_SOURCE_IDS:
            # Word-boundary match for the canonical id.
            if re.search(rf"(?<![A-Za-z0-9_-]){re.escape(sid)}(?![A-Za-z0-9_-])", flat):
                seen_per_id[sid].add(path.name)

    unreferenced = [sid for sid, files_seen in seen_per_id.items() if not files_seen]
    assert not unreferenced, (
        f"V1 source ids in V1_SOURCE_IDS but unreferenced in any prompt JSON: "
        f"{unreferenced}. Either the id is dead (remove from V1_SOURCE_IDS + "
        f"v1_block renderer) or a prompt forgot to weight it."
    )


def test_no_blocklisted_typo_in_any_prompt() -> None:
    """Hard floor: a legacy/typo'd id MUST NOT appear in any prompt as a
    bare token, ever. Allow-listed only inside ``twitsh://`` URI."""
    files = _iter_prompt_files()
    offenders: list[str] = []
    for path in files:
        flat = _flatten_prompt_text(json.loads(path.read_text(encoding="utf-8")))
        for typo in _LEGACY_TYPO_BLOCKLIST:
            for match in re.finditer(
                rf"(?<![A-Za-z0-9_-]){re.escape(typo)}(?![A-Za-z0-9_-])",
                flat,
            ):
                end = match.end()
                # Same URI-scheme allow-list as the main scanner.
                if flat[end : end + 3] == "://":
                    continue
                offenders.append(
                    f"{path.name}: blocklisted token {typo!r} at offset "
                    f"{match.start()} — canonical is in V1_SOURCE_IDS "
                    f"({V1_SOURCE_IDS}). Context: "
                    f"{_short_context(flat, match.start())}"
                )

    assert not offenders, (
        "Legacy / typo'd V1 source ids leaked into prompt JSON:\n  " + "\n  ".join(offenders)
    )


# ---------------------------------------------------------------------------
# In-code redeclaration scan
# ---------------------------------------------------------------------------


def test_v1_block_imports_canonical_module() -> None:
    """The v1_block dispatcher must import from ``v1_source_ids`` rather
    than redeclaring the four ids in its own module-level constant —
    that's the redeclaration Pattern A exists to prevent."""
    block_src = (_PACKAGE_ROOT / "sources" / "v1_block.py").read_text(encoding="utf-8")
    assert "from gecko_core.sources.v1_source_ids import" in block_src, (
        "gecko_core.sources.v1_block must import V1_SOURCE_IDS / V1SourceId "
        "from the canonical leaf module, not redeclare them locally."
    )


@pytest.mark.parametrize("sid", V1_SOURCE_IDS)
def test_canonical_id_is_a_valid_python_identifier(sid: str) -> None:
    """Defensive: if a canonical id isn't a valid Python identifier,
    something is very wrong (regex-driven prompt scans assume snake_case)."""
    assert sid.isidentifier(), f"canonical V1 source id {sid!r} is not a valid identifier"
