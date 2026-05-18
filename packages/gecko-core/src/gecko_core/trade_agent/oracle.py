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
import os
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol

from gecko_core.observability import emit_event
from gecko_core.observability.events import events_collection
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


# ---------------------------------------------------------------------------
# Semantic idea-hash (S24 WS-F #1).
#
# Two queries asking "deposit USDC into Kamino reserve right now?" and
# "should I put my USDC into Kamino reserve today?" must hit the same key.
# We normalise five dimensions: protocol, vertical, intent, size_bucket,
# horizon_bucket. Anything missing collapses to "_" so the hash is still
# stable. Synonym tables are deliberately small — this is dimensional
# normalisation, not NLP.
# ---------------------------------------------------------------------------

_PROTOCOL_SYNONYMS: dict[str, str] = {
    "kamino": "kamino",
    "kamino reserve": "kamino",
    "kamino lend": "kamino",
    "jupiter": "jupiter",
    "jup": "jupiter",
    "jito": "jito",
    "marginfi": "marginfi",
    "mfi": "marginfi",
    "drift": "drift",
    "raydium": "raydium",
    "ray": "raydium",
    "orca": "orca",
    "paysh": "paysh",
    "bazaar": "bazaar",
}

_VERTICAL_SYNONYMS: dict[str, str] = {
    "dex": "dex",
    "amm": "dex",
    "swap": "dex",
    "perp": "perps",
    "perps": "perps",
    "futures": "perps",
    "lend": "lending",
    "lending": "lending",
    "loan": "lending",
    "borrow": "lending",
    "deposit": "lending",
    "lst": "lst",
    "staking": "lst",
    "stake": "lst",
    "liquid staking": "lst",
    "infra": "infra",
}

_INTENT_SYNONYMS: dict[str, str] = {
    "long": "long",
    "buy": "long",
    "open long": "long",
    "go long": "long",
    "short": "short",
    "sell": "short",
    "open short": "short",
    "go short": "short",
    "deposit": "deposit",
    "put": "deposit",
    "supply": "deposit",
    "lend": "deposit",
    "withdraw": "withdraw",
    "exit": "withdraw",
    "redeem": "withdraw",
    "stake": "stake",
    "staking": "stake",
    "liquid staking": "stake",
    "unstake": "unstake",
    "swap": "swap",
    "trade": "swap",
}

# When an intent implies a vertical (e.g. "deposit" => "lending"), use this
# table to fill the vertical dimension if the text didn't mention one
# explicitly. Lets paraphrase pairs like "lend USDC on MarginFi" vs.
# "supply USDC to MFI" collapse to the same hash even though only one side
# carries the literal vertical token.
_INTENT_TO_VERTICAL: dict[str, str] = {
    "long": "perps",
    "short": "perps",
    "deposit": "lending",
    "withdraw": "lending",
    "stake": "lst",
    "unstake": "lst",
    "swap": "dex",
}

# Horizon tokens collapsed to coarse buckets. Anything matching "now",
# "today", "current", "right now" collapses to "now".
_HORIZON_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(right now|now|today|currently|at the moment)\b", re.I), "now"),
    (re.compile(r"\b(intraday|hours?)\b", re.I), "intraday"),
    (re.compile(r"\b(next few days|few days|days?|short[\s-]term)\b", re.I), "days"),
    (re.compile(r"\b(this week|this month|weeks?|near[\s-]term)\b", re.I), "weeks"),
    (re.compile(r"\b(months?|long[\s-]term|year)\b", re.I), "months"),
)

# Size tokens. "$X USDC" or "X% of bankroll" or "small / medium / large".
_SIZE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(tiny|small|sm)\b", re.I), "small"),
    (re.compile(r"\b(medium|med|moderate)\b", re.I), "medium"),
    (re.compile(r"\b(large|big|max|full|all[\s-]in)\b", re.I), "large"),
)


_PUNCT_RE = re.compile(r"[^a-z0-9%$\s-]+")


