"""Single source of truth for the V1 Source Signal block ids (S20-V1BLOCK-CONSISTENCY-01).

The four ids the V1 block dispatcher renders — ``twit_sh``, ``hn``,
``reddit``, ``gecko_precedent`` — are baked into:

  1. ``gecko_core.sources.v1_block.render_block`` (the dispatcher / renderer
     itself; ``_render_*`` keys + ``results.get(...)`` lookups).
  2. ``gecko_core.orchestration.advisor.context`` (empty-state stub +
     the ``v1_source_signal`` plumbing).
  3. The Pro-debate analyst / critic / scoper / judge prompts, versioned
     ``_default_prompts_v5.json`` through ``_default_prompts_v5_4.json``.
  4. The advisor panel CEO / business_manager / product_manager /
     staff_manager prompts (``_default_advisor_prompts.json``).

The S19 v1_block tear-out audit (``docs/audits/v1block-tear-out-prep.md``,
section §6) flagged this as the textbook **Pattern A** failure mode: same
concept declared in 4-plus places, no consistency test pinning them. If a
future tear-out renames an id (e.g. ``twit_sh`` → ``twitsh`` to align with
``ProviderKind``), the dispatcher and advisor stub will follow but the 5
prompt JSONs and 1 advisor prompt JSON will silently drift — the model
just stops weighting that signal, the failure is invisible, and we
discover it in eval-score noise weeks later.

Now every Python consumer imports ``V1_SOURCE_IDS`` (and the matching
``V1SourceId`` Literal) from here. The schema-drift test in
``tests/test_v1_source_ids_consistency.py`` parses every active prompt
JSON and asserts that every source-id token referenced is in
``V1_SOURCE_IDS`` — and that no legacy / typo'd id slips through
(``twitsh`` without underscore as a bare token, ``hackernews`` instead
of ``hn``, etc.).

Note on ``ProviderKind`` vs ``V1SourceId``:

  ``gecko_core.sources.types.ProviderKind`` is a **different** Literal —
  it's the value of the ``chunks.provider_kind`` SQL column and uses
  ``"twitsh"`` (no underscore) as its twit.sh kind. ``V1SourceId`` is
  the V1-block dispatcher id and uses ``"twit_sh"`` (with underscore).
  The two intentionally do not unify; conflating them would re-introduce
  the exact Pattern A bug class this module exists to prevent.

When adding a new V1 source kind:

  1. Add the value to ``V1SourceId`` below.
  2. Add the corresponding ``_render_*`` branch in
     ``gecko_core.sources.v1_block.render_block`` (the heading-contract
     guarantees absence-of-signal renders even when the source returned
     nothing — the new branch must follow the same shape).
  3. Update every active Pro analyst / advisor prompt that names sources
     by id; the consistency test will fail loudly until you do.
  4. The drift test will then pass — and the new id is locked.
"""

from __future__ import annotations

from typing import Final, Literal, get_args

V1SourceId = Literal["gecko_precedent", "twit_sh", "hn", "reddit"]
"""Static type alias for V1-block source dispatch ids. Keep in sync
with ``V1_SOURCE_IDS`` manually — the schema-drift test verifies they
match (``typing.get_args(V1SourceId)``)."""

V1_SOURCE_IDS: Final[tuple[str, ...]] = get_args(V1SourceId)
"""Runtime tuple — used by the v1_block dispatcher, advisor empty-state
stub, and the schema-drift assertion that scans every prompt JSON."""

__all__ = ["V1_SOURCE_IDS", "V1SourceId"]
