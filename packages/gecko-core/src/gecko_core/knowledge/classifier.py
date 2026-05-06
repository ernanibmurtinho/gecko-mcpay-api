"""Chunk-level knowledge classifier (S20-A2).

Distinct from ``gecko_core.routing.classifier`` — that module is the
QUERY-time classifier (single short prompt, latency-sensitive). This
module classifies *chunks* at INGEST time: many texts at once, batched
to amortize LLM cost, deterministic JSON output aligned 1:1 with the
input list.

Per Pattern A in CLAUDE.md, every Literal value comes from
``gecko_core.knowledge.taxonomy`` — no redeclarations. Subcategory is a
free-form string validated via ``is_valid_subcategory``; on a miss we
fall back to the first canonical subcategory of the predicted category
(silent recovery is OK here because subcategories are open-set).

Output schema per text:
    {"category": <Category>,
     "subcategory": <str>,
     "vertical": <Vertical>,
     "confidence": <float 0..1>}

When the model's reported confidence on the vertical is below
``_VERTICAL_CONFIDENCE_THRESHOLD`` we coerce ``vertical`` to ``"unknown"``
— same honest-unknown contract as the query-time classifier.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from openai import AsyncOpenAI
from pydantic import BaseModel, Field, ValidationError

from gecko_core.ingestion.settings import get_ingestion_settings
from gecko_core.knowledge.taxonomy import (
    CATEGORIES,
    SUBCATEGORIES,
    VERTICALS,
    Category,
    Vertical,
    is_valid_subcategory,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module budgets
# ---------------------------------------------------------------------------

_MODEL: str = "gpt-4o-mini"
_MAX_OUTPUT_TOKENS_PER_BATCH: int = 200
_MAX_INPUT_CHARS_PER_TEXT: int = 6000  # ~1.5K tokens (4 chars/token).
_VERTICAL_CONFIDENCE_THRESHOLD: float = 0.6
_DEFAULT_BATCH_SIZE: int = 20


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


class ChunkClassification(BaseModel):
    """One classification result per chunk.

    Aligned 1:1 with the input list of texts.
    """

    category: Category
    subcategory: str
    vertical: Vertical
    confidence: float = Field(..., ge=0.0, le=1.0)


class ChunkClassifierParseError(RuntimeError):
    """Raised when the LLM returns JSON that fails parsing or shape validation.

    Captures the raw model output on the exception (``raw_output``) so logs
    and traces can diagnose schema drift without re-querying.
    """

    def __init__(self, message: str, *, raw_output: str) -> None:
        super().__init__(message)
        self.raw_output = raw_output


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


def _build_subcat_listing() -> str:
    lines: list[str] = []
    for cat in CATEGORIES:
        subs = ", ".join(SUBCATEGORIES.get(cat, ()))
        lines.append(f"  - {cat}: {subs}")
    return "\n".join(lines)


def _build_prompt(texts: list[str]) -> str:
    cats = ", ".join(CATEGORIES)
    verts = ", ".join(VERTICALS)
    sub_listing = _build_subcat_listing()
    body_lines = [f"[{i}] {t}" for i, t in enumerate(texts)]
    body = "\n".join(body_lines)
    return (
        "You are a chunk-level knowledge classifier for a categorized retrieval "
        "system. For EACH input chunk, classify along three axes and return STRICT "
        "JSON.\n\n"
        f"Allowed categories: {cats}\n"
        f"Allowed verticals (use 'unknown' if unsure): {verts}\n"
        "Allowed subcategories (canonical seeds; pick the closest match):\n"
        f"{sub_listing}\n\n"
        'Return JSON of shape: {"results": [<one object per chunk in order>]}\n'
        "Each object has keys: category (string), subcategory (string), "
        "vertical (string), confidence (0-1 float).\n"
        "Output array length MUST equal input array length. No prose, no markdown.\n\n"
        f"Chunks ({len(texts)}):\n{body}"
    )


# ---------------------------------------------------------------------------
# Per-batch call
# ---------------------------------------------------------------------------


def _coerce_one(raw: dict[str, Any]) -> ChunkClassification:
    """Coerce a single raw result dict into ``ChunkClassification``.

    Subcategory falls back to the first canonical subcategory of the
    predicted category when the model's value is not registered.
    Vertical is coerced to ``"unknown"`` when confidence is below the
    threshold (honest-unknown contract).
    """
    category = raw.get("category")
    subcategory = raw.get("subcategory") or ""
    vertical = raw.get("vertical")
    confidence_raw = raw.get("confidence", 0.0)
    try:
        confidence = float(confidence_raw)
    except (TypeError, ValueError):
        confidence = 0.0
    if confidence < 0.0:
        confidence = 0.0
    if confidence > 1.0:
        confidence = 1.0

    if category not in CATEGORIES:
        raise ValidationError.from_exception_data(
            "ChunkClassification",
            [
                {
                    "type": "literal_error",
                    "loc": ("category",),
                    "input": category,
                    "ctx": {"expected": ", ".join(CATEGORIES)},
                }
            ],
        )
    cat: Category = category
    if vertical not in VERTICALS:
        # Unknown vertical string — coerce to 'unknown'.
        vert: Vertical = "unknown"
    else:
        vert = vertical
    if not is_valid_subcategory(cat, subcategory):
        # Fall back to first canonical subcategory for the predicted category.
        canonicals = SUBCATEGORIES.get(cat, ())
        subcategory = canonicals[0] if canonicals else subcategory

    if confidence < _VERTICAL_CONFIDENCE_THRESHOLD:
        vert = "unknown"

    return ChunkClassification(
        category=cat,
        subcategory=subcategory,
        vertical=vert,
        confidence=confidence,
    )


async def _classify_one_batch(
    texts: list[str],
    *,
    client: AsyncOpenAI,
) -> list[ChunkClassification]:
    """Single LLM call for ``texts``. Aligned 1:1 with input order."""
    prompt = _build_prompt(texts)
    t0 = time.perf_counter()
    completion: Any = await client.chat.completions.create(
        model=_MODEL,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        max_tokens=_MAX_OUTPUT_TOKENS_PER_BATCH,
        temperature=0.0,
    )
    ms_elapsed = int((time.perf_counter() - t0) * 1000)

    raw_output = completion.choices[0].message.content or ""
    usage = getattr(completion, "usage", None)
    tokens_in = int(getattr(usage, "prompt_tokens", 0) or 0)
    tokens_out = int(getattr(usage, "completion_tokens", 0) or 0)

    try:
        payload = json.loads(raw_output)
    except json.JSONDecodeError as exc:
        raise ChunkClassifierParseError(
            f"chunk classifier returned invalid JSON: {exc}",
            raw_output=raw_output,
        ) from exc

    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list) or len(results) != len(texts):
        raise ChunkClassifierParseError(
            (
                "chunk classifier returned wrong shape: expected "
                f"{{'results': [list of {len(texts)}]}}, got len="
                f"{len(results) if isinstance(results, list) else 'n/a'}"
            ),
            raw_output=raw_output,
        )

    out: list[ChunkClassification] = []
    for r in results:
        if not isinstance(r, dict):
            raise ChunkClassifierParseError(
                f"chunk classifier returned non-object element: {r!r}",
                raw_output=raw_output,
            )
        try:
            out.append(_coerce_one(r))
        except ValidationError as exc:
            raise ChunkClassifierParseError(
                f"chunk classifier element failed validation: {exc}",
                raw_output=raw_output,
            ) from exc

    logger.info(
        "knowledge.classify_chunks.batch",
        extra={
            "batch_size": len(texts),
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "ms_elapsed": ms_elapsed,
        },
    )
    return out


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def classify_chunks(
    texts: list[str],
    *,
    batch_size: int = _DEFAULT_BATCH_SIZE,
    client: AsyncOpenAI | None = None,
) -> list[ChunkClassification]:
    """Classify ``texts`` chunk-by-chunk. Returns a list aligned 1:1.

    Truncates each text at ``_MAX_INPUT_CHARS_PER_TEXT`` (≈1.5K tokens)
    before sending. Splits into batches of ``batch_size`` (default 20)
    and issues one LLM call per batch. Each call uses ``gpt-4o-mini``
    with deterministic ``temperature=0.0`` and JSON-mode output.
    """
    if not texts:
        return []
    if batch_size <= 0:
        raise ValueError("batch_size must be > 0")

    truncated = [t[:_MAX_INPUT_CHARS_PER_TEXT] for t in texts]

    if client is None:
        settings = get_ingestion_settings()
        if settings.openai_api_key is None:
            raise ValueError("OPENAI_API_KEY must be set to call classify_chunks")
        client = AsyncOpenAI(api_key=settings.openai_api_key.get_secret_value())

    out: list[ChunkClassification] = []
    for start in range(0, len(truncated), batch_size):
        batch = truncated[start : start + batch_size]
        out.extend(await _classify_one_batch(batch, client=client))
    return out


__all__ = [
    "ChunkClassification",
    "ChunkClassifierParseError",
    "classify_chunks",
]