def _normalize_text(text: str) -> str:
    """Lowercase + strip punctuation so synonym lookup is word-boundary safe.

    Hyphens preserved so multi-word tokens like ``"liquid staking"`` and
    ``"short-term"`` keep their shape; ``%`` and ``$`` preserved for size
    parsing downstream.
    """
    return _PUNCT_RE.sub(" ", text.lower())


def _normalize_token(text: str, table: dict[str, str]) -> str | None:
    """Match the longest synonym key whole-word in ``text``. None if no match."""
    lower = " " + _normalize_text(text) + " "
    best: tuple[int, str] | None = None
    for key, canon in table.items():
        if (f" {key} " in lower) and (best is None or len(key) > best[0]):
            best = (len(key), canon)
    return best[1] if best else None


def _semantic_dimensions(idea: str) -> dict[str, str]:
    """Extract the 5 normalised dimensions used for the cache key.

    Returns a dict with stable keys; missing dimensions collapse to ``"_"``
    so the hash is deterministic even on under-specified ideas.

    Vertical inference: when the text doesn't carry an explicit vertical
    token but the intent implies one (e.g. ``deposit`` => lending), the
    intent fills the vertical dimension. This lets paraphrases like
    "lend USDC on MarginFi" vs. "supply USDC to MFI" collapse to the
    same hash without forcing every caller to spell out the vertical.
    """
    text = _normalize_text(idea)
    protocol = _normalize_token(text, _PROTOCOL_SYNONYMS) or "_"
    vertical = _normalize_token(text, _VERTICAL_SYNONYMS) or "_"
    intent = _normalize_token(text, _INTENT_SYNONYMS) or "_"
    if vertical == "_" and intent in _INTENT_TO_VERTICAL:
        vertical = _INTENT_TO_VERTICAL[intent]

    horizon = "_"
    for pat, label in _HORIZON_PATTERNS:
        if pat.search(text):
            horizon = label
            break

    size = "_"
    for pat, label in _SIZE_PATTERNS:
        if pat.search(text):
            size = label
            break
    # Numeric size: "100 USDC", "5%". Coarse bucket on USD-ish magnitude.
    if size == "_":
        m = re.search(r"\$?\s*([0-9][0-9,_]*\.?[0-9]*)\s*(usdc|usd|sol|%)?", text, re.I)
        if m:
            try:
                val = float(m.group(1).replace(",", "").replace("_", ""))
                unit = (m.group(2) or "").lower()
                if unit == "%":
                    size = "small" if val < 5 else "medium" if val < 25 else "large"
                else:
                    size = "small" if val < 1_000 else "medium" if val < 25_000 else "large"
            except ValueError:
                pass

    return {
        "protocol": protocol,
        "vertical": vertical,
        "intent": intent,
        "size_bucket": size,
        "horizon_bucket": horizon,
    }


def idea_hash(idea: str | dict[str, Any]) -> str:
    """Stable, semantically-normalised cache key for an idea.

    For string ideas: extract five normalised dimensions
    ``(protocol, vertical, intent, size_bucket, horizon_bucket)``, render
    them as a fixed-key canonical form, and SHA-256. Two paraphrases of
    the same query collapse to the same hash.

    For dict ideas: canonical JSON (sorted keys) of the dict — the caller
    has already imposed structure, we just stabilise the encoding.

    32-hex-char return for compactness in Mongo indexes.
    """
    if isinstance(idea, dict):
        import json as _json

        canonical = _json.dumps(idea, sort_keys=True, default=str)
    else:
        dims = _semantic_dimensions(idea)
        # Fixed-key order — never alphabetical, never derived from data.
        canonical = (
            f"protocol={dims['protocol']}|"
            f"vertical={dims['vertical']}|"
            f"intent={dims['intent']}|"
            f"size={dims['size_bucket']}|"
            f"horizon={dims['horizon_bucket']}"
        )
    return hashlib.sha256(canonical.encode()).hexdigest()[:32]


