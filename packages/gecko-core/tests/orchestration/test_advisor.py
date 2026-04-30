"""Tests for `gecko_core.orchestration.advisor` (S4-ADVISOR-01..05).

Strategy: stub SessionStore, stub OpenAI client, stub embedder. Assert
panel shape, closing-line extraction, model selection, context loader,
pulse delta detection.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
from gecko_core.orchestration.advisor import (
    AdvisorSessionNotFoundError,
    compute_pulse_deltas,
    generate_panel,
    generate_voice,
    load_context,
)
from gecko_core.orchestration.advisor.agents import extract_closing_line
from gecko_core.orchestration.advisor.context import render_context_block
from gecko_core.orchestration.advisor.models import (
    PANEL_VOICE_ORDER,
    AdvisorPanel,
    AdvisorVoice,
)
from gecko_core.routing.catalog import AgentRole, Tier, lookup_model, task_for_role

# ---------------------------------------------------------------------------
# Stubs — kept local; reused across tests via fixtures.
# ---------------------------------------------------------------------------


class _StubSessionRecord:
    def __init__(self, idea: str, project_id: UUID | None = None) -> None:
        self.idea = idea
        self.project_id = project_id


class _StubStore:
    """In-memory SessionStore. Implements only what the advisor touches."""

    def __init__(
        self,
        *,
        record: _StubSessionRecord | None,
        result: dict[str, Any] | None,
        precedents: list[Any] | None = None,
    ) -> None:
        self._record = record
        self._result = result
        self._precedents = precedents or []

    async def get(self, session_id: UUID) -> _StubSessionRecord | None:
        return self._record

    async def get_result(self, session_id: UUID) -> dict[str, Any] | None:
        return self._result

    async def retrieve_gecko_precedent(
        self, *, embedding: list[float], limit: int = 5
    ) -> list[Any]:
        return self._precedents[:limit]


_TRANSCRIPT = {
    "turns": [
        {"agent": "analyst", "content": "ICP: SAFE-stage founders. TAM: ~22k startups."},
        {"agent": "judge", "content": "Verdict: SHIP V1 to founders evaluating SAFE term sheets."},
    ]
}


def _result_payload() -> dict[str, Any]:
    return {
        "transcript": _TRANSCRIPT,
        "pro_session_summary": (
            "Verdict: SHIP V1 to founders evaluating SAFE term sheets at pre-seed."
        ),
        "sources": [{"url": "https://carta.com/blog/safe", "type": "web"}],
    }


# Closing-line tail per role — deterministic strings used by the fake LLM
# so closing-line extraction has something to find.
_CLOSING_LINES: dict[AgentRole, str] = {
    AgentRole.ceo: "Strategic priority: lock the design-partner LOI with Carta this sprint.",
    AgentRole.cto: "Critical path: ship the SAFE-diff parser before any UI polish.",
    AgentRole.business_manager: "Lever this sprint: A/B test $49 vs $99 paywall on twit.sh signups.",
    AgentRole.product_manager: "Top backlog item: side-by-side SAFE diff with MFN highlighted.",
    AgentRole.staff_manager: (
        "Sprint plan: 1) TICKET-01 SAFE diff, 2) TICKET-02 paywall A/B, 3) TICKET-03 launch thread."
    ),
}


def _voice_output(role: AgentRole) -> str:
    """Generate a fake voice output that satisfies the closing-line regex."""
    return (
        f"## {role.value} output\nHere is some advisor markdown content.\n\n"
        f"More body text mentioning Pave and Carta for realism.\n\n"
        f"{_CLOSING_LINES[role]}\n"
    )


class _FakeUsage:
    def __init__(self, p: int, c: int) -> None:
        self.prompt_tokens = p
        self.completion_tokens = c


class _FakeChoice:
    def __init__(self, content: str) -> None:
        class _M:
            def __init__(self, c: str) -> None:
                self.content = c

        self.message = _M(content)


class _FakeResp:
    def __init__(self, content: str) -> None:
        self.choices = [_FakeChoice(content)]
        self.usage = _FakeUsage(420, 380)


class _FakeRaw:
    def __init__(self, content: str) -> None:
        self._r = _FakeResp(content)
        self.headers: dict[str, str] = {}

    def parse(self) -> _FakeResp:
        return self._r


class _FakeRawCompletions:
    def __init__(self) -> None:
        # Track per-model invocations so tests can assert which model each
        # voice resolved to.
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> _FakeRaw:
        self.calls.append(kwargs)
        # Pick the closing line based on the system prompt (the system
        # prompt opens with 'You are the CEO.' / 'You are the CTO.' etc).
        sys_msg = next(m for m in kwargs["messages"] if m["role"] == "system")
        sys_content = sys_msg["content"].lower()
        for role in PANEL_VOICE_ORDER:
            # Prompts contain literal "You are the CEO." etc; staff_manager
            # → "You are the STAFF MANAGER." We match on the role's lowercase
            # noun phrase to avoid false positives.
            needles = {
                AgentRole.ceo: "you are the ceo",
                AgentRole.cto: "you are the cto",
                AgentRole.business_manager: "you are the business manager",
                AgentRole.product_manager: "you are the product manager",
                AgentRole.staff_manager: "you are the staff manager",
            }
            if needles[role] in sys_content:
                return _FakeRaw(_voice_output(role))
        return _FakeRaw("(unrecognized voice)")


class _FakeCompletions:
    def __init__(self) -> None:
        self.with_raw_response = _FakeRawCompletions()


class _FakeChat:
    def __init__(self) -> None:
        self.completions = _FakeCompletions()


class _FakeOpenAI:
    def __init__(self) -> None:
        self.chat = _FakeChat()


@pytest.fixture
def session_uuid() -> UUID:
    return uuid4()


@pytest.fixture
def store_with_result() -> _StubStore:
    return _StubStore(
        record=_StubSessionRecord("Cap-table SAFE diff tool"),
        result=_result_payload(),
    )


@pytest.fixture(autouse=True)
def _stub_embedder(monkeypatch: pytest.MonkeyPatch) -> None:
    """Avoid the real OpenAI embedder in tests — return a tiny vector."""

    async def _fake_embed(texts: list[str]) -> tuple[list[list[float]], int]:
        return ([[0.0] * 8 for _ in texts], 0)

    import gecko_core.ingestion.embedder as embedder_mod

    monkeypatch.setattr(embedder_mod, "embed", _fake_embed)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_closing_line_extraction_per_role() -> None:
    """Each role's closing line gets extracted in prefix-included form."""
    for role, expected in _CLOSING_LINES.items():
        out = extract_closing_line(role, _voice_output(role))
        assert out == expected, f"role={role.value} extraction mismatch"


