"""Trade-agent runtime — one async event loop per agent.

Wires the spec → mode evaluator → oracle (cache-then-charge) → exec
adapter (advisor mode bypasses exec) → journal pipeline. Also owns the
hot-swap mechanic for in-flight spec version bumps.

Hot-swap design (per founder decision #3 in the v0.1 decisions memo):

The new ``AgentRuntime`` for spec v2 is started in parallel by the
caller (CLI / API). It runs in "shadow" status and:

1. Reads the existing ``agent_state`` doc for the agent_id.
2. Verifies the spec_version is *higher* than what's running (CAS on
   ``spec_version``).
3. Snapshots open positions from ``agent_positions`` (no copy — same
   docs, same position_ids).
4. Atomically flips ``agent_state.spec_version`` + ``spec_snapshot`` +
   ``spec_fingerprint`` via a single ``upsert_agent_state`` call.
5. Journals the swap; old runtime sees the version drift on its next
   tick and exits cleanly (drain).

On any step failure between 2-4, the new runtime aborts and the old
keeps running — no orphan state. The "atomic" claim is single-doc
Mongo upsert, which is atomic in Mongo's consistency model.

Journal writes are non-blocking on the hot path: ``tick`` enqueues
journal entries on a bounded queue; a background task drains them. On
queue overflow (>100 pending entries) the runtime drops the oldest and
logs a warning — better to lose stale events than block trading.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from gecko_core.observability import emit_event
from gecko_core.trade_agent.modes import AdvisorMode, TraderMode
from gecko_core.trade_agent.oracle import OracleWrapper, RateLimitedError
from gecko_core.trade_agent.primitives import (
    RiskState,
    compute_size_usd,
    passes_filter,
    would_breach,
)
from gecko_core.trade_agent.scheduler import Scheduler
from gecko_core.trade_agent.spec import AgentSpec
from gecko_core.trade_agent.state.models import (
    AgentJournalEntry,
    AgentMode,
    AgentState,
    AgentStatus,
)
from gecko_core.trade_agent.state.mongo import StateStore

logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_S = 30.0
STALE_HEARTBEAT_THRESHOLD_S = 120.0
JOURNAL_QUEUE_MAX = 100


HotpathEventHandler = Callable[[dict[str, Any]], Awaitable[None]]


class RuntimeError_(Exception):
    """Typed runtime error — never raise bare Exception."""


@dataclass
class AgentRuntime:
    """One asyncio event loop per agent."""

    agent_id: str
    spec: AgentSpec
    mode: AgentMode
    state_store: StateStore
    oracle: OracleWrapper
    exec_adapter: Any | None = None  # ExecAdapter; None for advisor-mode
    user_wallet: str | None = None
    execution_rail: str | None = None
    bankroll_usd: float = 1000.0

    # internal
    _journal_q: asyncio.Queue[AgentJournalEntry] = field(
        default_factory=lambda: asyncio.Queue(maxsize=JOURNAL_QUEUE_MAX)
    )
    _journal_task: asyncio.Task[None] | None = None
    _heartbeat_task: asyncio.Task[None] | None = None
    _scheduler: Scheduler = field(default_factory=Scheduler)
    _stop: asyncio.Event = field(default_factory=asyncio.Event)
    _status: AgentStatus = "starting"
    _advisor: AdvisorMode | None = None
    _trader: TraderMode | None = None

    def __post_init__(self) -> None:
        if self.mode == "advisor":
            self._advisor = AdvisorMode(spec=self.spec)
        elif self.mode == "trader":
            self._trader = TraderMode(spec=self.spec)
        else:  # pragma: no cover — Pydantic literal enforces this
            raise RuntimeError_(f"unknown mode: {self.mode!r}")

    # -- lifecycle ----------------------------------------------------

    async def start(self) -> None:
        """Boot the agent. Resumable on cold restart.

        On entry, looks up any existing ``agent_state`` for the agent_id;
        if status was "running" with a stale heartbeat, transitions to
        "resuming" and replays open positions (just logs them — exec
        replay is out of scope for SE-1).
        """
        existing = await self.state_store.get_agent_state(self.agent_id)
        if existing is not None and existing.status == "running":
            stale = (
                datetime.now(UTC) - existing.last_heartbeat_at
            ).total_seconds() > STALE_HEARTBEAT_THRESHOLD_S
            if stale:
                self._status = "resuming"
                await self._enqueue_journal(
                    "heartbeat_stale",
                    {"last_heartbeat_at": existing.last_heartbeat_at.isoformat()},
                )
                open_pos = await self.state_store.list_open_positions(self.agent_id)
                await self._enqueue_journal("agent_resumed", {"open_positions": len(open_pos)})

        snapshot = self.spec.model_dump(mode="json")
        state = AgentState(
            agent_id=self.agent_id,
            spec_id=self.spec.spec_id,
            spec_version=self.spec.version,
            spec_fingerprint=self.spec.fingerprint(),
            mode=self.mode,
            status="running",
            user_wallet=self.user_wallet,
            execution_rail=self.execution_rail,
            spec_snapshot=snapshot,
            last_heartbeat_at=datetime.now(UTC),
        )
        await self.state_store.upsert_agent_state(state)
        self._status = "running"

        self._journal_task = asyncio.create_task(
            self._drain_journal(), name=f"trade_agent.{self.agent_id}.journal"
        )
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(), name=f"trade_agent.{self.agent_id}.heartbeat"
        )
        await self._scheduler.start()
        # Scheduled 24h refresh of the cached verdict baseline.
        self._scheduler.schedule(
            "verdict_refresh",
            interval_s=60 * 60 * 24,
            callback=self._scheduled_refresh,
        )
        # Scheduled hotpath-snapshot write (5min cadence; TTL is 5min).
        # Stub source — real Helius/Pyth wiring lands in W3-1. The point
        # of writing now is to exercise the validator-gated TTL collection
        # so backtest replay + observability surfaces have data.
        self._scheduler.schedule(
            "hotpath_snapshot_tick",
            interval_s=60 * 5,
            callback=self._scheduled_hotpath_snapshot,
        )
        await self._enqueue_journal("agent_started", {"mode": self.mode})

    async def stop(self) -> None:
        self._stop.set()
        await self._scheduler.stop()
        await self._enqueue_journal("agent_stopped", {})
        # Drain pending journal before flipping status.
        await self._journal_q.join()
        if self._journal_task is not None:
            self._journal_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._journal_task
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._heartbeat_task
        await self.state_store.update_status(self.agent_id, "stopped")
        self._status = "stopped"

    async def pause(self) -> None:
        await self.state_store.update_status(self.agent_id, "paused")
        self._status = "paused"
        await self._enqueue_journal("agent_paused", {})

    @property
    def status(self) -> AgentStatus:
        return self._status

    # -- event handling ----------------------------------------------

    async def tick(self, event: dict[str, Any]) -> None:
        """Process one hot-path event.

        Never blocks on Mongo for the journal — the queue absorbs writes.
        Oracle calls may block (they're cache-first; the network charge
        is the rare path) but the cache lookup itself is async-fast.
        """
        # In paused mode, advisor still surfaces opportunities; trader-mode
        # entries are vetoed at the tick boundary.
        if self._status == "paused" and self.mode == "trader":
            return

        # S24 WS-D — per-tick global telemetry. Wallet may be None for
        # system-owned agents; that's expected and the wallet index is
        # sparse so we don't bloat the ledger.
        await emit_event(
            "agent.tick",
            {"agent_id": self.agent_id, "mode": self.mode},
            wallet=self.user_wallet,
        )

        if self.mode == "advisor":
            assert self._advisor is not None
            candidate = self._advisor.evaluate(event)
            if candidate is None:
                return
            await self._enqueue_journal(
                "opportunity",
                {
                    "mint": candidate.mint,
                    "idea": candidate.idea,
                    "rule_id": candidate.rule_id,
                },
            )
            await emit_event(
                "agent.opportunity",
                {
                    "agent_id": self.agent_id,
                    "mint": candidate.mint,
                    "idea": candidate.idea,
                    "rule_id": candidate.rule_id,
                },
                wallet=self.user_wallet,
            )
            return

        # trader mode — v0.1 raises at evaluate(); guard so a user
        # accidentally booting trader still sees a clean error.
        assert self._trader is not None
        candidate = self._trader.evaluate(event)
        if candidate is None:
            return

        # The remainder of this branch is dead in v0.1 (the evaluate call
        # above raises). We keep it scaffolded so AIML-2 only has to flip
        # the evaluator — the runtime wiring is already correct.
        risk_state = RiskState(
            open_positions=len(await self.state_store.list_open_positions(self.agent_id)),
            bankroll_usd=self.bankroll_usd,
            daily_loss_pct=0.0,
        )
        breach = would_breach(self.spec.risk, risk_state, candidate)
        if breach is not None:
            await self._enqueue_journal(
                "circuit_breaker_trip",
                {"reason": breach, "mint": candidate.mint},
            )
            return

        verdict = await self.oracle.get_verdict(
            idea=candidate.idea, tier="basic", trigger="entry_gate"
        )
        ok, reason = passes_filter(self.spec.filter, event, verdict=verdict)
        if not ok:
            await self._enqueue_journal(
                "circuit_breaker_trip", {"reason": reason, "mint": candidate.mint}
            )
            return

        size_usd = compute_size_usd(
            self.spec.sizing, bankroll_usd=self.bankroll_usd, verdict=verdict
        )
        if size_usd <= 0:
            return
        if self.exec_adapter is None:
            await self._enqueue_journal(
                "exec_error", {"reason": "no_adapter", "mint": candidate.mint}
            )
            await emit_event(
                "agent.error",
                {
                    "agent_id": self.agent_id,
                    "kind": "exec_error",
                    "reason": "no_adapter",
                    "mint": candidate.mint,
                },
                wallet=self.user_wallet,
            )
            return
        receipt = await self.exec_adapter.submit(
            mint=candidate.mint, side=candidate.side, size_usd=size_usd
        )
        await self._enqueue_journal(
            "entry",
            {
                "mint": candidate.mint,
                "size_usd": size_usd,
                "verdict": verdict,
                "receipt": receipt,
            },
        )

    # -- hot-swap ---------------------------------------------------

    async def hot_swap_to(self, new_spec: AgentSpec) -> None:
        """Atomically replace this runtime's spec with a higher version.

        Raises :class:`RuntimeError_` if ``new_spec.version`` is not
        strictly greater (lexicographic on the version triple suffices
        for v0.1 — a real semver comparator lands in v0.2).

        The swap is single-doc Mongo upsert; on success, the runtime's
        in-memory spec + evaluators are replaced before the next tick.
        """
        if not _version_gt(new_spec.version, self.spec.version):
            raise RuntimeError_(
                f"hot_swap requires version > current; "
                f"got {new_spec.version!r} <= {self.spec.version!r}"
            )

        existing = await self.state_store.get_agent_state(self.agent_id)
        if existing is None:
            raise RuntimeError_(f"agent_state not found for {self.agent_id!r}")

        open_pos = await self.state_store.list_open_positions(self.agent_id)
        old_version = self.spec.version

        new_state = existing.model_copy(
            update={
                "spec_id": new_spec.spec_id,
                "spec_version": new_spec.version,
                "spec_fingerprint": new_spec.fingerprint(),
                "spec_snapshot": new_spec.model_dump(mode="json"),
                "last_heartbeat_at": datetime.now(UTC),
            }
        )
        await self.state_store.upsert_agent_state(new_state)

        # Swap in-memory after persistence so a crash mid-swap leaves
        # the on-disk truth ahead, which is the recoverable direction.
        self.spec = new_spec
        if self.mode == "advisor":
            self._advisor = AdvisorMode(spec=new_spec)
        else:
            self._trader = TraderMode(spec=new_spec)

        await self._enqueue_journal(
            "spec_swap",
            {
                "from_version": old_version,
                "to_version": new_spec.version,
                "open_positions_preserved": len(open_pos),
            },
        )

    # -- internals --------------------------------------------------

    async def _heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.state_store.update_heartbeat(self.agent_id, datetime.now(UTC))
            except Exception:
                logger.exception("heartbeat.failed agent_id=%s", self.agent_id)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=HEARTBEAT_INTERVAL_S)
            except TimeoutError:
                continue

    async def _drain_journal(self) -> None:
        while True:
            entry = await self._journal_q.get()
            try:
                await self.state_store.write_journal(entry)
            except Exception:
                logger.exception(
                    "journal.write_failed agent_id=%s event=%s",
                    entry.agent_id,
                    entry.event,
                )
            finally:
                self._journal_q.task_done()

    async def _enqueue_journal(self, event: str, payload: dict[str, Any]) -> None:
        entry = AgentJournalEntry(
            agent_id=self.agent_id,
            ts=datetime.now(UTC),
            event=event,
            payload=payload,
        )
        try:
            self._journal_q.put_nowait(entry)
        except asyncio.QueueFull:
            # Drop the oldest to keep latency bounded; log the drop.
            with contextlib.suppress(asyncio.QueueEmpty):
                _ = self._journal_q.get_nowait()
                self._journal_q.task_done()
            logger.warning("journal.queue_full agent_id=%s dropped_oldest=1", self.agent_id)
            with contextlib.suppress(asyncio.QueueFull):
                self._journal_q.put_nowait(entry)

    async def _scheduled_hotpath_snapshot(self) -> None:
        """Write a stub snapshot doc per protocol.

        Real upstream is the Helius WS feed (ticket W3-1). The stub
        payload is enough to exercise the write path + TTL index. Failure
        is non-fatal — never block trading on observability writes.
        """
        proto = getattr(self.spec, "protocol", "unknown") or "unknown"
        snapshot = {
            "source": "stub",
            "spec_version": self.spec.version,
            "mode": self.mode,
            "status": self._status,
        }
        try:
            await self.state_store.write_hotpath_snapshot(self.agent_id, proto, snapshot)
        except Exception:
            logger.exception(
                "hotpath_snapshot.write_failed agent_id=%s protocol=%s",
                self.agent_id,
                proto,
            )

    async def _scheduled_refresh(self) -> None:
        # Best-effort scheduled re-verdict on the *session* idea. Cache
        # write happens inside oracle.get_verdict.
        try:
            await self.oracle.get_verdict(
                idea=f"session:{self.spec.name}",
                tier="basic",
                trigger="scheduled",
                force_refresh=True,
            )
            await self._enqueue_journal("verdict_called", {"trigger": "scheduled", "tier": "basic"})
        except RateLimitedError as exc:
            logger.info("oracle.rate_limited trigger=scheduled %s", exc)


def _version_gt(a: str, b: str) -> bool:
    """Compare semver triples (no pre-release suffix). True if a > b."""

    def _parse(v: str) -> tuple[int, int, int]:
        parts = v.split(".")
        if len(parts) != 3:
            raise ValueError(f"bad version: {v!r}")
        return (int(parts[0]), int(parts[1]), int(parts[2]))

    return _parse(a) > _parse(b)


# Public alias — "TradeAgent" reads better in CLI / docs than "AgentRuntime"
# even though the internal name stays AgentRuntime.
TradeAgent = AgentRuntime


__all__ = ["AgentRuntime", "RuntimeError_", "TradeAgent"]
