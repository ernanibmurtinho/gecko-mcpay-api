"""S24 WS-D — assert each EventKind writes to the events collection
with the expected payload shape.

The Mongo client is replaced by an in-memory fake via
``set_events_collection_override`` so tests run without a live Atlas.
"""

from __future__ import annotations

from typing import Any

import pytest
from gecko_core.observability import emit_event
from gecko_core.observability.events import set_events_collection_override


class _FakeCollection:
    """Minimal Motor-compatible async insert_one + find / count surface."""

    def __init__(self) -> None:
        self.docs: list[dict[str, Any]] = []

    async def insert_one(self, doc: dict[str, Any]) -> Any:
        self.docs.append(doc)

        class _Res:
            inserted_id = len(self.docs)

        return _Res()

    def find(self, query: dict[str, Any] | None = None) -> Any:
        docs = list(self.docs)
        if query:
            ts_q = query.get("ts", {})
            since = ts_q.get("$gte") if isinstance(ts_q, dict) else None
            if since is not None:
                docs = [d for d in docs if d["ts"] >= since]
            event_q = query.get("event")
            if event_q is not None:
                docs = [d for d in docs if d["event"] == event_q]

        class _Cursor:
            def __init__(self, items: list[dict[str, Any]]) -> None:
                self._items = items

            def __aiter__(self) -> Any:
                return self._gen()

            async def _gen(self) -> Any:
                for item in self._items:
                    yield item

        return _Cursor(docs)

    async def count_documents(self, query: dict[str, Any]) -> int:
        n = 0
        async for _ in self.find(query):
            n += 1
        return n


@pytest.fixture
def fake_events() -> Any:
    coll = _FakeCollection()
    set_events_collection_override(coll)
    yield coll
    set_events_collection_override(None)


@pytest.mark.asyncio
async def test_verdict_served_event_shape(fake_events: _FakeCollection) -> None:
    await emit_event(
        "verdict.served",
        {
            "tool": "gecko_trade_research",
            "tier": "basic",
            "latency_ms": 1234,
            "citation_count": 5,
            "dissent_count": 1,
            "confidence": 0.72,
        },
        wallet="WALLET-A",
    )
    assert len(fake_events.docs) == 1
    doc = fake_events.docs[0]
    assert doc["event"] == "verdict.served"
    assert doc["wallet"] == "WALLET-A"
    assert doc["payload"]["tier"] == "basic"
    assert doc["payload"]["latency_ms"] == 1234
    assert doc["payload"]["citation_count"] == 5
    assert doc["ts"] is not None


@pytest.mark.asyncio
async def test_cache_hit_and_miss(fake_events: _FakeCollection) -> None:
    await emit_event("cache.hit", {"agent_id": "a1", "idea_hash": "x"})
    await emit_event("cache.miss", {"agent_id": "a1", "idea_hash": "y"})
    kinds = [d["event"] for d in fake_events.docs]
    assert kinds == ["cache.hit", "cache.miss"]


@pytest.mark.asyncio
async def test_oracle_cost_usd_shape(fake_events: _FakeCollection) -> None:
    await emit_event(
        "oracle.cost_usd",
        {
            "tier": "pro",
            "llm_tokens_in": 1200,
            "llm_tokens_out": 800,
            "embed_tokens": 400,
            "total_usd": 0.0123,
        },
    )
    payload = fake_events.docs[0]["payload"]
    assert payload["tier"] == "pro"
    assert payload["total_usd"] == 0.0123


@pytest.mark.asyncio
async def test_agent_tick_and_opportunity(fake_events: _FakeCollection) -> None:
    await emit_event("agent.tick", {"agent_id": "a1", "mode": "advisor"})
    await emit_event(
        "agent.opportunity",
        {"agent_id": "a1", "mint": "Mint1", "idea": "buy_dip", "rule_id": "r-entry"},
        wallet="WALLET-A",
    )
    assert {d["event"] for d in fake_events.docs} == {
        "agent.tick",
        "agent.opportunity",
    }


@pytest.mark.asyncio
async def test_x402_settle_shape(fake_events: _FakeCollection) -> None:
    await emit_event(
        "x402.settle",
        {
            "route": "/trade_research",
            "amount_usdc": 0.25,
            "signature": "stub:sig123",
            "latency_ms": 88,
        },
    )
    payload = fake_events.docs[0]["payload"]
    assert payload["amount_usdc"] == 0.25
    assert payload["signature"] == "stub:sig123"


@pytest.mark.asyncio
async def test_x402_live_blocked_event(fake_events: _FakeCollection) -> None:
    await emit_event(
        "x402.live_blocked",
        {
            "tool": "gecko_trade_research",
            "requested_mode": "live",
            "reason": "kill_switch_engaged",
        },
    )
    doc = fake_events.docs[0]
    assert doc["event"] == "x402.live_blocked"
    assert doc["payload"]["reason"] == "kill_switch_engaged"


@pytest.mark.asyncio
async def test_emit_never_raises_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """If Mongo isn't configured, emit_event silently no-ops."""
    set_events_collection_override(None)
    monkeypatch.delenv("MONGODB_URI", raising=False)
    monkeypatch.delenv("MONGO_URI", raising=False)
    # Must not raise.
    await emit_event("verdict.served", {"tier": "basic"})


@pytest.mark.asyncio
async def test_emit_swallows_collection_errors() -> None:
    class _BrokenColl:
        async def insert_one(self, doc: dict[str, Any]) -> Any:
            raise RuntimeError("mongo blew up")

    set_events_collection_override(_BrokenColl())
    try:
        await emit_event("verdict.served", {"tier": "basic"})
    finally:
        set_events_collection_override(None)
