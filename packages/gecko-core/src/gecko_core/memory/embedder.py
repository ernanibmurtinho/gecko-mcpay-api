"""Thin wrapper over `gecko_core.ingestion.embedder` for memory entries.

The memory layer embeds at *save* time from a textual representation of the
`value` dict and at *search* time from the user's query string. Both paths
share the same retry + concurrency cap as the ingestion pipeline.
"""

from __future__ import annotations

import json
from typing import Any

from gecko_core.ingestion.embedder import embed_for_postgres_vector as _pg_embed


def render_value_for_embedding(
    entry_type: str,
    value: dict[str, Any],
    *,
    key: str | None = None,
) -> str:
    """Build the textual representation embedded at save time.

    Embeds the `value` dict (NOT the `key`, per the dispatch brief). The
    entry_type is prepended so semantically-distinct types don't collapse
    into the same neighborhood (e.g. a `pulse_run` value with
    `current_closing_lines == [...]` shouldn't match a `plan_advised`
    voice list verbatim).
    """
    body = json.dumps(value, sort_keys=True, default=str)
    prefix = f"[{entry_type}] "
    if key:
        prefix += f"({key}) "
    return prefix + body


async def embed_text(text: str) -> list[float]:
    """Embed a single string for the ``memory`` table (``vector(1536)``)."""
    vectors, _tokens = await _pg_embed([text])
    if not vectors:
        raise RuntimeError("embedder returned no vectors")
    return vectors[0]


__all__ = ["embed_text", "render_value_for_embedding"]
