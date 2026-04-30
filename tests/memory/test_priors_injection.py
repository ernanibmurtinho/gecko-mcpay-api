"""Priors-injection tests (S6-MINE-02).

Covers:
- env gate: opt-out yields empty block
- token cap: rendered block stays ≤ PRIORS_TOKEN_BUDGET (oldest dropped first)
- formatting: verdict + feature_shipped lines render with the expected fields
- defensive: store failures don't propagate
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from gecko_core.memory.models import MemoryEntry, MemoryEntryType, MemoryScope
from gecko_core.memory.priors import (
    PRIORS_TOKEN_BUDGET,
    build_priors_block,
    is_priors_enabled,
    render_priors_block,
)
from gecko_core.routing.costs import estimate_tokens


def _entry(
    *,
    entry_type: MemoryEntryType,
    value: dict,
    age_days: int = 0,
) -> MemoryEntry:
    return MemoryEntry(
        id=uuid4(),
        scope=MemoryScope(type="project", id="p1"),
        entry_type=entry_type,
        key=None,
        value=value,
        embedding=None,
        tx_signature=None,
        created_at=datetime.now(UTC) - timedelta(days=age_days),
    )


def test_priors_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GECKO_MEMORY_PRIORS", raising=False)
    assert is_priors_enabled() is False


def test_priors_env_gate_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GECKO_MEMORY_PRIORS", "1")
    assert is_priors_enabled() is True


def test_render_empty_returns_empty_string() -> None:
    assert render_priors_block([]) == ""


def test_render_includes_verdict_and_shipped_lines() -> None:
    entries = [
        _entry(
            entry_type=MemoryEntryType.verdict_received,
            value={"verdict": "ship", "idea": "hotel guide for Brazil"},
            age_days=2,
        ),
        _entry(
            entry_type=MemoryEntryType.feature_shipped,
            value={"feature": "auth flow", "outcome": "active users +12%"},
            age_days=10,
        ),
    ]
    block = render_priors_block(entries)
    assert "PRIOR DECISIONS" in block
    assert "VERDICT=SHIP" in block
    assert "hotel guide for Brazil" in block
    assert "SHIPPED auth flow" in block
    assert "active users +12%" in block


def test_render_drops_oldest_to_fit_token_budget() -> None:
    # 200 entries. With ~12-14 tokens each (sub-100 char line), we
    # blow the 800-token budget and the renderer must drop oldest first.
    # Idea field stays under the 120-char truncation guard so the idx
    # marker survives in the rendered output.
    entries: list[MemoryEntry] = []
    for i in range(200):
        entries.append(
            _entry(
                entry_type=MemoryEntryType.verdict_received,
                value={"verdict": "ship", "idea": f"idea-idx={i}-{'x' * 40}"},
                age_days=i,
            )
        )
    block = render_priors_block(entries)
    assert estimate_tokens(block) <= PRIORS_TOKEN_BUDGET
    # Newest entry (idx=0) must survive; oldest (idx=199) must be dropped.
    assert "idx=0-" in block
    assert "idx=199-" not in block


@pytest.mark.asyncio
async def test_build_priors_block_disabled_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GECKO_MEMORY_PRIORS", raising=False)
    block = await build_priors_block("some-project-id")
    assert block == ""


@pytest.mark.asyncio
async def test_build_priors_block_no_project_id_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GECKO_MEMORY_PRIORS", "1")
    block = await build_priors_block(None)
    assert block == ""


@pytest.mark.asyncio
async def test_build_priors_block_swallows_store_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A flaky store must not crash the caller — defensive contract."""
    monkeypatch.setenv("GECKO_MEMORY_PRIORS", "1")

    class _BadStore:
        async def list_by_scope(self, **_: object) -> list[MemoryEntry]:
            raise RuntimeError("supabase down")

    block = await build_priors_block("p1", store=_BadStore())  # type: ignore[arg-type]
    assert block == ""
