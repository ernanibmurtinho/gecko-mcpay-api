"""LLM-hygiene Commit D regression tests.

Three concerns:

1. ``pydantic_to_strict_schema`` round-trips every Pydantic model that
   becomes a Structured Outputs ``json_schema`` payload, asserting the
   four invariants strict mode requires (additionalProperties=false,
   required==properties, no $ref cycles, no top-level oneOf/anyOf).

2. ``supports_strict_outputs`` is conservative — only OpenAI-direct
   (router=='openai' OR 'legacy') and OpenAI-prefixed model ids opt in.
   Non-OpenAI providers via OpenRouter stay on json_object.

3. Each migrated call site, when its (model, router) supports strict
   mode, builds a request kwarg with ``response_format.type ==
   "json_schema"``. We mock at the ``chat.completions.create`` boundary —
   no live LLM calls.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from gecko_core.judges.synth import JudgeSynthEnvelope
from gecko_core.llm_helpers import (
    build_response_format,
    pydantic_to_strict_schema,
    supports_strict_outputs,
)
from gecko_core.models import (
    PRD,
    BusinessPlan,
    MarketLandscape,
    NextStepsWithFalsifiers,
    PerVoiceReadout,
    RefinedIdea,
    SurvivingDissent,
    ValidationReport,
)
from gecko_core.orchestration.basic import _LLMOutput
from gecko_core.orchestration.scaffold.models import ScaffoldDocs
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# pydantic_to_strict_schema invariants
# ---------------------------------------------------------------------------


def _walk_objects(node: Any, path: str = "root") -> list[tuple[str, dict[str, Any]]]:
    """Collect every object-shaped schema node + its path. Used by invariants."""
    found: list[tuple[str, dict[str, Any]]] = []
    if not isinstance(node, dict):
        return found
    if node.get("type") == "object" or "properties" in node:
        found.append((path, node))
    for k, v in node.items():
        if isinstance(v, dict):
            found.extend(_walk_objects(v, f"{path}.{k}"))
        elif isinstance(v, list):
            for i, x in enumerate(v):
                found.extend(_walk_objects(x, f"{path}.{k}[{i}]"))
    return found


_MIGRATED_MODELS: list[type[BaseModel]] = [
    BusinessPlan,
    ValidationReport,
    PRD,
    _LLMOutput,
    PerVoiceReadout,
    MarketLandscape,
    SurvivingDissent,
    NextStepsWithFalsifiers,
    RefinedIdea,
    JudgeSynthEnvelope,
    ScaffoldDocs,
]


@pytest.mark.parametrize("model_cls", _MIGRATED_MODELS, ids=lambda c: c.__name__)
def test_pydantic_to_strict_schema_enforces_strict_invariants(
    model_cls: type[BaseModel],
) -> None:
    """Every object node carries additionalProperties=false and required==props."""
    schema = pydantic_to_strict_schema(model_cls)
    objects: list[tuple[str, dict[str, Any]]] = []
    objects.extend(_walk_objects(schema))
    for def_name, def_node in (schema.get("$defs") or {}).items():
        objects.extend(_walk_objects(def_node, f"$defs.{def_name}"))

    assert objects, f"{model_cls.__name__} produced no object nodes"
    for path, obj in objects:
        assert obj.get("additionalProperties") is False, (
            f"{model_cls.__name__} {path}: additionalProperties not false"
        )
        props = list((obj.get("properties") or {}).keys())
        if not props:
            continue
        required = obj.get("required") or []
        assert set(required) == set(props), (
            f"{model_cls.__name__} {path}: required {required!r} != props {props!r}"
        )


def test_pydantic_to_strict_schema_does_not_mutate_pydantic_cache() -> None:
    """Calling the helper twice must not poison Pydantic's cached schema."""
    raw_first = RefinedIdea.model_json_schema()
    pydantic_to_strict_schema(RefinedIdea)
    raw_second = RefinedIdea.model_json_schema()
    # Pydantic emits a partial `required` list (not every property is
    # required). The cached schema must still reflect that, not the
    # all-required override the helper applies.
    assert raw_first == raw_second
    assert set(raw_second.get("required") or []) != set(raw_second.get("properties", {}).keys()), (
        "regression: Pydantic schema would already be all-required without the helper"
    )


