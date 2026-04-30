"""Tests for the auto-journal hooks (S5-MEM-04).

We test the four hook helpers directly against a fake `save()` so the
tests don't require a live Supabase or pull in the full workflow stack.
The hooks' job is small + uniform: when journaling is enabled, write a
typed entry shaped per the dispatch brief; when disabled, do nothing;
when the underlying save fails, swallow the exception.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
from gecko_core.memory import auto_journal
from gecko_core.memory.models import MemoryEntryType, MemoryScope


@pytest.fixture
def saved_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Patch `auto_journal.save` and capture invocations."""
    calls: list[dict[str, Any]] = []

    async def _fake_save(
        scope: MemoryScope,
        entry_type: MemoryEntryType,
        value: dict[str, Any],
        *,
        key: str | None = None,
        tx_signature: str | None = None,
        **kwargs: Any,
    ) -> UUID:
        calls.append(
            {
                "scope": scope,
                "entry_type": entry_type,
                "value": value,
                "key": key,
                "tx_signature": tx_signature,
            }
        )
        return uuid4()

    monkeypatch.setattr(auto_journal, "save", _fake_save)

    # also stub _is_enabled to skip Supabase project lookup
    async def _enabled(*, journal: bool, project_id: Any, store: Any) -> bool:
        return bool(journal)

    monkeypatch.setattr(auto_journal, "_is_enabled", _enabled)
    return calls


async def test_journal_verdict_writes_entry(saved_calls: list[dict[str, Any]]) -> None:
    sid = uuid4()
    pid = uuid4()
    out = await auto_journal.journal_verdict(
        session_id=sid,
        project_id=pid,
        idea="hotel guide for Brazil",
        verdict="ship",
        scores={"tam": 8, "wedge": 7, "v1_feasibility": 9},
        sources_count=12,
        tx_signature="solana-tx-abc",
    )
    assert out is not None
    assert len(saved_calls) == 1
    call = saved_calls[0]
    assert call["entry_type"] == MemoryEntryType.verdict_received
    assert call["scope"].type == "project"
    assert call["scope"].id == str(pid)
    assert call["value"]["idea"] == "hotel guide for Brazil"
    assert call["value"]["verdict"] == "ship"
    assert call["value"]["sources_count"] == 12
    assert call["tx_signature"] == "solana-tx-abc"


async def test_journal_scaffold_writes_entry(saved_calls: list[dict[str, Any]]) -> None:
    sid = uuid4()
    out = await auto_journal.journal_scaffold(
        session_id=sid,
        project_id=None,
        output_paths=["/tmp/PRD.md", "/tmp/business-plan.md", "/tmp/BUILDING.md"],
        total_tokens=1500,
        cost_usd=0.04,
    )
    assert out is not None
    call = saved_calls[0]
    assert call["entry_type"] == MemoryEntryType.scaffold_generated
    assert call["scope"].type == "session"
    assert call["value"]["output_paths"] == [
        "/tmp/PRD.md",
        "/tmp/business-plan.md",
        "/tmp/BUILDING.md",
    ]
    assert call["value"]["total_tokens"] == 1500


async def test_journal_plan_carries_all_voices(saved_calls: list[dict[str, Any]]) -> None:
    sid = uuid4()
    voices = [
        {"role": "ceo", "closing_line": "lock the LOI", "model_used": "kimi-k2"},
        {"role": "cto", "closing_line": "ship the parser", "model_used": "deepseek"},
        {"role": "business_manager", "closing_line": "raise seed", "model_used": "kimi-k2"},
        {
            "role": "product_manager",
            "closing_line": "interview 5 founders",
            "model_used": "deepseek",
        },
        {"role": "staff_manager", "closing_line": "sprint plan", "model_used": "claude"},
    ]
    await auto_journal.journal_plan(
        session_id=sid,
        project_id=None,
        voices=voices,
        tier_preset="balanced",
        total_cost_usd=0.18,
    )
    call = saved_calls[0]
    assert call["entry_type"] == MemoryEntryType.plan_advised
    assert len(call["value"]["voices"]) == 5
    assert call["value"]["tier_preset"] == "balanced"


async def test_no_journal_flag_suppresses_write(
    saved_calls: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    sid = uuid4()
    out = await auto_journal.journal_verdict(
        session_id=sid,
        project_id=None,
        idea="x",
        verdict="kill",
        journal=False,
    )
    assert out is None
    assert saved_calls == []


async def test_failed_write_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failure inside save() must not propagate to the user response."""

    async def _enabled(*, journal: bool, project_id: Any, store: Any) -> bool:
        return True

    async def _boom(*args: Any, **kwargs: Any) -> UUID:
        raise RuntimeError("supabase down")

    monkeypatch.setattr(auto_journal, "_is_enabled", _enabled)
    monkeypatch.setattr(auto_journal, "save", _boom)

    out = await auto_journal.journal_verdict(
        session_id=uuid4(), project_id=None, idea="x", verdict="ship"
    )
    assert out is None  # swallowed
