"""Tests for `gecko_core.orchestration.scaffold` (S3-01).

Strategy: stub the SessionStore (no Supabase), stub the OpenAI client
(no LLM), assert ScaffoldResult shape + file writes + error paths.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from gecko_core.orchestration.scaffold import (
    ScaffoldError,
    SessionNotFoundError,
    SessionNotReadyError,
    generate_scaffold,
)
from gecko_core.orchestration.scaffold.models import ScaffoldDocs

# A canned debate transcript shaped after the live_runs `good-cap-table-diff`
# ship case. Real-shaped — names a specific ICP, real comparables, concrete
# stack. The synthesizer never sees this directly in tests (we mock the LLM)
# but the prompt formatter DOES walk the structure, so the shape matters.
_CANNED_TRANSCRIPT: dict[str, Any] = {
    "turns": [
        {
            "agent": "analyst",
            "content": (
                "- Reachable TAM: ~22,000 US-incorporated startups raising "
                "pre-seed/seed in 2026; Carta has ~40k tracked cap tables.\n"
                "- ICP: founders evaluating SAFE term sheets at pre-seed who "
                "don't yet have a Carta seat.\n"
                "- Pave compensation data API names equity ranges by stage.\n"
                "- Counterfactual: founders eyeball SAFE diffs in Notion today."
            ),
            "tokens_in": 1200,
            "tokens_out": 220,
            "ts": 1.0,
            "seq": 2,
        },
        {
            "agent": "critic",
            "content": (
                "- Carta itself ships a diff feature for paid seats — wedge "
                "is the unpaid pre-Carta segment.\n"
                "- Risk: Reg D / 409A framing; stay non-advisory.\n"
                "Change my mind by: a named pre-seed founder who pays $49/mo today."
            ),
            "tokens_in": 800,
            "tokens_out": 180,
            "ts": 2.0,
            "seq": 4,
        },
        {
            "agent": "architect",
            "content": (
                "- Stack: Next 16 + Supabase Postgres + Stripe Checkout + "
                "Pave compensation API.\n"
                "- Watch the Pave API rate limit (60 req/min)."
            ),
            "tokens_in": 600,
            "tokens_out": 140,
            "ts": 3.0,
            "seq": 6,
        },
        {
            "agent": "scoper",
            "content": (
                "V1 (4 days):\n"
                "- Upload two SAFEs, render side-by-side diff.\n"
                "- Flag MFN, valuation cap, discount deltas.\n"
                "- Stripe paywall at $49/mo.\n"
                "Acceptance:\n"
                "- [ ] User uploads 2 SAFEs and sees a diff in <5s.\n"
                "- [ ] MFN deltas highlighted in red.\n"
                "V1_FEASIBLE_IN_4_DAYS: yes"
            ),
            "tokens_in": 700,
            "tokens_out": 240,
            "ts": 4.0,
            "seq": 8,
        },
        {
            "agent": "judge",
            "content": (
                "TAM: 6\nWEDGE: 8\nV1_FEASIBILITY: 8\n\n"
                "Verdict: SHIP V1 to founders evaluating SAFE term sheets at pre-seed."
            ),
            "tokens_in": 900,
            "tokens_out": 90,
            "ts": 5.0,
            "seq": 10,
        },
    ],
    "total_tokens_in": 4200,
    "total_tokens_out": 870,
    "budget_halt_reason": None,
}

_CANNED_SUMMARY = (
    "TAM: 6\nWEDGE: 8\nV1_FEASIBILITY: 8\n\n"
    "Verdict: SHIP V1 to founders evaluating SAFE term sheets at pre-seed."
)


class _StubSessionRecord:
    """Mimics SessionRecord.idea + ergonomic for asyncio mocks."""

    def __init__(self, idea: str) -> None:
        self.idea = idea


class _StubStore:
    """In-memory stand-in for SessionStore.

    Implements only the four methods generate_scaffold touches. Each call
    is recorded so tests can assert orchestration order without
    inspecting Supabase.
    """

    def __init__(
        self,
        *,
        record: _StubSessionRecord | None,
        result: dict[str, Any] | None,
    ) -> None:
        self._record = record
        self._result = result
        self.cost_calls: list[tuple[UUID, str, float]] = []

    async def get(self, session_id: UUID) -> _StubSessionRecord | None:
        return self._record

    async def get_result(self, session_id: UUID) -> dict[str, Any] | None:
        return self._result

    async def add_cost(self, session_id: UUID, kind: str, amount_usd: float) -> None:
        self.cost_calls.append((session_id, kind, amount_usd))


class _FakeUsage:
    def __init__(self, prompt_tokens: int, completion_tokens: int) -> None:
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class _FakeChoice:
    def __init__(self, content: str) -> None:
        # OpenAI response shape: choices[0].message.content
        class _Msg:
            def __init__(self, c: str) -> None:
                self.content = c

        self.message = _Msg(content)


class _FakeResponse:
    def __init__(self, content: str, usage: _FakeUsage | None) -> None:
        self.choices = [_FakeChoice(content)]
        self.usage = usage
        self.model = "gpt-4o"


class _FakeRawResponse:
    def __init__(self, content: str, usage: _FakeUsage | None) -> None:
        self._resp = _FakeResponse(content, usage)
        self.headers: dict[str, str] = {}

    def parse(self) -> _FakeResponse:
        return self._resp


class _FakeRawCompletions:
    def __init__(self, content: str, usage: _FakeUsage | None) -> None:
        self._content = content
        self._usage = usage
        self.last_kwargs: dict[str, Any] | None = None

    async def create(self, **kwargs: Any) -> _FakeRawResponse:
        self.last_kwargs = kwargs
        return _FakeRawResponse(self._content, self._usage)


class _FakeCompletions:
    def __init__(self, content: str, usage: _FakeUsage | None) -> None:
        self.with_raw_response = _FakeRawCompletions(content, usage)


class _FakeChat:
    def __init__(self, content: str, usage: _FakeUsage | None) -> None:
        self.completions = _FakeCompletions(content, usage)


class _FakeOpenAI:
    def __init__(self, content: str, usage: _FakeUsage | None) -> None:
        self.chat = _FakeChat(content, usage)


def _canned_docs_payload() -> dict[str, str]:
    """Synthesizer JSON output. Includes named entities so the file content
    test can assert the wedge-named ICP and Pave API actually appear."""
    docs = ScaffoldDocs(
        prd_md=(
            "# Product Requirements: Cap Table Diff\n\n"
            "## Problem statement\nFounders evaluating SAFE term sheets...\n\n"
            "## ICP\nfounders evaluating SAFE term sheets at pre-seed\n"
        ),
        business_plan_md=(
            "# Business Plan: Cap Table Diff\n\n"
            "## TAM Math\n~22,000 US-incorporated startups...\n\n"
            "## Comparables\n- Carta — growing\n"
        ),
        building_md=(
            "# Build Plan: Cap Table Diff\n\n"
            "## Stack recommendation\nNext 16 + Supabase + Stripe + Pave compensation API\n\n"
            "## Gotchas\n- Pave API rate limit (60 req/min)\n"
        ),
    )
    return docs.model_dump()


@pytest.fixture
def session_uuid() -> UUID:
    return uuid4()


def _make_result(transcript: dict[str, Any] | None, summary: str | None) -> dict[str, Any]:
    return {
        "transcript": transcript,
        "pro_session_summary": summary,
        "sources": [
            {"url": "https://carta.com/blog/safe-diff", "type": "web"},
            {"url": "https://www.pave.com/data", "type": "web"},
        ],
    }


async def test_generate_scaffold_writes_three_files_and_returns_paths(
    tmp_path: Path,
    session_uuid: UUID,
) -> None:
    store = _StubStore(
        record=_StubSessionRecord("Carta-aware cap table diff tool"),
        result=_make_result(_CANNED_TRANSCRIPT, _CANNED_SUMMARY),
    )
    fake = _FakeOpenAI(json.dumps(_canned_docs_payload()), _FakeUsage(3500, 2400))

    result = await generate_scaffold(
        session_uuid,
        tmp_path,
        store=store,  # type: ignore[arg-type]
        openai_client=fake,  # type: ignore[arg-type]
    )

    expected_dir = tmp_path / ".gecko" / "scaffolds" / str(session_uuid)
    assert result.session_id == session_uuid
    assert len(result.paths) == 3
    assert [p.name for p in result.paths] == ["PRD.md", "business-plan.md", "BUILDING.md"]
    for p in result.paths:
        assert p.parent == expected_dir.resolve()
        assert p.is_file()
        assert p.stat().st_size > 0

    assert result.tokens_used == 3500 + 2400
    assert result.cost_usd > 0  # gpt-4o token estimate seeded the value
    # add_cost is best-effort but should be called once.
    assert len(store.cost_calls) == 1
    assert store.cost_calls[0][1] == "llm"


async def test_generate_scaffold_passes_named_entities_in_prompt(
    tmp_path: Path,
    session_uuid: UUID,
) -> None:
    """The synthesizer must SEE the named ICP / Pave API in its user prompt
    so the JSON output isn't generic. We assert on the kwargs passed to
    chat.completions.with_raw_response.create."""
    store = _StubStore(
        record=_StubSessionRecord("Carta-aware cap table diff tool"),
        result=_make_result(_CANNED_TRANSCRIPT, _CANNED_SUMMARY),
    )
    fake = _FakeOpenAI(json.dumps(_canned_docs_payload()), _FakeUsage(100, 100))

    await generate_scaffold(
        session_uuid,
        tmp_path,
        store=store,  # type: ignore[arg-type]
        openai_client=fake,  # type: ignore[arg-type]
    )

    last = fake.chat.completions.with_raw_response.last_kwargs
    assert last is not None
    assert last["model"] == "gpt-4o"
    assert last["temperature"] == 0.2
    assert last["response_format"] == {"type": "json_object"}
    user_msg = next(m for m in last["messages"] if m["role"] == "user")
    user_text = user_msg["content"]
    # Named entities from the canned transcript must appear in the prompt.
    assert "founders evaluating SAFE term sheets at pre-seed" in user_text
    assert "Pave compensation API" in user_text
    assert "Verdict: SHIP" in user_text


async def test_generate_scaffold_accepts_legacy_kill_verdict(
    tmp_path: Path,
    session_uuid: UUID,
) -> None:
    """S17-TONE-01: legacy KILL transcripts are scaffold-eligible.

    The old behavior raised ``KillVerdictError``; the new vocabulary maps
    KILL → PIVOT and scaffolds documents that describe the redirected idea.
    The synthesizer prompt is responsible for not echoing KILL in its output.
    """
    kill_summary = "Verdict: KILL — saturated category with no named ICP."
    kill_transcript = {
        **_CANNED_TRANSCRIPT,
        "turns": [
            *_CANNED_TRANSCRIPT["turns"][:-1],
            {**_CANNED_TRANSCRIPT["turns"][-1], "content": kill_summary},
        ],
    }
    store = _StubStore(
        record=_StubSessionRecord("yet another todo app"),
        result=_make_result(kill_transcript, kill_summary),
    )
    canned_json = json.dumps(
        {
            "prd_md": "# PRD: Pivoted idea\n\n## Problem statement\nPivot redirect.",
            "business_plan_md": "# Business plan\n\n## TAM\n(none named)",
            "building_md": "# Build plan\n\n## Stack\nNext.js",
        }
    )
    fake = _FakeOpenAI(canned_json, _FakeUsage(10, 10))

    result = await generate_scaffold(
        session_uuid,
        tmp_path,
        store=store,  # type: ignore[arg-type]
        openai_client=fake,  # type: ignore[arg-type]
    )

    assert result.session_id == session_uuid
    assert (tmp_path / ".gecko").exists()


async def test_generate_scaffold_session_not_found(
    tmp_path: Path,
    session_uuid: UUID,
) -> None:
    store = _StubStore(record=None, result=None)
    fake = _FakeOpenAI("", None)

    with pytest.raises(SessionNotFoundError):
        await generate_scaffold(
            session_uuid,
            tmp_path,
            store=store,  # type: ignore[arg-type]
            openai_client=fake,  # type: ignore[arg-type]
        )


async def test_generate_scaffold_session_not_ready(
    tmp_path: Path,
    session_uuid: UUID,
) -> None:
    store = _StubStore(
        record=_StubSessionRecord("idea"),
        result=None,
    )
    fake = _FakeOpenAI("", None)

    with pytest.raises(SessionNotReadyError):
        await generate_scaffold(
            session_uuid,
            tmp_path,
            store=store,  # type: ignore[arg-type]
            openai_client=fake,  # type: ignore[arg-type]
        )


async def test_generate_scaffold_rejects_malformed_json(
    tmp_path: Path,
    session_uuid: UUID,
) -> None:
    """Synthesizer returns JSON missing required keys → ScaffoldError."""
    store = _StubStore(
        record=_StubSessionRecord("idea"),
        result=_make_result(_CANNED_TRANSCRIPT, _CANNED_SUMMARY),
    )
    # Missing prd_md key — ScaffoldDocs validation should fail.
    fake = _FakeOpenAI(
        json.dumps({"business_plan_md": "x", "building_md": "y"}), _FakeUsage(10, 10)
    )

    with pytest.raises(ScaffoldError):
        await generate_scaffold(
            session_uuid,
            tmp_path,
            store=store,  # type: ignore[arg-type]
            openai_client=fake,  # type: ignore[arg-type]
        )


async def test_generate_scaffold_rejects_invalid_json_text(
    tmp_path: Path,
    session_uuid: UUID,
) -> None:
    store = _StubStore(
        record=_StubSessionRecord("idea"),
        result=_make_result(_CANNED_TRANSCRIPT, _CANNED_SUMMARY),
    )
    fake = _FakeOpenAI("not json at all", _FakeUsage(10, 10))

    with pytest.raises(ScaffoldError):
        await generate_scaffold(
            session_uuid,
            tmp_path,
            store=store,  # type: ignore[arg-type]
            openai_client=fake,  # type: ignore[arg-type]
        )


async def test_file_contents_reference_named_entities_when_synthesizer_obeys(
    tmp_path: Path,
    session_uuid: UUID,
) -> None:
    """Smoke: when the synthesizer returns docs that mention the named ICP /
    stack / API (as a well-behaved gpt-4o would), the written files
    contain those strings verbatim. Guards against accidental rewriting
    of payload content during the disk write."""
    store = _StubStore(
        record=_StubSessionRecord("Carta-aware cap table diff tool"),
        result=_make_result(_CANNED_TRANSCRIPT, _CANNED_SUMMARY),
    )
    fake = _FakeOpenAI(json.dumps(_canned_docs_payload()), _FakeUsage(100, 100))

    result = await generate_scaffold(
        session_uuid,
        tmp_path,
        store=store,  # type: ignore[arg-type]
        openai_client=fake,  # type: ignore[arg-type]
    )

    prd_text = result.paths[0].read_text(encoding="utf-8")
    plan_text = result.paths[1].read_text(encoding="utf-8")
    build_text = result.paths[2].read_text(encoding="utf-8")
    assert "founders evaluating SAFE term sheets at pre-seed" in prd_text
    assert "Pave" in build_text
    assert "TAM Math" in plan_text