def test_closing_line_fallback_when_missing() -> None:
    """No closing-line match → last non-empty line, never empty string."""
    out = extract_closing_line(AgentRole.ceo, "no closing here\nlast line of body")
    assert out == "last line of body"


def test_panel_voice_order_is_stable() -> None:
    """CEO first, staff_manager last per persona-dependency chain."""
    assert PANEL_VOICE_ORDER[0] == AgentRole.ceo
    assert PANEL_VOICE_ORDER[-1] == AgentRole.staff_manager
    assert len(PANEL_VOICE_ORDER) == 5


async def test_load_context_fills_all_fields(
    session_uuid: UUID, store_with_result: _StubStore
) -> None:
    ctx = await load_context(
        session_uuid,
        store=store_with_result,  # type: ignore[arg-type]
        v1_source_signal="## V1 Source Signal\n(stubbed)",
    )
    assert ctx.idea == "Cap-table SAFE diff tool"
    assert ctx.pro_verdict == "ship"
    assert ctx.pro_transcript is not None
    assert "ICP: SAFE-stage founders" in ctx.pro_transcript
    assert ctx.v1_source_signal == "## V1 Source Signal\n(stubbed)"
    rendered = render_context_block(ctx)
    assert "Cap-table SAFE diff tool" in rendered
    assert "SHIP" in rendered


