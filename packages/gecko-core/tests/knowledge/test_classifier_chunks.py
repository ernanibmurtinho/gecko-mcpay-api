"""Tests for the chunk-level knowledge classifier (S20-A2).

Stubs ``AsyncOpenAI`` so the classifier never reaches the network. The
fake completion shape mirrors what the production OpenAI SDK returns
(``.choices[0].message.content`` + ``.usage.prompt_tokens`` /
``.completion_tokens``).
"""

from __future__ import annotations

import inspect
import json
from typing import Any

import pytest
from gecko_core.knowledge import classifier as cls_mod
from gecko_core.knowledge.classifier import (
    ChunkClassification,
    ChunkClassifierParseError,
    classify_chunks,
)
from gecko_core.knowledge.taxonomy import CATEGORIES, VERTICALS

# ---------------------------------------------------------------------------
# Fake AsyncOpenAI client
# ---------------------------------------------------------------------------


class _FakeMessage:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeChoice:
    def __init__(self, content: str) -> None:
        self.message = _FakeMessage(content)


class _FakeUsage:
    def __init__(self, prompt_tokens: int, completion_tokens: int) -> None:
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class _FakeCompletion:
    def __init__(self, content: str) -> None:
        self.choices = [_FakeChoice(content)]
        self.usage = _FakeUsage(123, 45)


class _FakeChatCompletions:
    def __init__(self, responses: list[str]) -> None:
        # Each call pops the next canned response. If the list runs out,
        # the last response is reused (helps with single-batch tests).
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> _FakeCompletion:
        self.calls.append(kwargs)
        content = self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]
        return _FakeCompletion(content)


class _FakeChat:
    def __init__(self, completions: _FakeChatCompletions) -> None:
        self.completions = completions


class _FakeAsyncOpenAI:
    def __init__(self, responses: list[str]) -> None:
        self.completions = _FakeChatCompletions(responses)
        self.chat = _FakeChat(self.completions)


def _wrap(results: list[dict[str, Any]]) -> str:
    return json.dumps({"results": results})


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_canned_neobank_query() -> None:
    """A canned response for a regulated USDC neobank chunk classifies cleanly."""
    canned = _wrap(
        [
            {
                "category": "business_financial",
                "subcategory": "regulatory",
                "vertical": "neobank",
                "confidence": 0.92,
            }
        ]
    )
    client = _FakeAsyncOpenAI([canned])
    out = await classify_chunks(
        ["I want to launch a USDC neobank in Brazil"],
        client=client,  # type: ignore[arg-type]
    )
    assert len(out) == 1
    assert out[0].category == "business_financial"
    assert out[0].subcategory == "regulatory"
    assert out[0].vertical == "neobank"
    assert out[0].confidence >= 0.7


@pytest.mark.asyncio
async def test_batches_split_at_size_20() -> None:
    """25 inputs split into batches of 20 + 5 → 2 LLM calls, aligned outputs."""
    batch1 = _wrap(
        [
            {
                "category": "ai_ml",
                "subcategory": "rag",
                "vertical": "ai_agent_platform",
                "confidence": 0.8,
            }
        ]
        * 20
    )
    batch2 = _wrap(
        [
            {
                "category": "product",
                "subcategory": "feature",
                "vertical": "consumer_social",
                "confidence": 0.7,
            }
        ]
        * 5
    )
    client = _FakeAsyncOpenAI([batch1, batch2])
    texts = [f"chunk-{i}" for i in range(25)]
    out = await classify_chunks(texts, batch_size=20, client=client)  # type: ignore[arg-type]
    assert len(out) == 25
    assert len(client.completions.calls) == 2
    assert all(o.category == "ai_ml" for o in out[:20])
    assert all(o.category == "product" for o in out[20:])


@pytest.mark.asyncio
async def test_invalid_subcategory_falls_back_to_first() -> None:
    """Unknown subcategory → falls back to the first canonical for that category."""
    canned = _wrap(
        [
            {
                "category": "ai_ml",
                "subcategory": "totally_made_up_subcat",
                "vertical": "ai_agent_platform",
                "confidence": 0.85,
            }
        ]
    )
    client = _FakeAsyncOpenAI([canned])
    out = await classify_chunks(["a chunk about models"], client=client)  # type: ignore[arg-type]
    # First canonical ai_ml subcategory is "model" per the taxonomy seed.
    assert out[0].subcategory == "model"


@pytest.mark.asyncio
async def test_malformed_json_raises_with_raw_capture() -> None:
    """Non-JSON output raises ChunkClassifierParseError preserving raw_output."""
    raw = "this is not JSON at all { broken"
    client = _FakeAsyncOpenAI([raw])
    with pytest.raises(ChunkClassifierParseError) as exc_info:
        await classify_chunks(["x"], client=client)  # type: ignore[arg-type]
    assert exc_info.value.raw_output == raw


@pytest.mark.asyncio
async def test_low_vertical_confidence_coerces_to_unknown() -> None:
    """confidence < 0.6 on the vertical → vertical='unknown' (honest unknown)."""
    canned = _wrap(
        [
            {
                "category": "market_intelligence",
                "subcategory": "trend",
                "vertical": "neobank",
                "confidence": 0.3,
            }
        ]
    )
    client = _FakeAsyncOpenAI([canned])
    out = await classify_chunks(["fuzzy market chatter"], client=client)  # type: ignore[arg-type]
    assert out[0].vertical == "unknown"


@pytest.mark.asyncio
async def test_wrong_length_results_raises() -> None:
    """Aligned-length contract: shorter results array than inputs raises."""
    canned = _wrap(
        [
            {
                "category": "ai_ml",
                "subcategory": "rag",
                "vertical": "ai_agent_platform",
                "confidence": 0.9,
            }
        ]
    )
    client = _FakeAsyncOpenAI([canned])
    with pytest.raises(ChunkClassifierParseError):
        await classify_chunks(["a", "b", "c"], client=client)  # type: ignore[arg-type]


def test_prompt_lists_all_categories_and_verticals() -> None:
    """Schema-drift guard: every Category and Vertical name appears in the prompt source."""
    src = inspect.getsource(cls_mod)
    # The prompt is built dynamically; assert the helpers reference the canonical tuples.
    assert "CATEGORIES" in src
    assert "VERTICALS" in src
    # And asserting via a built prompt covers the runtime path:
    prompt = cls_mod._build_prompt(["sample"])
    for c in CATEGORIES:
        assert c in prompt, f"category {c!r} missing from prompt"
    for v in VERTICALS:
        assert v in prompt, f"vertical {v!r} missing from prompt"


@pytest.mark.asyncio
async def test_empty_input_returns_empty() -> None:
    """No texts → no LLM call, returns []."""
    client = _FakeAsyncOpenAI([_wrap([])])
    out = await classify_chunks([], client=client)  # type: ignore[arg-type]
    assert out == []
    assert client.completions.calls == []


def test_chunk_classification_is_pydantic_model() -> None:
    """Type signature contract — ChunkClassification is the public schema."""
    cc = ChunkClassification(
        category="ai_ml",
        subcategory="model",
        vertical="ai_agent_platform",
        confidence=0.9,
    )
    assert cc.category == "ai_ml"
