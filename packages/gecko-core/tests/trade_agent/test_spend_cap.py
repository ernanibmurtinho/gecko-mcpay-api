"""S24 WS-F #4 — per-agent daily oracle spend cap.

Seed 5 ``oracle.cost_usd`` events totalling $3.01 for one ``agent_id``.
Calling ``get_verdict`` must refuse with :class:`SpendCapExceededError`
and emit ``agent.spend_cap_hit``. The runtime catches that and journals a
``circuit_breaker_trip`` with reason ``daily_spend_cap``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from gecko_core.observability.events import set_events_collection_override
from gecko_core.trade_agent.oracle import (
    OracleWrapper,
    SpendCapExceededError,
    make_stub_caller,
    reset_panel_semaphore_for_tests,
)
from gecko_core.trade_agent.state.mongo import InMemoryStateStore


class _FakeEvents:
    """Motor-shaped fake collection — supports insert_one + find/aiter."""

    def __init__(self, seeded: list[dict[str, Any]] | None = None) -> None:
        self.docs: list[dict[str, Any]] = list(seeded or [])

    async def insert_one(self, doc: dict[str, Any]) -> Any:
        self.docs.append(doc)

        class _Res:
            inserted_id = len(self.docs)

        return _Res()

    def find(self, query: dict[str, Any] | None = None) -> Any:
        docs = list(self.docs)
        q = query or {}
        ev = q.get("event")
        if ev is not None:
            docs = [d for d in docs if d.get("event") == ev]
        ts_q = q.get("ts", {})
        since = ts_q.get("$gte") if isinstance(ts_q, dict) else None
        if since is not None:
            docs = [d for d in docs if d.get("ts") >= since]
        pa_q = q.get("payload.agent_id")
        if pa_q is not None:
            docs = [d for d in docs if (d.get("payload") or {}).get("agent_id") == pa_q]

        class _Cursor:
            def __init__(self, items: list[dict[str, Any]]) -> None:
                self._items = items

            def __aiter__(self) -> Any:
                return self._gen()

            async def _gen(self) -> Any:
                for item in self._items:
                    yield item

        return _Cursor(docs)


@pytest.fixture(autouse=True)
def _reset_semaphore() -> None:
    reset_panel_semaphore_for_tests()
    yield
    reset_panel_semaphore_for_tests()


@pytest.mark.asyncio
async def test_spend_cap_refuses_panel_call_and_emits_event() -> None:
    now = datetime.now(UTC)
    seeded = [
        {
            "ts": now,
            "event": "oracle.cost_usd",
            "wallet": None,
            "payload": {"agent_id": "agent-X", "total_usd": v},
        }
        # 5 events summing to $3.01 (cap is $3.00).
        for v in (0.60, 0.60, 0.60, 0.60, 0.61)
    ]
    fake = _FakeEvents(seeded=seeded)
    set_events_collection_override(fake)
    try:
        store = InMemoryStateStore()
        oracle = OracleWrapper(
            agent_id="agent-X",
            state_store=store,
            caller=make_stub_caller({"verdict": "act"}),
        )
        with pytest.raises(SpendCapExceededError):
            await oracle.get_verdict(
                idea="deposit USDC into Kamino today",
                trigger="entry_gate",
                force_refresh=True,
            )
        kinds = [d["event"] for d in fake.docs if d["event"] == "agent.spend_cap_hit"]
        assert len(kinds) == 1, "expected exactly one agent.spend_cap_hit event"
    finally:
        set_events_collection_override(None)


@pytest.mark.asyncio
async def test_runtime_halts_on_spend_cap(valid_spec_dict: dict[str, Any]) -> None:
    """Runtime catches SpendCapExceededError -> journals circuit_breaker_trip
    with reason daily_spend_cap and flips status to halted."""
    from gecko_core.trade_agent.runtime import AgentRuntime
    from gecko_core.trade_agent.spec import AgentSpec

    spec = AgentSpec.model_validate(valid_spec_dict)

    now = datetime.now(UTC)
    seeded = [
        {
            "ts": now,
            "event": "oracle.cost_usd",
            "wallet": None,
            "payload": {"agent_id": "agent-Y", "total_usd": 3.50},
        }
    ]
    fake = _FakeEvents(seeded=seeded)
    set_events_collection_override(fake)
    try:
        store = InMemoryStateStore()
        oracle = OracleWrapper(
            agent_id="agent-Y",
            state_store=store,
            caller=make_stub_caller({"verdict": "act"}),
        )
        runtime = AgentRuntime(
            agent_id="agent-Y",
            spec=spec,
            mode="advisor",
            state_store=store,
            oracle=oracle,
        )
        # Directly invoke the halt helper as if the oracle raised mid-tick.
        await runtime._halt_on_spend_cap("test-cap-trip")
        assert runtime.status == "halted"
        # Drain the journal queue so we can assert the entries.
        entries: list[Any] = []
        while not runtime._journal_q.empty():
            entries.append(runtime._journal_q.get_nowait())
        events = [(e.event, e.payload) for e in entries]
        kinds = [e for e, _ in events]
        assert "circuit_breaker_trip" in kinds
        trip = next(p for k, p in events if k == "circuit_breaker_trip")
        assert trip["reason"] == "daily_spend_cap"
    finally:
        set_events_collection_override(None)