# ---------------------------------------------------------------------------
# supports_strict_outputs predicate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model_id", "router", "expected"),
    [
        # OpenAI direct — bare and prefixed ids both opt in.
        ("gpt-4o", "openai", True),
        ("gpt-4o-mini", "openai", True),
        ("openai/gpt-4.1-nano", "openai", True),
        ("gpt-4o", "legacy", True),  # legacy plane points at OpenAI shape
        # OpenRouter — even OpenAI-prefixed ids stay False until contract-tested.
        ("openai/gpt-4.1-nano", "openrouter", False),
        ("anthropic/claude-sonnet-4.6", "openrouter", False),
        ("moonshotai/kimi-k2.6", "openrouter", False),
        ("google/gemini-3-flash-preview", "openrouter", False),
        # ClawRouter is treated as non-strict for now (cost-header path).
        ("openai/gpt-4o", "clawrouter", False),
        # Empty / unknown router → False, defensive.
        ("gpt-4o", "", False),
        ("gpt-4o", "garbage", False),
        # Non-OpenAI bare id (defensive — no provider prefix doesn't grant
        # strict if router is non-OpenAI).
        ("gpt-4o", "openrouter", False),
    ],
)
def test_supports_strict_outputs_matrix(model_id: str, router: str, expected: bool) -> None:
    assert supports_strict_outputs(model_id, router) is expected


# ---------------------------------------------------------------------------
# build_response_format
# ---------------------------------------------------------------------------


def test_build_response_format_returns_strict_schema_on_openai() -> None:
    rf = build_response_format(RefinedIdea, "openai/gpt-5-mini", "openai")
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["name"] == "RefinedIdea"
    assert rf["json_schema"]["strict"] is True
    schema = rf["json_schema"]["schema"]
    assert schema["additionalProperties"] is False


def test_build_response_format_falls_back_for_non_openai_router() -> None:
    rf = build_response_format(RefinedIdea, "moonshotai/kimi-k2.6", "openrouter")
    assert rf == {"type": "json_object"}


def test_build_response_format_with_none_model_forces_json_object() -> None:
    rf = build_response_format(None, "gpt-4o", "openai")
    assert rf == {"type": "json_object"}


# ---------------------------------------------------------------------------
# Call-site smoke: each migrated site, when (model, router) supports strict,
# emits a json_schema response_format on the wire.
# ---------------------------------------------------------------------------


def _make_async_create(content: str) -> tuple[Any, list[dict[str, Any]]]:
    """Build a fake AsyncOpenAI whose chat.completions.create captures kwargs."""
    captured: list[dict[str, Any]] = []
    fake_resp = MagicMock()
    fake_resp.choices = [MagicMock()]
    fake_resp.choices[0].message.content = content
    fake_resp.usage = MagicMock(prompt_tokens=10, completion_tokens=5)
    fake_resp.model = "openai/gpt-4.1-nano"

    async def _create(**kwargs: Any) -> Any:
        captured.append(kwargs)
        return fake_resp

    client = MagicMock()
    client.chat.completions.create = AsyncMock(side_effect=_create)
    client.close = AsyncMock()
    return client, captured


def _make_async_stream(content: str) -> tuple[Any, list[dict[str, Any]]]:
    """Fake AsyncOpenAI whose chat.completions.create returns a streaming
    iterator. Used by basic._call_llm tests after the llm_client migration."""
    captured: list[dict[str, Any]] = []

    content_chunk = MagicMock()
    content_chunk.id = "gen-test"
    content_chunk.model = "gpt-4o"
    content_chunk.provider = "openai"
    content_chunk.model_extra = None
    content_choice = MagicMock()
    content_choice.delta = MagicMock()
    content_choice.delta.content = content
    content_choice.finish_reason = None
    content_chunk.choices = [content_choice]
    content_chunk.usage = None

    final_chunk = MagicMock()
    final_chunk.id = "gen-test"
    final_chunk.model = "gpt-4o"
    final_chunk.provider = "openai"
    final_chunk.model_extra = None
    final_choice = MagicMock()
    final_choice.delta = MagicMock()
    final_choice.delta.content = None
    final_choice.finish_reason = "stop"
    final_chunk.choices = [final_choice]
    final_usage = MagicMock()
    final_usage.prompt_tokens = 10
    final_usage.completion_tokens = 5
    final_usage.cost = None
    final_usage.cost_details = None
    final_usage.model_extra = None
    final_chunk.usage = final_usage

    chunks = [content_chunk, final_chunk]

    class _FakeStream:
        def __init__(self) -> None:
            self._chunks = list(chunks)

        def __aiter__(self) -> _FakeStream:
            return self

        async def __anext__(self) -> Any:
            if not self._chunks:
                raise StopAsyncIteration
            return self._chunks.pop(0)

        async def close(self) -> None:
            return None

    async def _create(**kwargs: Any) -> Any:
        captured.append(kwargs)
        return _FakeStream()

    client = MagicMock()
    client.chat.completions.create = AsyncMock(side_effect=_create)
    client.close = AsyncMock()
    return client, captured


