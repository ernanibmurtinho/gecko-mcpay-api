"""Tests for contradiction detection (S5-MEM-06)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from gecko_core.memory import contradictions
from gecko_core.memory.contradictions import check
from gecko_core.memory.models import MemoryEntry, MemoryEntryType, MemoryScope


def _entry(
    *,
    scope: MemoryScope,
    entry_type: MemoryEntryType,
    value: dict[str, Any],
) -> MemoryEntry:
    return MemoryEntry(
        id=uuid4(),
        scope=scope,
        entry_type=entry_type,
        key=None,
        value=value,
        created_at=datetime.now(UTC),
    )


class _StubStore:
    def __init__(self, candidates: list[tuple[MemoryEntry, float]]) -> None:
        self._candidates = candidates

    async def search(
        self,
        *,
        scope: MemoryScope,
        query_embedding: list[float],
        top_k: int = 10,
        similarity_threshold: float = 0.78,
    ) -> list[tuple[MemoryEntry, float]]:
        return [
            (e, s)
            for e, s in self._candidates
            if e.scope.type == scope.type and e.scope.id == scope.id and s >= similarity_threshold
        ]


@pytest.fixture(autouse=True)
def _stub_embedder(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake(text: str) -> list[float]:
        return [0.001] * 1536

    monkeypatch.setattr(contradictions, "embed_text", _fake)


async def test_two_ship_verdicts_no_contradiction() -> None:
    scope = MemoryScope(type="project", id="A")
    prior = _entry(
        scope=scope,
        entry_type=MemoryEntryType.verdict_received,
        value={"idea": "AI estate planner", "verdict": "ship"},
    )
    incoming = _entry(
        scope=scope,
        entry_type=MemoryEntryType.verdict_received,
        value={"idea": "AI estate planner v2", "verdict": "ship"},
    )
    store = _StubStore([(prior, 0.92)])
    result = await check(scope, incoming, store=store)  # type: ignore[arg-type]
    assert result is None


async def test_ship_then_kill_flags_contradiction() -> None:
    scope = MemoryScope(type="project", id="A")
    prior = _entry(
        scope=scope,
        entry_type=MemoryEntryType.verdict_received,
        value={"idea": "AI estate planner", "verdict": "ship"},
    )
    incoming = _entry(
        scope=scope,
        entry_type=MemoryEntryType.verdict_received,
        value={"idea": "AI tax preparer", "verdict": "kill"},
    )
    store = _StubStore([(prior, 0.81)])
    result = await check(scope, incoming, store=store)  # type: ignore[arg-type]
    assert result is not None
    assert result.contradicting_entry_id == prior.id
    assert result.similarity == pytest.approx(0.81)
    assert "SHIP" in result.reason and "KILL" in result.reason


async def test_scope_isolation_no_cross_scope_match() -> None:
    scope_a = MemoryScope(type="project", id="A")
    scope_b = MemoryScope(type="project", id="B")
    prior_other = _entry(
        scope=scope_b,
        entry_type=MemoryEntryType.verdict_received,
        value={"idea": "x", "verdict": "ship"},
    )
    incoming = _entry(
        scope=scope_a,
        entry_type=MemoryEntryType.verdict_received,
        value={"idea": "x", "verdict": "kill"},
    )
    store = _StubStore([(prior_other, 0.95)])
    result = await check(scope_a, incoming, store=store)  # type: ignore[arg-type]
    assert result is None


async def test_advisor_voiced_uses_llm_judge(monkeypatch: pytest.MonkeyPatch) -> None:
    scope = MemoryScope(type="project", id="A")
    prior = _entry(
        scope=scope,
        entry_type=MemoryEntryType.advisor_voiced,
        value={"role": "ceo", "closing_line": "lock the LOI this sprint"},
    )
    incoming = _entry(
        scope=scope,
        entry_type=MemoryEntryType.advisor_voiced,
        value={"role": "ceo", "closing_line": "abandon the LOI; pivot to enterprise"},
    )

    async def _judge(
        *, client: Any, incoming_text: str, prior_text: str, model: str = "gpt-4o-mini"
    ) -> tuple[bool, str]:
        return True, "priorities point in opposite directions"

    monkeypatch.setattr(contradictions, "_llm_judges_conflict", _judge)

    store = _StubStore([(prior, 0.83)])
    # openai_client=object() so we never instantiate the real one.
    result = await check(scope, incoming, store=store, openai_client=object())  # type: ignore[arg-type]
    assert result is not None
    assert result.similarity == pytest.approx(0.83)
    assert "opposite" in result.reason
