"""S24 WS-F #2 — assert the global panel semaphore bounds concurrency to 8.

Fires N=24 concurrent get_verdict calls (each with a unique idea so cache
is bypassed). The fake caller records max-concurrent invocations seen
inside the semaphore-guarded section. Expectation: max-concurrent == 8,
not 24.
"""

from __future__ import annotations

import asyncio

import pytest
from gecko_core.trade_agent.oracle import (
    PANEL_CONCURRENCY,
    OracleWrapper,
    reset_panel_semaphore_for_tests,
)
from gecko_core.trade_agent.state.mongo import InMemoryStateStore


@pytest.fixture(autouse=True)
def _reset_semaphore() -> None:
    reset_panel_semaphore_for_tests()
    yield
    reset_panel_semaphore_for_tests()


@pytest.mark.asyncio
async def test_panel_semaphore_caps_concurrent_calls_at_8() -> None:
    in_flight = 0
    max_in_flight = 0
    lock = asyncio.Lock()

    async def slow_caller(*, idea, tier, agent_id):
        nonlocal in_flight, max_in_flight
        async with lock:
            in_flight += 1
            if in_flight > max_in_flight:
                max_in_flight = in_flight
        # Long enough that every coroutine piles in before any release.
        await asyncio.sleep(0.3)
        async with lock:
            in_flight -= 1
        return {"verdict": "act"}

    # One state store + OracleWrapper per agent — InMemoryStateStore has a
    # single asyncio.Lock that would serialise cache lookups across agents
    # and mask the semaphore cap. Production has no such cross-agent lock.
    oracles = [
        OracleWrapper(
            agent_id=f"agent-{i}",
            state_store=InMemoryStateStore(),
            caller=slow_caller,
        )
        for i in range(24)
    ]
    await asyncio.gather(
        *(
            o.get_verdict(idea=f"unique idea {i}", trigger="entry_gate")
            for i, o in enumerate(oracles)
        )
    )
    # Cap is exactly PANEL_CONCURRENCY (8). Allow == cap, never above.
    assert max_in_flight <= PANEL_CONCURRENCY, (
        f"semaphore breached: max in-flight {max_in_flight} > cap {PANEL_CONCURRENCY}"
    )
    # And we should actually saturate it, otherwise the test is vacuous.
    assert max_in_flight == PANEL_CONCURRENCY
