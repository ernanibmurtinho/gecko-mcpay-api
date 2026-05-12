"""Oracle wrapper around ``gecko_trade_research``.

All MCP calls flow through here. The runtime never invokes the MCP tool
directly — it asks the oracle for a verdict; the oracle decides whether
to serve from cache or charge.

Triggers per `docs/strategy/2026-05-11-trade-vertical-expansion.md` §5:

* Scheduled refresh (basic, 24h)
* Startup pro-tier verdict (once per ``up``)
* Circuit-breaker trip (basic, 1x/h ceiling)
* Manual user re-verdict (basic or pro, 1x/15 min ceiling)
* Entry gate (basic; cache hit on identical idea_hash avoids it)
* Spec change (pro)

The wrapper exposes ``get_verdict(idea, tier, trigger)`` and the four
trigger-specific helpers; trigger is what enforces the rate limits.

The MCP call itself is abstracted as :class:`VerdictCaller`. Real
production wires the gecko_trade_research client; tests inject a fake.
This keeps the cache-then-charge logic testable without booting MCP.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from gecko_core.observability import emit_event
from gecko_core.trade_agent.state.models import AgentVerdictCacheEntry
from gecko_core.trade_agent.state.mongo import StateStore

logger = logging.getLogger(__name__)

Tier = Literal["basic", "pro"]
Trigger = Literal[
    "scheduled",
    "startup",
    "circuit_breaker",
    "manual",
    "entry_gate",
    "spec_change",
]

# Rate-limit ceilings per trigger, in seconds. ``None`` means "no
# rate-limit on this caller" (e.g. startup fires exactly once per agent
# lifetime, which is enforced by the runtime, not this wrapper).
RATE_LIMITS_S: dict[Trigger, float | None] = {
    "scheduled": None,  # scheduler enforces cadence; we don't double-gate
    "startup": None,
    "circuit_breaker": 60 * 60,  # 1x/h
    "manual": 60 * 15,  # 1x/15min
    "entry_gate": None,  # cache controls; entry-gate spam is bounded by event rate
    "spec_change": None,
}

# Default cache freshness window for the entry-gate path — older than
# this and we re-verdict even on idea_hash match. Tunable per-spec via
# ``oracle_freshness_s``; default chosen to match the scheduled-refresh
# 24h cadence so the entry path rarely re-fetches.
DEFAULT_FRESHNESS_S = 60 * 60 * 24


def idea_hash(idea: str | dict[str, Any]) -> str:
    """Normalise an idea string/dict into a stable cache key.

    Semantically-equivalent ideas should hit the same cache key — for a
    v0.1 baseline we lowercase + strip + collapse whitespace, then SHA-256.
    A more aggressive normaliser (synonym table, embedding-based bucket)
    is AIML-1 follow-up work — wire-compatible with this signature.
    """
    if isinstance(idea, dict):
        # Sort keys so dict ordering doesn't perturb the hash.
        import json as _json

        canonical = _json.dumps(idea, sort_keys=True, default=str)
    else:
        canonical = " ".join(idea.lower().strip().split())
    return hashlib.sha256(canonical.encode()).hexdigest()[:32]


class VerdictCaller(Protocol):
    """Boundary to the gecko_trade_research MCP tool.

    Single async method so wiring is trivial: production passes a closure
    that invokes the MCP client; tests pass an in-memory fake.
    """

    async def __call__(
        self,
        *,
        idea: str | dict[str, Any],
        tier: Tier,
        agent_id: str,
    ) -> dict[str, Any]: ...


@dataclass
class _LastFire:
    monotonic: float = 0.0


@dataclass
class OracleWrapper:
    """Cache-then-charge wrapper. One per agent runtime.

    Per-trigger last-fire timestamps are kept in-process; restart resets
    them which is acceptable (a restart is already a rare event and the
    Mongo cache is the durable source for verdicts themselves).
    """

    agent_id: str
    state_store: StateStore
    caller: VerdictCaller
    freshness_s: float = DEFAULT_FRESHNESS_S
    # Wall-clock for tier/freshness math; monotonic for rate limits.
    _now_wall: Callable[[], datetime] = field(default_factory=lambda: lambda: datetime.now(UTC))
    _now_mono: Callable[[], float] = field(default_factory=lambda: time.monotonic)
    _last_fire: dict[Trigger, _LastFire] = field(default_factory=dict)
    _gate_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def get_verdict(
        self,
        *,
        idea: str | dict[str, Any],
        tier: Tier = "basic",
        trigger: Trigger = "entry_gate",
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        """Return a verdict — cache-first unless ``force_refresh``.

        Raises :class:`RateLimitedError` if the trigger would breach its
        ceiling. The caller (runtime / scheduler) decides whether to
        suppress or surface the error — we never silently no-op.
        """
        key = idea_hash(idea)
        if not force_refresh:
            cached = await self.state_store.get_verdict_cache(self.agent_id, key)
            if cached is not None and self._is_fresh(cached):
                logger.info(
                    "oracle.cache_hit agent_id=%s idea_hash=%s tier=%s trigger=%s",
                    self.agent_id,
                    key,
                    cached.tier,
                    trigger,
                )
                await emit_event(
                    "cache.hit",
                    {
                        "agent_id": self.agent_id,
                        "idea_hash": key,
                        "tier": cached.tier,
                        "trigger": trigger,
                    },
                )
                return cached.verdict
        # Cache miss (either no entry or stale). Record before charging.
        await emit_event(
            "cache.miss",
            {
                "agent_id": self.agent_id,
                "idea_hash": key,
                "tier": tier,
                "trigger": trigger,
                "force_refresh": force_refresh,
            },
        )

        # Cache miss / forced refresh — check rate limit before charging.
        self._enforce_rate_limit(trigger)

        async with self._gate_lock:
            # Re-check the cache inside the lock so a stampede of
            # entry-gate calls for the same idea collapses to one MCP
            # call. Common case on a hot strategy: many events, one idea.
            if not force_refresh:
                cached = await self.state_store.get_verdict_cache(self.agent_id, key)
                if cached is not None and self._is_fresh(cached):
                    return cached.verdict

            logger.info(
                "oracle.charge agent_id=%s idea_hash=%s tier=%s trigger=%s",
                self.agent_id,
                key,
                tier,
                trigger,
            )
            verdict = await self.caller(idea=idea, tier=tier, agent_id=self.agent_id)

        await self.state_store.upsert_verdict_cache(
            AgentVerdictCacheEntry(
                agent_id=self.agent_id,
                idea_hash=key,
                cached_at=self._now_wall(),
                tier=tier,
                verdict=verdict,
            )
        )
        self._mark_fired(trigger)
        return verdict

    def _is_fresh(self, entry: AgentVerdictCacheEntry) -> bool:
        cached_at = entry.cached_at
        if cached_at.tzinfo is None:
            cached_at = cached_at.replace(tzinfo=UTC)
        age = (self._now_wall() - cached_at).total_seconds()
        return age < self.freshness_s

    def _enforce_rate_limit(self, trigger: Trigger) -> None:
        ceiling = RATE_LIMITS_S.get(trigger)
        if ceiling is None:
            return
        last = self._last_fire.get(trigger)
        if last is None:
            return
        elapsed = self._now_mono() - last.monotonic
        if elapsed < ceiling:
            raise RateLimitedError(
                f"trigger={trigger} fired {elapsed:.0f}s ago; ceiling is {ceiling:.0f}s"
            )

    def _mark_fired(self, trigger: Trigger) -> None:
        self._last_fire[trigger] = _LastFire(monotonic=self._now_mono())


class RateLimitedError(Exception):
    """Raised when a manual or circuit-breaker re-verdict would breach
    the trigger's cadence ceiling."""