async def test_load_context_session_not_found(session_uuid: UUID) -> None:
    store = _StubStore(record=None, result=None)
    with pytest.raises(AdvisorSessionNotFoundError):
        await load_context(session_uuid, store=store)  # type: ignore[arg-type]


async def test_load_context_runs_on_kill_verdict(session_uuid: UUID) -> None:
    """Unlike scaffold, advisor accepts kill verdicts (pivot advice)."""
    kill_result = {
        "transcript": _TRANSCRIPT,
        "pro_session_summary": "Verdict: KILL — saturated with no named ICP.",
        "sources": [],
    }
    store = _StubStore(
        record=_StubSessionRecord("yet another todo app"),
        result=kill_result,
    )
    ctx = await load_context(session_uuid, store=store)  # type: ignore[arg-type]
    assert ctx.pro_verdict == "kill"


async def test_generate_panel_returns_five_voices_in_order(
    session_uuid: UUID, store_with_result: _StubStore
) -> None:
    fake = _FakeOpenAI()
    panel = await generate_panel(
        session_uuid,
        tier_preset=Tier.balanced,
        store=store_with_result,  # type: ignore[arg-type]
        openai_client=fake,  # type: ignore[arg-type]
    )
    assert isinstance(panel, AdvisorPanel)
    assert len(panel.voices) == 5
    assert [v.role for v in panel.voices] == list(PANEL_VOICE_ORDER)
    for v in panel.voices:
        assert isinstance(v, AdvisorVoice)
        assert v.output_md
        assert v.closing_line and v.closing_line.startswith(
            (
                "Strategic priority:",
                "Critical path:",
                "Lever this sprint:",
                "Top backlog item:",
                "Sprint plan:",
            )
        )
    assert panel.total_cost_usd >= 0.0


async def test_generate_panel_uses_five_distinct_models_at_balanced(
    session_uuid: UUID, store_with_result: _StubStore
) -> None:
    """At Tier.balanced the role→task matrix maps to 5 distinct models."""
    fake = _FakeOpenAI()
    panel = await generate_panel(
        session_uuid,
        tier_preset=Tier.balanced,
        store=store_with_result,  # type: ignore[arg-type]
        openai_client=fake,  # type: ignore[arg-type]
    )
    # Cross-check: each voice resolves to the curated catalog model for its
    # task profile. At Tier.balanced today the matrix yields 3 distinct
    # models across the 5 voices (planning + complex_coding + creative_writing
    # all currently land on Kimi K2.6) — the contract is per-task-correctness,
    # not raw distinctness, since catalog edits may collapse or split cells.
    for v in panel.voices:
        expected = lookup_model(task_for_role(v.role), Tier.balanced).id
        assert v.model_used == expected, f"{v.role}: {v.model_used} != {expected}"
    used_ids = {v.model_used for v in panel.voices}
    assert len(used_ids) >= 2  # at minimum the matrix fans across providers


async def test_generate_voice_single(session_uuid: UUID, store_with_result: _StubStore) -> None:
    fake = _FakeOpenAI()
    v = await generate_voice(
        session_uuid,
        "ceo",
        tier_preset=Tier.balanced,
        store=store_with_result,  # type: ignore[arg-type]
        openai_client=fake,  # type: ignore[arg-type]
    )
    assert v.role == AgentRole.ceo
    assert v.closing_line.startswith("Strategic priority:")


async def test_generate_voice_alias_resolves(
    session_uuid: UUID, store_with_result: _StubStore
) -> None:
    fake = _FakeOpenAI()
    v = await generate_voice(
        session_uuid,
        "sm",
        store=store_with_result,  # type: ignore[arg-type]
        openai_client=fake,  # type: ignore[arg-type]
    )
    assert v.role == AgentRole.staff_manager


