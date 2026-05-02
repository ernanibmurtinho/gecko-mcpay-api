"""S14-PULSE-01 — pulse engine unit tests.

Covers:
  * `run_pulse_v14` creates a NEW session row with `phase="during_build"`
    and `parent_session_id` set to the input session_id.
  * The advisor panel runs against the parent session's idea (the new
    pulse session row inherits the parent's idea + tier).
  * Verdict + gap_classification synthesized from panel consensus
    (build_count >= 4 promotes to BUILD).
  * `summary_bullets` is populated from the panel's distinctive
    closing lines (max 3).

Stub-friendly: the test uses a fake SessionStore that captures
`create()` calls and returns canned `SessionRecord`s. The advisor
`generate_panel` is monkeypatched to return a canned panel — we don't
run real LLM calls in unit tests.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from gecko_core.models import Verdict
from gecko_core.orchestration.advisor import models as advisor_models
from gecko_core.orchestration.advisor import pulse_engine
from gecko_core.orchestration.advisor.models import (
    PANEL_VOICE_ORDER,
    AdvisorPanel,
    AdvisorVoice,
)
from gecko_core.routing.catalog import Tier
from gecko_core.sessions.store import SessionRecord


class _FakeStore:
    """Captures `create()` calls + returns a canned parent SessionRecord."""

    def __init__(self, parent_record: SessionRecord) -> None:
        self._parent = parent_record
        self.created: list[dict[str, Any]] = []
        self.new_session_id = uuid4()
        # S17-PERSIST-01: capture set_result + update_status calls so the
        # pulse-persistence regression test can assert the workflow wrote
        # back to the new pulse session row.
        self.results: dict[UUID, dict[str, Any]] = {}
        self.statuses: dict[UUID, str] = {}

    async def get(self, session_id: UUID) -> SessionRecord | None:
        if session_id == self._parent.id:
            return self._parent
        return None

    async def create(
        self,
        idea: str,
        tier: Tier,
        payment_mode: str = "stub",
        *,
        phase: str = "pre_product",
        parent_session_id: UUID | None = None,
    ) -> UUID:
        self.created.append(
            {
                "idea": idea,
                "tier": tier,
                "payment_mode": payment_mode,
                "phase": phase,
                "parent_session_id": parent_session_id,
            }
        )
        return self.new_session_id

    async def set_result(self, session_id: UUID, result: dict[str, Any]) -> None:
        self.results[session_id] = result

    async def update_status(self, session_id: UUID, status: str) -> None:
        self.statuses[session_id] = status


def _build_panel_with_consensus(sid: UUID, *, build_voices: int) -> AdvisorPanel:
    """Build a canned panel where ``build_voices`` voices say "ship it"."""
    voices: list[AdvisorVoice] = []
    for i, role in enumerate(PANEL_VOICE_ORDER):
        text = "ship it now" if i < build_voices else "wait and observe"
        voices.append(
            AdvisorVoice(
                role=role,
                model_used=f"stub/{role.value}",
                output_md=f"## {role.value}",
                closing_line=f"{role.value}: {text}",
                tokens_in=10,
                tokens_out=20,
                cost_usd=0.0,
            )
        )
    return AdvisorPanel(
        session_id=str(sid),
        voices=voices,
        total_cost_usd=0.0,
        generated_at=datetime.now().astimezone(),
    )


def _parent_record(sid: UUID) -> SessionRecord:
    return SessionRecord(
        id=sid,
        idea="An AI agent marketplace for legal contract review",
        tier="basic",
        status="complete",
        payment_mode="stub",
        phase="pre_product",
        parent_session_id=None,
        created_at=datetime.now().astimezone(),
    )


@pytest.mark.asyncio
async def test_run_pulse_v14_creates_during_build_session_with_parent_fk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pulse run inserts a new session row with phase + parent_session_id."""
    parent_sid = uuid4()
    store = _FakeStore(_parent_record(parent_sid))

    async def _fake_panel(session_id: UUID | str, **_: Any) -> AdvisorPanel:
        # Caller passes the PARENT session_id to generate_panel — the
        # context the panel reads belongs to the parent.
        assert UUID(str(session_id)) == parent_sid
        return _build_panel_with_consensus(parent_sid, build_voices=4)

    # Patch generate_panel inside the advisor package — pulse_engine
    # imports it lazily via `from gecko_core.orchestration.advisor import generate_panel`.
    import gecko_core.orchestration.advisor as advisor_pkg

    monkeypatch.setattr(advisor_pkg, "generate_panel", _fake_panel)

    result = await pulse_engine.run_pulse_v14(
        parent_session_id=parent_sid,
        tier_preset=Tier.balanced,
        store=store,  # type: ignore[arg-type]
        skip_citations=True,
    )

    # A new session row was created with the right phase + parent FK.
    assert len(store.created) == 1
    create_call = store.created[0]
    assert create_call["phase"] == "during_build"
    assert create_call["parent_session_id"] == parent_sid
    assert create_call["idea"] == "An AI agent marketplace for legal contract review"

    # PulseResult points at both sessions.
    assert isinstance(result, advisor_models.PulseResult)
    assert UUID(result.parent_session_id) == parent_sid
    assert UUID(result.pulse_session_id) == store.new_session_id


