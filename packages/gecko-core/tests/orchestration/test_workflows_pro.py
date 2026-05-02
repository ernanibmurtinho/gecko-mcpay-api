"""Workflow-level integration test for tier='pro'.

Stubs the LLM client + AG2 build_groupchat + SessionStore. Asserts:
  - events appended to pro_events in monotonically increasing order
  - per-agent cost rows written to session_costs
  - ResearchResult.transcript is populated and tier flips to 'pro'
"""

from __future__ import annotations

from collections.abc import Awaitable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest


@pytest.fixture
def stubbed_rag(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make rag_query return a couple of canned chunks; skip Supabase entirely."""
    from types import SimpleNamespace

    async def _fake_rag_query(*_a: object, **_kw: object) -> list[Any]:
        return [
            SimpleNamespace(
                source_url="https://example.com/a",
                chunk_index=0,
                similarity=0.9,
                text="Hosts care about local recommendations.",
            ),
            SimpleNamespace(
                source_url="https://example.com/b",
                chunk_index=1,
                similarity=0.7,
                text="Brazil has a fragmented hospitality market.",
            ),
        ]

    monkeypatch.setattr("gecko_core.workflows.rag_query", _fake_rag_query, raising=False)
    # `_run_pro_debate` imports rag_query inside the function — patch its
    # module-level symbol too.
    monkeypatch.setattr("gecko_core.rag.query.rag_query", _fake_rag_query, raising=False)


@pytest.fixture
def stubbed_groupchat(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace AG2's `build_groupchat` with a fake yielding 5 canned replies."""
    canned = {
        "analyst": "TAM is real.",
        "critic": "Wedge is fuzzy.",
        "architect": "Next.js + Supabase.",
        "scoper": "V1 in 4 days.",
        "judge": "7/10 — ship V1.",
    }

    class _FakeAgent:
        def __init__(self, name: str) -> None:
            self.name = name

        async def a_generate_reply(self, messages: object = None) -> str:
            return canned[self.name]

    class _FakeChat:
        def __init__(self) -> None:
            self.agents = [_FakeAgent(n) for n in canned]
            self.messages: list[dict[str, object]] = []

    class _FakeManager:
        def __init__(self) -> None:
            self.groupchat = _FakeChat()

    def _fake_build(_cfg: dict[str, object], **_kw: object) -> _FakeManager:
        return _FakeManager()

    from gecko_core.orchestration import pro as pro_mod

    monkeypatch.setattr(pro_mod, "build_groupchat", _fake_build)


def _make_fake_store() -> AsyncMock:
    """A SessionStore stub that captures pro_events + session_costs writes."""
    store = AsyncMock()
    store.appended_events: list[dict[str, Any]] = []  # type: ignore[attr-defined]
    store.appended_costs: list[dict[str, Any]] = []  # type: ignore[attr-defined]

    async def _append_pro_event(**kw: Any) -> int:
        store.appended_events.append(kw)  # type: ignore[attr-defined]
        return len(store.appended_events)  # type: ignore[attr-defined]

    async def _append_session_cost(**kw: Any) -> int:
        store.appended_costs.append(kw)  # type: ignore[attr-defined]
        return len(store.appended_costs)  # type: ignore[attr-defined]

    store.append_pro_event = AsyncMock(side_effect=_append_pro_event)
    store.append_session_cost = AsyncMock(side_effect=_append_session_cost)
    return store


async def test_workflows_pro_branch_emits_events_and_costs(
    stubbed_rag: None,
    stubbed_groupchat: None,
) -> None:
    """End-to-end pro branch under stubbed dependencies."""
    from gecko_core.models import ResearchResult, SourceInfo
    from gecko_core.workflows import _run_pro_debate

    fake_store = _make_fake_store()
    sid = uuid4()

    base_result = ResearchResult(
        session_id=str(sid),
        tier="basic",
        business_plan={  # type: ignore[arg-type]
            "problem": "p",
            "icp": "i",
            "solution": "s",
            "market": "m",
            "business_model": "bm",
            "channels": "c",
            "risks": ["r"],
            "citations": [],
        },
        validation_report={  # type: ignore[arg-type]
            "market_size_signal": "x",
            "competitor_analysis": "y",
            "demand_evidence": "z",
            "risk_flags": [],
            "citations": [],
        },
        prd={  # type: ignore[arg-type]
            "v1_scope": ["a"],
            "v2_scope": [],
            "v3_scope": [],
            "acceptance_criteria": ["b"],
            "non_functional": [],
            "success_metrics": [],
            "citations": [],
        },
        sources=[
            SourceInfo(
                url="https://example.com/a",
                type="web",
                chunk_count=1,
                indexed_at=datetime.now(UTC),
            )
        ],
    )

    result = await _run_pro_debate(sid, "an idea worth validating", base_result, fake_store)

    # Tier flipped, transcript populated.
    assert result.tier == "pro"
    assert result.transcript is not None
    assert len(result.transcript["turns"]) == 5
    assert result.pro_session_summary == "7/10 — ship V1."

    # pro_events writes: 5 turn_start + 5 turn_end + 1 final = 11
    assert len(fake_store.appended_events) == 11  # type: ignore[attr-defined]
    seqs = [e["seq"] for e in fake_store.appended_events]  # type: ignore[attr-defined]
    assert seqs == sorted(seqs)  # monotonically increasing
    types_in_order = [e["event_type"] for e in fake_store.appended_events]  # type: ignore[attr-defined]
    assert types_in_order[-1] == "final"

    # session_costs: one row per turn_end with non-null agent.
    assert len(fake_store.appended_costs) == 5  # type: ignore[attr-defined]
    agents = {row["agent"] for row in fake_store.appended_costs}  # type: ignore[attr-defined]
    assert agents == {"analyst", "critic", "architect", "scoper", "judge"}
    # All cost values must be non-negative Decimals; with 0 token counts the
    # estimate is exactly 0, which is fine — the row exists for attribution.
    for row in fake_store.appended_costs:  # type: ignore[attr-defined]
        assert isinstance(row["cost_usd"], Decimal)
        assert row["cost_usd"] >= Decimal("0")


_ = (Awaitable, UUID)  # keep imports referenced
