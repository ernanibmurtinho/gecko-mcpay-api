"""``bb trade-agent`` Click subcommands.

Thin Click wrappers. All business logic lives in :mod:`.runtime` and
sibling modules; this file is parse-args / call-core / format-output.

Five subcommands per the SE-1 / SE-2 split:

* ``up`` — start an agent in the foreground (v0.1).
* ``ls`` — list agents in ``agent_state``.
* ``inspect`` — tail journal + open positions + last verdict.
* ``stop`` — graceful shutdown.
* ``pause`` — existing positions managed, no new entries.

The CLI runs against the live :class:`MongoStateStore` when
``MONGODB_URI`` is set; otherwise it errors out (``up`` would have no
durable journal). Tests do not exercise the CLI's Mongo branch directly
— see ``test_runtime_lifecycle.py`` for the runtime contract.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path
from typing import Any

import click

from gecko_core.trade_agent.exec_adapters import ExecAdapterError, get_adapter
from gecko_core.trade_agent.oracle import (
    OracleWrapper,
    make_rest_caller,
    make_stub_caller,
)
from gecko_core.trade_agent.oracle_client import GeckoOracleClient
from gecko_core.trade_agent.runtime import AgentRuntime
from gecko_core.trade_agent.spec import SpecValidationError, load_spec
from gecko_core.trade_agent.state.mongo import (
    InMemoryStateStore,
    MongoStateStore,
    StateStore,
)


def _resolve_state_store() -> StateStore:
    """Build the production state store; fail loud if Mongo is missing."""
    uri = os.environ.get("MONGODB_URI") or os.environ.get("MONGO_URI")
    if not uri:
        raise click.ClickException(
            "MONGODB_URI is not set. The trade-agent runtime requires Mongo "
            "for durable journal/state. Set MONGODB_URI or run with "
            "GECKO_TRADE_AGENT_INMEMORY=1 for dev-only ephemeral state."
        )
    if os.environ.get("GECKO_TRADE_AGENT_INMEMORY") == "1":
        return InMemoryStateStore()
    return MongoStateStore(uri)


@click.group(name="trade-agent")
def trade_agent_cmd() -> None:
    """Long-running self-hosted trade-agent runtime.

    Loads a coach-emitted strategy spec, listens to hot-path events, and
    journals advisor opportunities (or executes in trader mode, v0.2).
    """


@trade_agent_cmd.command("up")
@click.option(
    "--spec",
    "spec_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Path to the coach-emitted strategy JSON.",
)
@click.option(
    "--mode",
    type=click.Choice(["advisor", "trader"]),
    default="advisor",
    show_default=True,
    help="Runtime mode. v0.1 ships advisor; trader is a stub.",
)
@click.option(
    "--exec-rail",
    type=click.Choice(["okx", "okx-agentic-wallet", "sendai", "backpack"]),
    default=None,
    help="Execution rail. Required for trader mode; ignored in advisor.",
)
@click.option(
    "--agent-id",
    default=None,
    help="Override the agent id (defaults to a fresh uuid4 prefix).",
)
@click.option(
    "--user-wallet",
    default=None,
    help="Owner wallet — surfaced in `ls --user-wallet <addr>`.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help=(
        "Use a stub oracle caller (always returns 'act' verdict). No "
        "network I/O against api.geckovision.tech. For local smoke runs."
    ),
)
def up_cmd(
    spec_path: Path,
    mode: str,
    exec_rail: str | None,
    agent_id: str | None,
    user_wallet: str | None,
    dry_run: bool,
) -> None:
    """Start an agent in the foreground."""
    try:
        spec = load_spec(spec_path)
    except SpecValidationError as exc:
        raise click.ClickException(f"spec invalid: {exc}") from exc

    store = _resolve_state_store()
    aid = agent_id or f"agent_{uuid.uuid4().hex[:12]}"

    adapter = None
    if mode == "trader":
        if exec_rail is None:
            raise click.ClickException("--exec-rail is required for trader mode")
        try:
            adapter = get_adapter(exec_rail)
        except ExecAdapterError as exc:
            raise click.ClickException(str(exc)) from exc

    # SE-2: wire the real REST oracle client unless --dry-run.
    # GECKO_API_BASE defaults to the production deployment;
    # GECKO_X402_MODE defaults to "stub" (per project_x402_stub_then_live
    # memory: keep stub for friction-free first-call user tests; flip to
    # live only when ready to charge).
    oracle_client: GeckoOracleClient | None = None
    if dry_run:
        caller = make_stub_caller()
    else:
        api_base = os.environ.get("GECKO_API_BASE", "https://api.geckovision.tech")
        x402_mode_raw = os.environ.get("GECKO_X402_MODE", "stub")
        if x402_mode_raw not in ("stub", "live"):
            raise click.ClickException(
                f"GECKO_X402_MODE={x402_mode_raw!r} invalid; must be 'stub' or 'live'"
            )
        oracle_client = GeckoOracleClient(
            api_base=api_base,
            x402_mode=x402_mode_raw,  # type: ignore[arg-type]
        )
        caller = make_rest_caller(
            oracle_client,
            protocol=spec.protocol,
            vertical=spec.vertical,
        )

    oracle = OracleWrapper(
        agent_id=aid,
        state_store=store,
        caller=caller,
    )
    runtime = AgentRuntime(
        agent_id=aid,
        spec=spec,
        mode=mode,  # type: ignore[arg-type]
        state_store=store,
        oracle=oracle,
        exec_adapter=adapter,
        user_wallet=user_wallet,
        execution_rail=exec_rail,
    )

    async def _run() -> None:
        await runtime.start()
        click.echo(f"agent {aid} up — mode={mode} spec={spec.name}@{spec.version}")
        # v0.1: idle loop; real event ingest lands in SE-2 wiring.
        try:
            while runtime.status == "running":
                await asyncio.sleep(1.0)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            await runtime.stop()
            if oracle_client is not None:
                await oracle_client.aclose()
            click.echo(f"agent {aid} stopped")

    asyncio.run(_run())


@trade_agent_cmd.command("ls")
@click.option("--user-wallet", default=None)
def ls_cmd(user_wallet: str | None) -> None:
    """List agents from ``agent_state``."""
    store = _resolve_state_store()

    async def _list() -> None:
        agents = await store.list_agent_states(user_wallet=user_wallet)
        if not agents:
            click.echo("(no agents)")
            return
        for a in agents:
            click.echo(
                f"{a.agent_id}  status={a.status}  mode={a.mode}  spec={a.spec_id}@{a.spec_version}"
            )

    asyncio.run(_list())


@trade_agent_cmd.command("inspect")
@click.argument("agent_id")
@click.option("--limit", default=20, show_default=True)
def inspect_cmd(agent_id: str, limit: int) -> None:
    """Tail journal + show open positions + last verdict."""
    store = _resolve_state_store()

    async def _inspect() -> None:
        state = await store.get_agent_state(agent_id)
        if state is None:
            raise click.ClickException(f"agent {agent_id!r} not found")
        click.echo(
            f"agent {agent_id}  status={state.status}  mode={state.mode}  "
            f"spec={state.spec_id}@{state.spec_version}"
        )
        positions = await store.list_open_positions(agent_id)
        click.echo(f"open positions: {len(positions)}")
        for p in positions:
            click.echo(
                f"  {p.position_id}  mint={p.mint}  size_usd={p.size_usd}  entry={p.entry_price}"
            )
        journal = await store.tail_journal(agent_id, limit=limit)
        click.echo(f"recent journal ({len(journal)}):")
        for e in journal:
            click.echo(f"  {e.ts.isoformat()}  {e.event}  {e.payload}")

    asyncio.run(_inspect())


@trade_agent_cmd.command("stop")
@click.argument("agent_id")
def stop_cmd(agent_id: str) -> None:
    """Mark an agent stopped in ``agent_state``.

    v0.1: this is a state-flip only. The foreground ``up`` process polls
    its own status and exits cleanly on transition. v1.0 daemonised
    runtime will SIGTERM the worker.
    """
    store = _resolve_state_store()

    async def _stop() -> None:
        from datetime import UTC, datetime

        from gecko_core.trade_agent.state.models import AgentJournalEntry

        state = await store.get_agent_state(agent_id)
        if state is None:
            raise click.ClickException(f"agent {agent_id!r} not found")
        await store.update_status(agent_id, "stopped")
        # Journal the transition so `inspect` shows it. The foreground
        # `up` process also emits `agent_stopped` when it shuts down; the
        # CLI flip is a separate signalling path (used when the operator
        # stops an agent from a different shell).
        await store.write_journal(
            AgentJournalEntry(
                agent_id=agent_id,
                ts=datetime.now(UTC),
                event="stopped",
                payload={"source": "cli"},
            )
        )
        click.echo(f"agent {agent_id} marked stopped")

    asyncio.run(_stop())


@trade_agent_cmd.command("pause")
@click.argument("agent_id")
def pause_cmd(agent_id: str) -> None:
    """Pause new entries; existing positions stay managed."""
    store = _resolve_state_store()

    async def _pause() -> None:
        from datetime import UTC, datetime

        from gecko_core.trade_agent.state.models import AgentJournalEntry

        state = await store.get_agent_state(agent_id)
        if state is None:
            raise click.ClickException(f"agent {agent_id!r} not found")
        await store.update_status(agent_id, "paused")
        await store.write_journal(
            AgentJournalEntry(
                agent_id=agent_id,
                ts=datetime.now(UTC),
                event="paused",
                payload={"source": "cli"},
            )
        )
        click.echo(f"agent {agent_id} paused")

    asyncio.run(_pause())


@trade_agent_cmd.command("reverdict")
@click.argument("agent_id")
@click.option(
    "--tier",
    type=click.Choice(["basic", "pro"]),
    default="basic",
    show_default=True,
    help="Verdict tier — basic ($0.25) or pro ($0.75). Stub-mode is free.",
)
@click.option(
    "--force",
    is_flag=True,
    default=True,
    help="Force refresh even on cache hit. Default ON (the point of reverdict is to surface a fresh call).",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Use stub caller — no network at all, returns synthetic 'act' verdict.",
)
@click.option(
    "--live",
    is_flag=True,
    default=False,
    help="Use live x402 signing (NOT WIRED — will fail until follow-up ticket).",
)
def reverdict_cmd(agent_id: str, tier: str, force: bool, dry_run: bool, live: bool) -> None:
    """Fire a manual verdict cycle. Demo-friendly — surfaces a verdict
    in real-time instead of waiting on the 24h scheduled cadence.

    Looks up the agent's spec from agent_state, builds an OracleWrapper,
    calls get_verdict(trigger=manual, force_refresh=True). Writes to the
    verdict cache + journals the event so ``inspect`` shows it.
    """
    from datetime import UTC, datetime

    from gecko_core.trade_agent.state.models import AgentJournalEntry

    store = _resolve_state_store()

    async def _reverdict() -> None:
        state = await store.get_agent_state(agent_id)
        if state is None:
            raise click.ClickException(f"agent {agent_id!r} not found")

        spec_snapshot = state.spec_snapshot or {}
        spec_name = spec_snapshot.get("name") or state.spec_id
        protocol = spec_snapshot.get("protocol", "unknown")

        # Build the same idea shape the runtime uses for scheduled refresh.
        idea = f"session:{spec_name}"

        # Pick the caller. CLI defaults x402_mode=stub regardless of env so
        # the broader-system GECKO_X402_MODE=live (used by scripts/trading_oracle/)
        # doesn't crash this command — the oracle_client live-mode signing
        # path is not wired yet. --live flag below opts in explicitly.
        if dry_run:
            caller = make_stub_caller()
        else:
            api_base = os.environ.get("GECKO_API_BASE", "https://api.geckovision.tech")
            x402_mode = "live" if live else "stub"
            client = GeckoOracleClient(api_base=api_base, x402_mode=x402_mode)
            caller = make_rest_caller(
                client, protocol=protocol, vertical=spec_snapshot.get("vertical", "dex")
            )

        oracle = OracleWrapper(
            agent_id=agent_id,
            state_store=store,
            caller=caller,
        )
        click.echo(
            f"firing manual {tier} verdict for {agent_id} (protocol={protocol}, idea={idea!r})..."
        )
        verdict = await oracle.get_verdict(
            idea=idea,
            tier=tier,
            trigger="manual",
            force_refresh=force,  # type: ignore[arg-type]
        )

        # Journal it so `inspect` sees the cycle.
        entry = AgentJournalEntry(
            agent_id=agent_id,
            ts=datetime.now(UTC),
            event="verdict_called",
            payload={
                "trigger": "manual",
                "tier": tier,
                "verdict": verdict.get("verdict"),
                "confidence": verdict.get("confidence"),
                "n_citations": len(verdict.get("citations", []) or []),
                "n_dissent": verdict.get("dissent_count", 0),
            },
        )
        await store.write_journal(entry)

        click.echo(
            f"  verdict={verdict.get('verdict')}  "
            f"confidence={verdict.get('confidence')}  "
            f"citations={len(verdict.get('citations', []) or [])}  "
            f"dissent={verdict.get('dissent_count', 0)}"
        )
        if not dry_run and hasattr(caller, "__self__"):
            await caller.__self__.aclose()  # type: ignore[attr-defined]

    asyncio.run(_reverdict())


@trade_agent_cmd.command("purge")
@click.option(
    "--status",
    type=click.Choice(["stopped", "running", "paused", "halted"]),
    default="stopped",
    show_default=True,
    help="Status to purge.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="List what would be purged without deleting.",
)
def purge_cmd(status: str, dry_run: bool) -> None:
    """Delete agent_state rows by status. Cleans up orphans from earlier
    runs where SIGKILL prevented the Mongo state update.

    Does NOT delete journal entries (those are TTL-managed at 90 days).
    """
    store = _resolve_state_store()

    async def _purge() -> None:
        states = await store.list_agent_states(status=status)
        if not states:
            click.echo(f"no agents with status={status!r}")
            return
        for s in states:
            if dry_run:
                click.echo(f"WOULD PURGE  {s.agent_id}  spec={s.spec_id}")
            else:
                # Use the raw collection — there's no delete in StateStore protocol.
                # This is a CLI-side cleanup, not a runtime concern, so reaching past
                # the protocol is acceptable. v1.0 would add StateStore.delete.
                if isinstance(store, MongoStateStore):
                    await store._db["agent_state"].delete_one({"agent_id": s.agent_id})
                    click.echo(f"PURGED       {s.agent_id}")
                else:
                    click.echo(f"SKIP (in-memory store, no-op): {s.agent_id}")

    asyncio.run(_purge())


@trade_agent_cmd.command("backtest")
@click.option(
    "--spec",
    "spec_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Path to the coach-emitted strategy JSON.",
)
@click.option(
    "--window-days",
    type=int,
    default=90,
    show_default=True,
    help="Replay window length in days (metadata only — actual length "
    "is governed by the history source).",
)
@click.option(
    "--source",
    type=click.Choice(["fixture", "mongo"]),
    default="fixture",
    show_default=True,
    help="History source. 'fixture' reads --history-path JSON; 'mongo' "
    "replays agent_hotpath_snapshot (requires MONGODB_URI).",
)
@click.option(
    "--history-path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Path to a history fixture JSON (required when --source=fixture).",
)
@click.option(
    "--oracle-fixture",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Recorded verdicts JSON. Defaults to pessimistic (no trades).",
)
@click.option(
    "--gating",
    type=click.Choice(["both", "on", "off"]),
    default="both",
    show_default=True,
)
@click.option(
    "--out",
    "out_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Write the full BacktestResult JSON to this path.",
)
def backtest_cmd(
    spec_path: Path,
    window_days: int,
    source: str,
    history_path: Path | None,
    oracle_fixture: Path | None,
    gating: str,
    out_path: Path | None,
) -> None:
    """Replay a strategy spec against historical data with gating ON vs OFF.

    Emits Sharpe / max-DD / PnL delta — the "verdict-gated PnL vs
    ungated baseline" numbers from the co-founder brief Section 1.7.

    Per founder constraint, this NEVER calls gecko_trade_research live —
    the oracle is always a fixture during replay.
    """
    import json as _json

    from gecko_core.trade_agent.backtest import (
        FixtureHistorySource,
        MongoHotpathHistorySource,
        PessimisticOracleFixture,
        RecordedOracleFixture,
    )

    try:
        spec = load_spec(spec_path)
    except SpecValidationError as exc:
        raise click.ClickException(f"spec invalid: {exc}") from exc

    if source == "fixture":
        if history_path is None:
            raise click.ClickException("--history-path is required when --source=fixture")
        history = FixtureHistorySource(history_path)
    else:
        # mongo branch — wired but tests don't exercise it
        try:
            from pymongo import MongoClient  # type: ignore[import-not-found]
        except ImportError as exc:
            raise click.ClickException("--source=mongo requires pymongo installed") from exc
        uri = os.environ.get("MONGODB_URI") or os.environ.get("MONGO_URI")
        if not uri:
            raise click.ClickException("MONGODB_URI not set for --source=mongo")
        client = MongoClient(uri)
        history = MongoHotpathHistorySource(  # type: ignore[assignment]
            agent_id=spec.spec_id,
            window_start_ts=0.0,
            window_end_ts=window_days * 86400.0,
            client=client,
        )

    oracle: Any
    if oracle_fixture is not None:
        oracle = RecordedOracleFixture(oracle_fixture)
    else:
        oracle = PessimisticOracleFixture()

    async def _run() -> None:
        from gecko_core.trade_agent.backtest.harness import BacktestHarness

        harness = BacktestHarness(spec, history=history, oracle=oracle)
        result = await harness.run(gating=gating, window_days=window_days)  # type: ignore[arg-type]

        # Rich-style table to stdout
        try:
            from rich.console import Console
            from rich.table import Table

            console = Console()
            table = Table(title=f"backtest {spec.name}@{spec.version}")
            table.add_column("arm")
            table.add_column("pnl_pct", justify="right")
            table.add_column("sharpe", justify="right")
            table.add_column("max_dd_pct", justify="right")
            table.add_column("hit_rate", justify="right")
            table.add_column("n_trades", justify="right")
            for label, run in (("gated", result.gated), ("ungated", result.ungated)):
                if run is None:
                    continue
                table.add_row(
                    label,
                    f"{run.pnl_pct:.4f}",
                    f"{run.sharpe:.4f}",
                    f"{run.max_dd_pct:.4f}",
                    f"{run.hit_rate:.4f}",
                    str(run.n_trades),
                )
            console.print(table)
            console.print(
                f"delta_pnl_pct={result.delta_pnl_pct:.4f}  "
                f"delta_sharpe={result.delta_sharpe:.4f}  "
                f"verdict_calls={result.verdict_call_count}  "
                f"cache_hit_rate={result.cache_hit_rate:.4f}"
            )
        except ImportError:
            click.echo(result.model_dump_json(indent=2))

        if out_path is not None:
            out_path.write_text(_json.dumps(result.model_dump(), indent=2, default=str))
            click.echo(f"wrote {out_path}")

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# S24 WS-D Task #2 — `bb trade-agent doctor` health probe.
#
# Each check is a thin function returning ``(ok: bool, detail: str)`` plus a
# remediation hint surfaced only on failure. Keep checks side-effect-free
# except the Mongo probe, which writes-then-deletes one document under the
# dedicated ``gecko_trade_agent.doctor_probe`` collection.
# ---------------------------------------------------------------------------


def _check_mongo() -> tuple[bool, str, str]:
    """Write-then-delete a noop doc; tolerant of MONGO_URI/MONGODB_URI."""
    uri = os.environ.get("MONGODB_URI") or os.environ.get("MONGO_URI")
    if not uri or uri == "__unset__":
        return (
            False,
            "MONGODB_URI not set",
            "export MONGODB_URI=mongodb+srv://... (Atlas SRV connection string)",
        )
    try:
        from pymongo import MongoClient  # type: ignore[import-untyped]

        client = MongoClient(uri, serverSelectionTimeoutMS=3000)
        coll = client["gecko_trade_agent"]["doctor_probe"]
        from datetime import UTC, datetime

        res = coll.insert_one({"ts": datetime.now(UTC), "probe": True})
        coll.delete_one({"_id": res.inserted_id})
        client.close()
        return (True, "mongo reach ok (write+read+delete)", "")
    except Exception as exc:
        return (
            False,
            f"mongo unreachable: {type(exc).__name__}: {exc}",
            "verify MONGODB_URI credentials + Atlas IP allowlist",
        )


def _check_x402() -> tuple[bool, str, str]:
    mode = os.environ.get("X402_MODE", "stub")
    if mode != "live":
        return (True, f"X402_MODE={mode} (no buyer-wallet check needed)", "")
    buyer = os.environ.get("GECKO_BUYER_WALLET_SECRET") or os.environ.get("GECKO_BUYER_WALLET_KEY")
    if not buyer:
        return (
            False,
            "X402_MODE=live but no buyer wallet secret in env",
            "set GECKO_BUYER_WALLET_SECRET (SSM SecureString preferred)",
        )
    return (True, "X402_MODE=live + buyer wallet configured", "")


def _check_frames_wallet() -> tuple[bool, str, str]:
    path = Path.home() / ".agentwallet" / "config.json"
    if not path.exists():
        return (
            False,
            f"frames.ag wallet config missing at {path}",
            "run `agentwallet init` (frames.ag CLI) to seed config",
        )
    try:
        import json as _json

        _json.loads(path.read_text())
    except Exception as exc:
        return (
            False,
            f"frames.ag config invalid JSON: {exc}",
            f"re-run `agentwallet init` to regenerate {path}",
        )
    return (True, f"frames.ag config ok at {path}", "")


def _check_mcp_registration() -> tuple[bool, str, str]:
    import subprocess

    try:
        proc = subprocess.run(
            ["claude", "mcp", "list"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except FileNotFoundError:
        return (
            False,
            "`claude` CLI not found on PATH",
            "install Claude Code CLI: https://docs.claude.com/en/docs/claude-code",
        )
    except Exception as exc:
        return (False, f"`claude mcp list` failed: {exc}", "verify Claude Code CLI install")
    if proc.returncode != 0:
        return (
            False,
            f"`claude mcp list` exited {proc.returncode}: {proc.stderr.strip()[:120]}",
            "run `claude mcp list` manually to inspect",
        )
    if "gecko" not in proc.stdout.lower():
        return (
            False,
            "no gecko-mcp registration found in `claude mcp list`",
            "run `claude mcp add gecko -- gecko-mcp` (or see skill.md)",
        )
    return (True, "gecko-mcp registered with Claude Code", "")


@trade_agent_cmd.command("doctor")
def doctor_cmd() -> None:
    """Health probe — checks Mongo, x402, frames.ag wallet, MCP registration.

    Each check prints one line. A non-zero exit code on any failure
    signals "operator action required" to wrapping scripts (ECS task
    health-check, CI smoke).
    """
    checks: list[tuple[str, tuple[bool, str, str]]] = [
        ("mongo", _check_mongo()),
        ("x402", _check_x402()),
        ("frames-wallet", _check_frames_wallet()),
        ("mcp-registration", _check_mcp_registration()),
    ]
    failures = 0
    for name, (ok, detail, fix) in checks:
        mark = "PASS" if ok else "FAIL"
        click.echo(f"[{mark}] {name}: {detail}")
        if not ok:
            failures += 1
            if fix:
                click.echo(f"       remediation: {fix}")
    if failures:
        raise click.exceptions.Exit(code=1)


__all__ = ["trade_agent_cmd"]