# ---------------------------------------------------------------------------
# Global per-process panel semaphore (S24 WS-F #2).
#
# The 8 is a deliberate magic number: OpenAI's per-org default is 500 RPM
# on gpt-4o-mini. Each Pro panel call fires ~7 LLM calls (one per voice).
# A burst of 8 in-flight panels = 56 concurrent OpenAI calls; even with the
# trade-panel staggering of round-robin voices, we want comfortable head-
# room under the per-minute ceiling. Tunable via env for ops emergencies.
#
# Per-process, not per-agent: we want ALL agents on one ECS task to share
# the same OpenAI quota budget. Cluster-wide back-pressure rides on the
# 429-backoff path below.
# ---------------------------------------------------------------------------

PANEL_CONCURRENCY = int(os.environ.get("GECKO_PANEL_CONCURRENCY", "8"))
_PANEL_SEMAPHORE: asyncio.Semaphore | None = None


def get_panel_semaphore() -> asyncio.Semaphore:
    """Lazy-init the global panel semaphore.

    Async semaphores must be created inside the running loop in older
    asyncio versions; deferring until first use avoids "got Future
    attached to a different loop" errors in tests.
    """
    global _PANEL_SEMAPHORE
    if _PANEL_SEMAPHORE is None:
        _PANEL_SEMAPHORE = asyncio.Semaphore(PANEL_CONCURRENCY)
    return _PANEL_SEMAPHORE


def reset_panel_semaphore_for_tests() -> None:
    """Test helper — clear the cached semaphore between event loops."""
    global _PANEL_SEMAPHORE
    _PANEL_SEMAPHORE = None


# ---------------------------------------------------------------------------
# 429 backoff (S24 WS-F #3).
#
# Caller delegates to ``call_with_429_backoff`` which retries the inner
# awaitable with exponential delay on OpenAI rate-limit errors. Detection
# is duck-typed: we look for ``status_code == 429`` or "rate limit" in the
# error message — works against both raw httpx errors and the OpenAI SDK's
# RateLimitError without importing the SDK at module scope.
# ---------------------------------------------------------------------------

_BACKOFF_DELAYS_S: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0, 16.0)


def _is_rate_limit_error(exc: BaseException) -> bool:
    if getattr(exc, "status_code", None) == 429:
        return True
    if getattr(exc, "status", None) == 429:
        return True
    name = exc.__class__.__name__.lower()
    if "ratelimit" in name:
        return True
    msg = str(exc).lower()
    return "rate limit" in msg or "429" in msg


async def call_with_429_backoff(
    fn: Callable[[], Awaitable[Any]],
    *,
    agent_id: str | None = None,
    tier: str | None = None,
) -> Any:
    """Invoke ``fn`` with exponential backoff on 429s.

    Emits :code:`oracle.rate_limit_hit` per retry attempt so the WS-D
    cost-ledger can correlate spikes with model-route fan-in. The final
    attempt re-raises; the caller treats that as a hard failure.
    """
    last_exc: BaseException | None = None
    for attempt, delay in enumerate((0.0, *_BACKOFF_DELAYS_S)):
        if delay > 0:
            await asyncio.sleep(delay)
        try:
            return await fn()
        except Exception as exc:  # duck-typed 429 detection
            if not _is_rate_limit_error(exc):
                raise
            last_exc = exc
            await emit_event(
                "oracle.rate_limit_hit",
                {
                    "agent_id": agent_id,
                    "tier": tier,
                    "attempt": attempt + 1,
                    "next_delay_s": (
                        _BACKOFF_DELAYS_S[attempt] if attempt < len(_BACKOFF_DELAYS_S) else None
                    ),
                    "error": exc.__class__.__name__,
                },
            )
    # All retries exhausted — re-raise the last 429.
    assert last_exc is not None
    raise last_exc


# ---------------------------------------------------------------------------
# Per-agent daily spend cap (S24 WS-F #4).
# ---------------------------------------------------------------------------

DEFAULT_AGENT_DAILY_USD_CAP = 3.00


