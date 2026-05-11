"""Async-safe, in-process TTL cache for the trade-agent hot path.

Design constraints (per `docs/strategy/2026-05-11-trade-vertical-expansion.md` §6):

* No external store — pure-Python ``dict`` + :func:`time.monotonic`. Not
  Redis. The trader-mode entry-gate budget cannot afford a network hop
  per check.
* Async-safe — multiple coroutines may ``get``/``set`` the same key
  concurrently. We hold a per-key :class:`asyncio.Lock` so a slow
  producer doesn't stampede readers.
* Inspectable — ``stats()`` returns hit/miss/eviction counts that the
  ``bb trade-agent inspect`` command surfaces.

Eviction is lazy. We don't run a background sweeper; entries expire on
the next access. Memory pressure is bounded by the number of distinct
keys the agent touches in a TTL window, which for a single agent is
O(positions × subscriptions) — small.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _Entry:
    value: Any
    expires_at: float  # monotonic seconds


class HotpathCache:
    """In-process TTL cache.

    Locks are per-key to avoid serialising unrelated traffic. The cache
    is owned by a single :class:`gecko_core.trade_agent.runtime.Runtime`
    instance — sharing across processes is explicitly out of scope (use
    the ``agent_hotpath_snapshot`` Mongo collection for warm restart).
    """

    def __init__(self) -> None:
        self._data: dict[str, _Entry] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()
        self._hits = 0
        self._misses = 0
        self._evictions = 0
        self._sets = 0

    async def _lock_for(self, key: str) -> asyncio.Lock:
        # Acquire the global lock briefly to materialise a per-key lock.
        # We do NOT hold the global lock during get/set work itself —
        # only while creating the asyncio.Lock object the first time.
        lock = self._locks.get(key)
        if lock is not None:
            return lock
        async with self._global_lock:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
            return lock

    async def get(self, key: str) -> Any | None:
        """Return the cached value or ``None`` if missing/expired."""
        lock = await self._lock_for(key)
        async with lock:
            entry = self._data.get(key)
            if entry is None:
                self._misses += 1
                return None
            if entry.expires_at <= time.monotonic():
                # Lazy eviction.
                self._data.pop(key, None)
                self._evictions += 1
                self._misses += 1
                logger.info(
                    "hotpath_cache.evict key=%s reason=expired",
                    key,
                )
                return None
            self._hits += 1
            return entry.value

    async def set(self, key: str, value: Any, ttl_seconds: float) -> None:
        """Store ``value`` under ``key`` for ``ttl_seconds``.

        ``ttl_seconds`` must be strictly positive — a 0-TTL entry would
        be evicted on the next read and is almost certainly a bug.
        """
        if ttl_seconds <= 0:
            raise ValueError(f"ttl_seconds must be > 0, got {ttl_seconds!r}")
        lock = await self._lock_for(key)
        async with lock:
            self._data[key] = _Entry(
                value=value,
                expires_at=time.monotonic() + ttl_seconds,
            )
            self._sets += 1

    async def invalidate(self, key: str) -> None:
        """Remove ``key`` from the cache. No-op if absent."""
        lock = await self._lock_for(key)
        async with lock:
            if self._data.pop(key, None) is not None:
                self._evictions += 1
                logger.info(
                    "hotpath_cache.evict key=%s reason=invalidate",
                    key,
                )

    def stats(self) -> dict[str, int]:
        """Return hit/miss/eviction counters.

        Surfaced by ``bb trade-agent inspect`` so users can sanity-check
        the agent's read pattern without attaching a debugger.
        """
        return {
            "hits": self._hits,
            "misses": self._misses,
            "evictions": self._evictions,
            "sets": self._sets,
            "size": len(self._data),
        }