@pytest.mark.asyncio
async def test_run_pulse_v14_strong_build_consensus_promotes_to_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With 4/5 voices saying ship, derive_verdict promotes to GO.

    S17-TONE-01: GO replaced BUILD as the single-token verdict for
    "greenlight" verdicts. Same semantics, softer founder-facing copy.

    S17-VERDICT-01: this path covers consensus-promotion, not the
    evidence-strength floor — bypass `is_low_grounding` so an empty
    citations list (skip_citations=True) doesn't floor the verdict to
    REFINE before the consensus rule runs.
    """
    parent_sid = uuid4()
    store = _FakeStore(_parent_record(parent_sid))

    async def _fake_panel(session_id: UUID | str, **_: Any) -> AdvisorPanel:
        return _build_panel_with_consensus(parent_sid, build_voices=4)

    import gecko_core.orchestration.advisor as advisor_pkg

    monkeypatch.setattr(advisor_pkg, "generate_panel", _fake_panel)
    # Bypass the S17-VERDICT-01 evidence floor for this consensus-path
    # test by patching `is_low_grounding` in BOTH callers — the floor
    # check inside `derive_verdict` (models module) and the local
    # `low_grounding` flag inside `run_pulse_v14` (pulse_engine module).
    from gecko_core import models as _models_mod

    monkeypatch.setattr(pulse_engine, "is_low_grounding", lambda _c: False)
    monkeypatch.setattr(_models_mod, "is_low_grounding", lambda _c: False)

    result = await pulse_engine.run_pulse_v14(
        parent_session_id=parent_sid,
        store=store,  # type: ignore[arg-type]
        skip_citations=True,
    )

    assert result.verdict == Verdict.GO
    assert result.gap_classification == "Partial:pricing"


@pytest.mark.asyncio
async def test_run_pulse_v14_weak_consensus_falls_back_to_refine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With <4 voices saying ship, the verdict stays at REFINE."""
    parent_sid = uuid4()
    store = _FakeStore(_parent_record(parent_sid))

    async def _fake_panel(session_id: UUID | str, **_: Any) -> AdvisorPanel:
        return _build_panel_with_consensus(parent_sid, build_voices=2)

    import gecko_core.orchestration.advisor as advisor_pkg

    monkeypatch.setattr(advisor_pkg, "generate_panel", _fake_panel)

    result = await pulse_engine.run_pulse_v14(
        parent_session_id=parent_sid,
        store=store,  # type: ignore[arg-type]
        skip_citations=True,
    )

    assert result.verdict == Verdict.REFINE
    assert result.gap_classification == "Partial:segment"  # fallback


@pytest.mark.asyncio
async def test_run_pulse_v14_summary_bullets_capped_at_three(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 3-bullet summary uses distinct voices and caps at 3 entries."""
    parent_sid = uuid4()
    store = _FakeStore(_parent_record(parent_sid))

    async def _fake_panel(session_id: UUID | str, **_: Any) -> AdvisorPanel:
        return _build_panel_with_consensus(parent_sid, build_voices=3)

    import gecko_core.orchestration.advisor as advisor_pkg

    monkeypatch.setattr(advisor_pkg, "generate_panel", _fake_panel)

    result = await pulse_engine.run_pulse_v14(
        parent_session_id=parent_sid,
        store=store,  # type: ignore[arg-type]
        skip_citations=True,
    )

    assert len(result.summary_bullets) == 3
    # Each bullet starts with a voice prefix.
    assert all(":" in b for b in result.summary_bullets)


@pytest.mark.asyncio
async def test_run_pulse_v14_missing_parent_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-existent parent session raises AdvisorSessionNotFoundError."""
    parent_sid = uuid4()

    class _EmptyStore:
        async def get(self, _: UUID) -> SessionRecord | None:
            return None

        async def create(self, *args: Any, **kwargs: Any) -> UUID:
            raise AssertionError("create() should not be called when parent is missing")

    with pytest.raises(advisor_models.AdvisorSessionNotFoundError):
        await pulse_engine.run_pulse_v14(
            parent_session_id=parent_sid,
            store=_EmptyStore(),  # type: ignore[arg-type]
            skip_citations=True,
        )


@pytest.mark.asyncio
async def test_run_pulse_v14_persists_result_and_marks_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S17-PERSIST-01 regression — pulse must write result_json + status='complete'.

    Bug: pulse_engine.run_pulse_v14 returned a PulseResult to the caller
    but never persisted it on the new pulse_session_id row. As a result,
    `bb pulse` runs left status='pending' / result_json=NULL in Supabase
    and downstream readers (advisor context lookup, /sessions/{id}/result)
    failed with "not ready". This test pins the persistence contract.
    """
    parent_sid = uuid4()
    store = _FakeStore(_parent_record(parent_sid))

    async def _fake_panel(session_id: UUID | str, **_: Any) -> AdvisorPanel:
        return _build_panel_with_consensus(parent_sid, build_voices=3)

    import gecko_core.orchestration.advisor as advisor_pkg

    monkeypatch.setattr(advisor_pkg, "generate_panel", _fake_panel)

    result = await pulse_engine.run_pulse_v14(
        parent_session_id=parent_sid,
        store=store,  # type: ignore[arg-type]
        skip_citations=True,
    )

    pulse_sid = store.new_session_id
    # Result JSON written on the new pulse session row.
    assert pulse_sid in store.results
    persisted = store.results[pulse_sid]
    assert persisted["pulse_session_id"] == str(pulse_sid)
    assert persisted["parent_session_id"] == str(parent_sid)
    assert persisted["verdict"] == result.verdict.value
    # Status flipped to 'complete'.
    assert store.statuses.get(pulse_sid) == "complete"