def agent_daily_usd_cap() -> float:
    raw = os.environ.get("GECKO_AGENT_DAILY_USD_CAP")
    if not raw:
        return DEFAULT_AGENT_DAILY_USD_CAP
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_AGENT_DAILY_USD_CAP


async def _sum_daily_oracle_spend(agent_id: str) -> float:
    """Sum ``payload.total_usd`` from ``oracle.cost_usd`` events for
    ``agent_id`` in the last 24h. Returns 0.0 when events collection is
    unavailable — fail-open, never block on missing ledger."""
    coll = events_collection()
    if coll is None:
        return 0.0
    since = datetime.now(UTC) - timedelta(hours=24)
    total = 0.0
    try:
        cursor = coll.find(
            {
                "event": "oracle.cost_usd",
                "ts": {"$gte": since},
                "payload.agent_id": agent_id,
            }
        )
        async for doc in cursor:
            try:
                total += float(doc.get("payload", {}).get("total_usd") or 0.0)
            except (TypeError, ValueError):
                continue
    except Exception as exc:  # never block on observability failure
        logger.warning("oracle.spend_cap_lookup_failed agent_id=%s err=%s", agent_id, exc)
        return 0.0
    return total


class SpendCapExceededError(Exception):
    """Raised when an agent's 24h oracle spend would breach the daily cap.

    The runtime catches this, halts the agent, and journals a
    ``circuit_breaker_trip`` with reason ``daily_spend_cap``. Recovery is
    operator-only.
    """


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

        # Daily-spend cap — refuse the panel call when 24h oracle spend
        # exceeds GECKO_AGENT_DAILY_USD_CAP (default $3). Hard halt —
        # operator must intervene to clear.
        cap = agent_daily_usd_cap()
        spent_24h = await _sum_daily_oracle_spend(self.agent_id)
        if spent_24h >= cap:
            await emit_event(
                "agent.spend_cap_hit",
                {
                    "agent_id": self.agent_id,
                    "idea_hash": key,
                    "tier": tier,
                    "trigger": trigger,
                    "spent_24h_usd": round(spent_24h, 6),
                    "cap_usd": cap,
                },
            )
            raise SpendCapExceededError(
                f"agent_id={self.agent_id} 24h oracle spend ${spent_24h:.4f} "
                f">= cap ${cap:.2f}; refusing panel call"
            )

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
            # Bound concurrent panel charges per-process (OpenAI RPM
            # headroom). Wrapping inside the cache lock means the
            # cache-stampede collapse still works; the lock + semaphore
            # together bound BOTH same-idea fanout AND cross-idea
            # cross-agent fanout. 429 backoff fires inside the caller.
            sem = get_panel_semaphore()

            async def _do_call() -> dict[str, Any]:
                return await self.caller(idea=idea, tier=tier, agent_id=self.agent_id)

            async with sem:
                verdict = await call_with_429_backoff(_do_call, agent_id=self.agent_id, tier=tier)

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
        # S35-#99 — verdict envelope split into two cite lists.
        "evidence_citations": [],
        "framework_context": [],
    }

    async def _caller(*, idea: str | dict[str, Any], tier: Tier, agent_id: str) -> dict[str, Any]:
        if callable(default):
            return await default(idea=idea, tier=tier, agent_id=agent_id)  # type: ignore[no-any-return]
        return dict(default)

    return _caller


__all__ = [
    "DEFAULT_AGENT_DAILY_USD_CAP",
    "DEFAULT_FRESHNESS_S",
    "PANEL_CONCURRENCY",
    "RATE_LIMITS_S",
    "OracleWrapper",
    "RateLimitedError",
    "SpendCapExceededError",
    "Tier",
    "Trigger",
    "VerdictCaller",
    "agent_daily_usd_cap",
    "call_with_429_backoff",
    "get_panel_semaphore",
    "idea_hash",
    "make_rest_caller",
    "make_stub_caller",
    "reset_panel_semaphore_for_tests",
]