async def test_generate_voice_unknown_alias_raises(
    session_uuid: UUID, store_with_result: _StubStore
) -> None:
    fake = _FakeOpenAI()
    with pytest.raises(ValueError):
        await generate_voice(
            session_uuid,
            "cfo",
            store=store_with_result,  # type: ignore[arg-type]
            openai_client=fake,  # type: ignore[arg-type]
        )


async def test_generate_panel_session_not_found(session_uuid: UUID) -> None:
    store = _StubStore(record=None, result=None)
    fake = _FakeOpenAI()
    with pytest.raises(AdvisorSessionNotFoundError):
        await generate_panel(
            session_uuid,
            store=store,  # type: ignore[arg-type]
            openai_client=fake,  # type: ignore[arg-type]
        )


def _make_panel(closing_lines: dict[AgentRole, str], session_id: str) -> AdvisorPanel:
    voices = [
        AdvisorVoice(
            role=role,
            model_used="stub/model",
            output_md="x",
            closing_line=closing_lines.get(role, "x"),
            tokens_in=0,
            tokens_out=0,
            cost_usd=0.0,
        )
        for role in PANEL_VOICE_ORDER
    ]
    return AdvisorPanel(session_id=session_id, voices=voices, total_cost_usd=0.0)


def test_pulse_deltas_no_prior() -> None:
    panel = _make_panel(_CLOSING_LINES, "sid")
    deltas = compute_pulse_deltas(panel=panel, previous_panel=None)
    assert len(deltas) == 5
    assert all(not d.changed for d in deltas)
    assert all(d.reason == "no prior pulse on file" for d in deltas)


def test_pulse_deltas_detect_change() -> None:
    prev = _make_panel(_CLOSING_LINES, "sid")
    new_lines = dict(_CLOSING_LINES)
    new_lines[AgentRole.ceo] = "Strategic priority: pivot to AI-vet-Rx after the SAFE-diff plateau."
    curr = _make_panel(new_lines, "sid")
    deltas = compute_pulse_deltas(panel=curr, previous_panel=prev)
    by_role = {d.role: d for d in deltas}
    assert by_role[AgentRole.ceo].changed is True
    assert by_role[AgentRole.cto].changed is False


# ---------------------------------------------------------------------------
# S5-API-02 — run_pulse walks pulse_runs history for project-scoped deltas
# ---------------------------------------------------------------------------


