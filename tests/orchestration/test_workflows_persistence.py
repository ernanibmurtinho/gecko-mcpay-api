"""S17-PERSIST-01 — research workflow must persist result_json to the store.

Bug repro: `bb research` runs end-to-end successfully but
`bb scaffold $SID` later fails with "Not ready: session <id> has no
persisted result yet". Root cause: only the FastAPI background task
called `store.set_result(...)`. CLI / MCP / direct-SDK callers went
through `gecko_core.workflows.research` which only flipped status to
'complete' and never wrote `result_json`.

This test pins the contract by driving the workflow against an in-memory
fake store (heavy collaborators stubbed) and asserting set_result was
called with a non-None dict for both basic and pro tiers.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from gecko_core import workflows
from gecko_core.models import (
    PRD,
    BusinessPlan,
    ResearchResult,
    SourceInfo,
    ValidationReport,
    Verdict,
)


class _FakeStore:
    """Captures every persistence call workflows.research makes."""

    def __init__(self) -> None:
        self.session_id: UUID = uuid4()
        self.results: dict[UUID, dict[str, Any]] = {}
        self.statuses: dict[UUID, str] = {}
        self.costs: list[tuple[UUID, str, float]] = []

    async def create(
        self,
        idea: str,
        tier: str = "basic",
        payment_mode: str = "stub",
        *,
        phase: str = "pre_product",
        parent_session_id: UUID | None = None,
    ) -> UUID:
        return self.session_id

    async def update_status(self, session_id: UUID, status: str) -> None:
        self.statuses[session_id] = status

    async def set_result(self, session_id: UUID, result: dict[str, Any]) -> None:
        self.results[session_id] = result

    async def add_cost(self, session_id: UUID, kind: str, amount_usd: float) -> None:
        self.costs.append((session_id, kind, float(amount_usd)))

    async def get(self, session_id: UUID) -> Any:  # for the auto-journal hook
        return None


def _build_result(sid: UUID, *, tier: str = "basic") -> ResearchResult:
    return ResearchResult(
        session_id=str(sid),
        tier=tier,  # type: ignore[arg-type]
        business_plan=BusinessPlan(
            problem="p",
            icp="i",
            solution="s",
            market="m",
            business_model="bm",
            channels="c",
            risks=["r1"],
            citations=[],
        ),
        validation_report=ValidationReport(
            market_size_signal="m",
            competitor_analysis="c",
            demand_evidence="d",
            risk_flags=[],
            citations=[],
            gap_classification="Partial:segment",
            gap_summary="gap",
        ),
        prd=PRD(
            v1_scope=["v1"],
            v2_scope=[],
            v3_scope=[],
            acceptance_criteria=["a"],
            non_functional=[],
            success_metrics=[],
            citations=[],
        ),
        sources=[
            SourceInfo(
                url="https://example.com/a",  # type: ignore[arg-type]
                type="web",
                chunk_count=1,
                indexed_at=datetime.now(UTC),
            )
        ],
        verdict=Verdict.REFINE,
    )


@pytest.mark.asyncio
async def test_research_basic_persists_result_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """basic-tier workflow.research() must call store.set_result before completing."""
    store = _FakeStore()
    expected_result = _build_result(store.session_id, tier="basic")

    async def _fake_basic_generate(session_id: UUID, idea: str, _store: Any) -> ResearchResult:
        return expected_result

    async def _fake_ingest(session_id: UUID, candidates: Any, _store: Any) -> Any:
        from gecko_core.ingestion import IngestionResult

        return IngestionResult(session_id=str(session_id), indexed=1, skipped=0, failed=0)

    async def _fake_payment_gate(*_a: Any, **_k: Any) -> None:
        return None

    async def _fake_dispatch(*_a: Any, **_k: Any) -> str | None:
        return None

    monkeypatch.setattr(workflows, "basic_generate", _fake_basic_generate)
    monkeypatch.setattr(workflows, "ingest", _fake_ingest)
    monkeypatch.setattr(workflows, "run_payment_gate", _fake_payment_gate)
    monkeypatch.setattr(workflows, "_dispatch_stub_integration_providers", _fake_dispatch)

    # Disable transcript capture + auto-journal: best-effort hooks that
    # would otherwise require disk + memory wiring we don't care about.
    monkeypatch.setenv("GECKO_TRANSCRIPT_CAPTURE", "false")

    result = await workflows.research(
        idea="smoke test",
        tier="basic",
        urls=["https://example.com/a"],
        auto_approve=True,
        store=store,  # type: ignore[arg-type]
        skip_payment_gate=True,
        journal=False,
    )

    # The bug: result was returned but never persisted. Pin the fix.
    assert store.session_id in store.results, (
        "workflows.research did not call store.set_result — "
        "CLI scaffold path will fail with 'no persisted result yet'"
    )
    persisted = store.results[store.session_id]
    assert isinstance(persisted, dict)
    assert persisted["session_id"] == str(store.session_id)
    assert persisted["verdict"] == result.verdict.value
    # And status flipped to 'complete' AFTER the result write (order
    # matters: a reader that sees status='complete' must also see the
    # persisted result_json).
    assert store.statuses.get(store.session_id) == "complete"