def make_rest_caller(
    client: Any,
    *,
    protocol: str,
    vertical: str = "dex",
) -> VerdictCaller:
    """Adapt a :class:`GeckoOracleClient` to the :class:`VerdictCaller`
    Protocol.

    The runtime / cache layer thinks in ``(idea, tier, agent_id)``; the
    REST client thinks in ``(idea, protocol, vertical, tier)``. The
    ``protocol`` + ``vertical`` are loaded from the agent spec at CLI
    boot and bound here once.

    ``client`` is typed ``Any`` to avoid a hard import-cycle between
    ``oracle.py`` and ``oracle_client.py``; the CLI passes a real
    ``GeckoOracleClient`` instance.
    """

    async def _caller(*, idea: str | dict[str, Any], tier: Tier, agent_id: str) -> dict[str, Any]:
        # The cache key is computed upstream from the raw idea (dict or
        # str); the REST surface accepts a string only, so stringify.
        if isinstance(idea, dict):
            idea_str = " ".join(f"{k}={v}" for k, v in sorted(idea.items()))
        else:
            idea_str = idea
        verdict = await client.call(
            idea=idea_str,
            protocol=protocol,
            vertical=vertical,
            tier=tier,
        )
        dumped: dict[str, Any] = verdict.model_dump(mode="json")
        return dumped

    return _caller


def make_stub_caller(
    response: dict[str, Any] | Callable[..., Awaitable[dict[str, Any]]] | None = None,
) -> VerdictCaller:
    """Test helper. Returns a :class:`VerdictCaller` that always yields
    ``response`` (or a default ``act`` verdict if unset).
    """

    default = response or {
        "verdict": "act",
        "confidence": 0.6,
        "dissent": [],
        "citations": [],
    }

    async def _caller(*, idea: str | dict[str, Any], tier: Tier, agent_id: str) -> dict[str, Any]:
        if callable(default):
            return await default(idea=idea, tier=tier, agent_id=agent_id)  # type: ignore[no-any-return]
        return dict(default)

    return _caller


__all__ = [
    "DEFAULT_FRESHNESS_S",
    "RATE_LIMITS_S",
    "OracleWrapper",
    "RateLimitedError",
    "Tier",
    "Trigger",
    "VerdictCaller",
    "idea_hash",
    "make_rest_caller",
    "make_stub_caller",
]