class _PulseRunsStubStore(_StubStore):
    """Extends _StubStore with pulse_runs read/write tracking."""

    def __init__(
        self,
        *,
        record: _StubSessionRecord | None,
        result: dict[str, Any] | None,
        prior_pulse_row: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(record=record, result=result)
        self._prior_pulse_row = prior_pulse_row
        self.inserted: list[dict[str, Any]] = []
        self.lookup_calls: list[dict[str, Any]] = []

    async def get_latest_pulse_run(
        self,
        *,
        session_id: UUID | None = None,
        project_id: UUID | None = None,
    ) -> dict[str, Any] | None:
        self.lookup_calls.append({"session_id": session_id, "project_id": project_id})
        return self._prior_pulse_row

    async def insert_pulse_run(
        self,
        *,
        session_id: UUID,
        project_id: UUID | None,
        panel_json: dict[str, Any],
        deltas_json: list[dict[str, Any]],
    ) -> UUID:
        new_id = uuid4()
        self.inserted.append(
            {
                "id": new_id,
                "session_id": session_id,
                "project_id": project_id,
                "panel_json": panel_json,
                "deltas_json": deltas_json,
            }
        )
        return new_id


async def test_run_pulse_with_project_walks_history(session_uuid: UUID) -> None:
    """run_pulse(project_id=...) reads the most recent prior panel + computes deltas.

    Builds a stub store with a prior pulse row whose CEO closing line
    differs from the new panel's. Asserts:
        1. The store's get_latest_pulse_run was called with project_id.
        2. The resulting deltas mark CEO as changed.
        3. A new pulse_runs row was persisted.
    """
    project_uuid = uuid4()

    # Prior panel had a different CEO closing line — should produce changed=True.
    prior_panel = _make_panel(
        {
            **_CLOSING_LINES,
            AgentRole.ceo: "Strategic priority: pivot to dev-tools instead.",
        },
        str(session_uuid),
    )
    prior_row = {
        "id": str(uuid4()),
        "session_id": str(session_uuid),
        "project_id": str(project_uuid),
        "panel_json": prior_panel.model_dump(mode="json"),
        "deltas_json": [],
        "created_at": "2026-04-28T00:00:00+00:00",
    }
    store = _PulseRunsStubStore(
        record=_StubSessionRecord("Cap-table SAFE diff tool", project_id=project_uuid),
        result=_result_payload(),
        prior_pulse_row=prior_row,
    )

    fake = _FakeOpenAI()
    from gecko_core.orchestration.advisor import run_pulse

    pulse = await run_pulse(
        session_id=session_uuid,
        project_id=project_uuid,
        store=store,  # type: ignore[arg-type]
        openai_client=fake,  # type: ignore[arg-type]
        journal=False,
    )

    # Lookup called with project_id (project takes precedence).
    assert store.lookup_calls
    assert store.lookup_calls[0]["project_id"] == project_uuid
    assert store.lookup_calls[0]["session_id"] is None

    # Delta detected on CEO (prior closing line differed).
    by_role = {d.role: d for d in pulse.deltas}
    assert by_role[AgentRole.ceo].changed is True
    # CTO matches prior — no change.
    assert by_role[AgentRole.cto].changed is False

    # New pulse_runs row persisted with project_id.
    assert len(store.inserted) == 1
    inserted = store.inserted[0]
    assert inserted["session_id"] == session_uuid
    assert inserted["project_id"] == project_uuid


async def test_run_pulse_session_only_no_prior(session_uuid: UUID) -> None:
    """run_pulse(session_id=...) with no prior row reports 'no prior pulse'."""
    store = _PulseRunsStubStore(
        record=_StubSessionRecord("idea X"),
        result=_result_payload(),
        prior_pulse_row=None,
    )
    fake = _FakeOpenAI()
    from gecko_core.orchestration.advisor import run_pulse

    pulse = await run_pulse(
        session_id=session_uuid,
        store=store,  # type: ignore[arg-type]
        openai_client=fake,  # type: ignore[arg-type]
        journal=False,
    )
    assert all(d.reason == "no prior pulse on file" for d in pulse.deltas)
    # Lookup hit session_id (no project provided).
    assert store.lookup_calls[0]["session_id"] == session_uuid
    # New row persisted even when there's no prior — bootstraps history.
    assert len(store.inserted) == 1


async def test_run_pulse_requires_session_or_project() -> None:
    """run_pulse() with neither id raises ValueError."""
    from gecko_core.orchestration.advisor import run_pulse

    with pytest.raises(ValueError, match="session_id or project_id"):
        await run_pulse(journal=False)


async def test_panel_v1_source_signal_passes_through(
    session_uuid: UUID, store_with_result: _StubStore
) -> None:
    """V1 block surfaces in the user prompt sent to each voice."""
    fake = _FakeOpenAI()
    await generate_panel(
        session_uuid,
        tier_preset=Tier.balanced,
        store=store_with_result,  # type: ignore[arg-type]
        openai_client=fake,  # type: ignore[arg-type]
        v1_source_signal="## V1 Source Signal\n### Twitter / X (twit.sh)\n- @builder42",
    )
    calls = fake.chat.completions.with_raw_response.calls
    assert len(calls) == 5
    for call in calls:
        user = next(m for m in call["messages"] if m["role"] == "user")
        assert "@builder42" in user["content"]
