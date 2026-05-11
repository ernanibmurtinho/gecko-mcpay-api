"""Tests for HotpathCache — TTL, concurrency, invalidation, stats."""

from __future__ import annotations

import asyncio

import pytest
from gecko_core.trade_agent.hotpath.cache import HotpathCache


async def test_set_get_roundtrip() -> None:
    cache = HotpathCache()
    await cache.set("k", {"x": 1}, ttl_seconds=10)
    got = await cache.get("k")
    assert got == {"x": 1}
    stats = cache.stats()
    assert stats["hits"] == 1
    assert stats["misses"] == 0
    assert stats["sets"] == 1


async def test_miss_returns_none_and_counts() -> None:
    cache = HotpathCache()
    assert await cache.get("nope") is None
    assert cache.stats()["misses"] == 1


async def test_ttl_expiration_evicts() -> None:
    cache = HotpathCache()
    await cache.set("k", "v", ttl_seconds=0.01)
    await asyncio.sleep(0.05)
    assert await cache.get("k") is None
    stats = cache.stats()
    assert stats["evictions"] == 1
    assert stats["misses"] == 1


async def test_invalidate_removes_entry() -> None:
    cache = HotpathCache()
    await cache.set("k", "v", ttl_seconds=10)
    await cache.invalidate("k")
    assert await cache.get("k") is None
    assert cache.stats()["evictions"] == 1


async def test_invalidate_missing_is_noop() -> None:
    cache = HotpathCache()
    await cache.invalidate("nope")  # should not raise
    assert cache.stats()["evictions"] == 0


async def test_ttl_must_be_positive() -> None:
    cache = HotpathCache()
    with pytest.raises(ValueError):
        await cache.set("k", "v", ttl_seconds=0)
    with pytest.raises(ValueError):
        await cache.set("k", "v", ttl_seconds=-1)


async def test_concurrent_set_get_same_key() -> None:
    cache = HotpathCache()

    async def writer(i: int) -> None:
        await cache.set("shared", i, ttl_seconds=10)

    async def reader() -> int | None:
        return await cache.get("shared")

    await asyncio.gather(*(writer(i) for i in range(50)))
    # After concurrent writers, key must exist and hold one of the
    # written values.
    val = await cache.get("shared")
    assert val is not None
    assert 0 <= val <= 49


async def test_size_in_stats() -> None:
    cache = HotpathCache()
    for i in range(5):
        await cache.set(f"k{i}", i, ttl_seconds=10)
    assert cache.stats()["size"] == 5