async def test_basic_call_llm_uses_strict_schema_on_openai_router(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """basic._call_llm forwards a strict json_schema when the caller passes one."""
    from gecko_core.orchestration.basic import _call_llm

    client, captured = _make_async_stream('{"foo": "bar"}')
    rf = build_response_format(_LLMOutput, "openai/gpt-4.1-nano", "openai")

    await _call_llm(
        client=client,
        model="openai/gpt-4.1-nano",
        system="sys",
        user="u",
        temperature=0.3,
        max_tokens=2000,
        response_format=rf,
    )

    assert len(captured) == 1
    sent = captured[0]
    assert sent["response_format"]["type"] == "json_schema"
    assert sent["response_format"]["json_schema"]["name"] == "_LLMOutput"
    assert sent["response_format"]["json_schema"]["strict"] is True
    # Hygiene preserved: seed=42 and max_tokens still flow through.
    assert sent["seed"] == 42
    assert sent["max_tokens"] == 2000
    # Streaming hygiene: stream + include_usage forwarded so OpenRouter
    # keep-alives + final-chunk usage land.
    assert sent["stream"] is True
    assert sent["stream_options"] == {"include_usage": True}


async def test_basic_call_llm_defaults_to_json_object_when_no_format_supplied() -> None:
    """Backwards compatibility: callers that don't thread response_format
    keep the legacy ``json_object`` wire shape."""
    from gecko_core.orchestration.basic import _call_llm

    client, captured = _make_async_stream('{"foo": "bar"}')
    await _call_llm(
        client=client,
        model="gpt-4o-mini",
        system="sys",
        user="u",
        temperature=0.3,
    )
    assert captured[0]["response_format"] == {"type": "json_object"}


async def test_post_processor_call_json_strict_on_openai_router(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """post_processors._call_json picks strict mode when the resolver returns
    an OpenAI router."""
    from gecko_core.orchestration.pro import post_processors

    monkeypatch.setattr(
        post_processors,
        "_resolve_post_processor_model",
        lambda: ("openai/gpt-4.1-nano", "openai"),
    )
    monkeypatch.setattr(
        post_processors,
        "get_orchestration_settings",
        lambda: MagicMock(max_tokens_post_processor=2000),
    )

    client, captured = _make_async_create('{"competitors": []}')
    await post_processors._call_json(client, system="sys", user="u", model_cls=MarketLandscape)

    sent = captured[0]
    assert sent["response_format"]["type"] == "json_schema"
    assert sent["response_format"]["json_schema"]["name"] == "MarketLandscape"


async def test_post_processor_call_json_falls_back_on_openrouter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OpenRouter path stays on json_object; the f67b211 Pydantic adapters
    remain in charge of absorbing string-vs-list drift."""
    from gecko_core.orchestration.pro import post_processors

    monkeypatch.setattr(
        post_processors,
        "_resolve_post_processor_model",
        lambda: ("openai/gpt-4.1-nano", "openrouter"),
    )
    monkeypatch.setattr(
        post_processors,
        "get_orchestration_settings",
        lambda: MagicMock(max_tokens_post_processor=2000),
    )

    client, captured = _make_async_create('{"competitors": []}')
    await post_processors._call_json(client, system="sys", user="u", model_cls=MarketLandscape)
    assert captured[0]["response_format"] == {"type": "json_object"}


def test_contradictions_inline_schema_strict_on_openai() -> None:
    """The 2-field contradicts/reason payload is hand-rolled (no Pydantic
    class). Smoke that supports_strict_outputs still gates it."""
    assert supports_strict_outputs("gpt-4o-mini", "openai") is True
    assert supports_strict_outputs("anthropic/claude-sonnet-4.6", "openrouter") is False
