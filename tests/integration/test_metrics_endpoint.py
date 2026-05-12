"""S24 WS-D — /metrics endpoint integration test.

Emit 5 verdict events + supporting cache hits/misses + one x402 settle,
hit GET /metrics on the live FastAPI app, assert aggregate counts and
JSON shape are what the WS-D contract promises.

Mongo is replaced by an in-memory fake via
``set_events_collection_override``; the FastAPI handler doesn't know the
difference.
"""

from __future__ import annotations

import os

os.environ.setdefault("X402_MODE", "stub")
os.environ.setdefault("GECKO_WALLET_ADDRESS", "STUB_WALLET_ADDRESS_NOT_FOR_LIVE")

from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from gecko_core.observability import emit_event
from gecko_core.observability.events import set_events_collection_override


class _FakeCollection:
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
            ts_q = query.get("ts")
            if isinstance(ts_q, dict):
                since = ts_q.get("$gte")
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
def fake_events_coll() -> Any:
    coll = _FakeCollection()
    set_events_collection_override(coll)
    yield coll
    set_events_collection_override(None)


@pytest.mark.asyncio
async def test_metrics_endpoint_aggregates_emitted_events(
    fake_events_coll: _FakeCollection,
) -> None:
    # Seed five verdict.served events at varying tiers/latencies, plus
    # cache hit/miss + one settlement. All inside the 24h window.
    _ = datetime.now(UTC)  # anchor for human reading of test intent
    for i in range(5):
        await emit_event(
            "verdict.served",
            {
                "tool": "gecko_trade_research",
                "tier": "basic" if i < 3 else "pro",
                "latency_ms": 100 + (i * 50),
                "citation_count": 4,
                "dissent_count": 1,
                "confidence": 0.7,
            },
            wallet=f"WALLET-{i % 2}",
        )
    await emit_event("cache.hit", {"agent_id": "a1"})
    await emit_event("cache.hit", {"agent_id": "a1"})
    await emit_event("cache.miss", {"agent_id": "a1"})
    await emit_event(
        "x402.settle",
        {"route": "/trade_research", "amount_usdc": 0.25, "signature": "sig1"},
    )
    await emit_event("agent.error", {"agent_id": "a1", "reason": "exec_error"})
    await emit_event(
        "x402.live_blocked",
        {"tool": "gecko_trade_research", "reason": "not_in_allowlist"},
    )

    # Hit the endpoint via a freshly imported FastAPI app.
    from gecko_api.main import app  # imported after env+override are in place

    client = TestClient(app)
    resp = client.get("/metrics")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # Shape contract.
    assert body["schema_version"] == 1
    assert "generated_at" in body
    assert "windows" in body
    assert set(body["windows"].keys()) == {"24h", "7d"}

    w24 = body["windows"]["24h"]
    assert w24["verdicts_served"] == 5
    assert w24["cache_hits"] == 2
    assert w24["cache_misses"] == 1
    assert w24["cache_hit_ratio"] == pytest.approx(2 / 3, rel=1e-3)
    assert w24["revenue_usdc"] == pytest.approx(0.25)
    # Two distinct wallets seeded.
    assert w24["distinct_wallets"] == 2
    # p50/p95 present + non-None for non-empty sample.
    assert w24["latency_p50_ms"] is not None
    assert w24["latency_p95_ms"] is not None
    # top_tiers: basic should outrank pro (3 vs 2).
    tiers = {row["tier"]: row["count"] for row in w24["top_tiers"]}
    assert tiers.get("basic") == 3
    assert tiers.get("pro") == 2

    assert body["errors_24h"] == 1
    assert body["x402_live_blocked_24h"] == 1
