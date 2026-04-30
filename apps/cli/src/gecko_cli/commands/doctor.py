"""``bb doctor`` — pre-flight resolution + connectivity checks (S8-CONFIG-02).

Surfaces the unified config plane in ONE place so an operator can answer
"why is my CLI/MCP run failing?" without reading three settings modules.

Layout: a Rich table, one row per check, with a status glyph + one-line
explanation. Exit code is the contract: 0 = all green, 1 = at least one
red. Yellow (warning / info) does not affect exit code.

Each row is a pure function so the test suite can wire mocked
stores/clients in. Keep network calls behind clearly named helpers
(``_check_memory_roundtrip`` etc.) and never print raw secret values —
only "set" / "missing" + a 4-char tail of the key for diff'ability.
"""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass
from typing import Any, Literal, Protocol
from uuid import uuid4

import click
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

console = Console()

Status = Literal["ok", "warn", "err"]
_GLYPH: dict[Status, str] = {
    "ok": "[green]OK[/green]",
    "warn": "[yellow]WARN[/yellow]",
    "err": "[red]FAIL[/red]",
}


@dataclass(frozen=True)
class CheckRow:
    """One row of the doctor table.

    ``name`` is the short label (e.g. ``Supabase URL``); ``detail`` is the
    explanation column. ``status`` drives the glyph and the exit code (only
    ``"err"`` rows force exit 1).
    """

    name: str
    status: Status
    detail: str


# ---------------------------------------------------------------------------
# Helpers — secret masking, env presence
# ---------------------------------------------------------------------------


def _mask(value: str | None) -> str:
    """Render a secret as ``set (...XXXX)`` so operators can diff env without
    leaking the key. ``None`` / empty → ``missing``.
    """
    if not value:
        return "missing"
    tail = value[-4:] if len(value) >= 4 else "?"
    return f"set (...{tail})"


# ---------------------------------------------------------------------------
# Per-row checks
# ---------------------------------------------------------------------------


def check_supabase_env(env: dict[str, str]) -> CheckRow:
    """Verify SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY are present."""
    url = env.get("SUPABASE_URL", "")
    key = env.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not url:
        return CheckRow("Supabase URL", "err", "SUPABASE_URL is missing")
    if not key:
        return CheckRow(
            "Supabase URL", "err", f"url={url} but SUPABASE_SERVICE_ROLE_KEY is missing"
        )
    return CheckRow("Supabase URL", "ok", f"{url} (service-role {_mask(key)})")


class _MemoryStoreLike(Protocol):
    """Minimal surface so tests can substitute a fake."""

    async def insert(self, **kwargs: Any) -> Any: ...
    async def list_by_scope(self, **kwargs: Any) -> Any: ...
    async def delete(self, entry_id: Any) -> Any: ...


async def check_memory_roundtrip(store: _MemoryStoreLike | None = None) -> CheckRow:
    """Write + read + delete a probe entry on the ``memory`` table.

    Catches F7 (CLI writes vs MCP reads pointing at different Supabase
    projects) — if the CLI's resolved store can't read what it just
    wrote, the table is unreachable from this process.
    """
    from gecko_core.memory.models import MemoryEntryType, MemoryScope
    from gecko_core.memory.store import MemoryStore

    s = store or MemoryStore.from_env()
    scope = MemoryScope(type="user", id=f"doctor-probe-{uuid4()}")
    try:
        entry_id = await s.insert(
            scope=scope,
            entry_type=MemoryEntryType.user_note,
            value={"probe": True, "source": "bb doctor"},
            embedding=None,
            key="doctor.probe",
        )
    except Exception as exc:
        return CheckRow(
            "Memory roundtrip", "err", f"insert failed: {exc.__class__.__name__}: {exc}"
        )

    try:
        rows = await s.list_by_scope(scope=scope, limit=5)
    except Exception as exc:
        return CheckRow("Memory roundtrip", "err", f"read-back failed: {exc}")

    if not rows or not any(getattr(r, "id", None) == entry_id for r in rows):
        return CheckRow(
            "Memory roundtrip",
            "err",
            "wrote a probe row but couldn't read it back — CLI and MCP may "
            "point at different Supabase projects (see audit F7).",
        )

    # Best-effort cleanup. Don't fail the row on cleanup errors.
    import contextlib

    with contextlib.suppress(Exception):  # pragma: no cover — best-effort
        await s.delete(entry_id)

    return CheckRow("Memory roundtrip", "ok", "write + read + delete succeeded")


def check_llm_resolution(env: dict[str, str]) -> CheckRow:
    """Resolve the LLM client config and report base_url + key presence."""
    from gecko_core.orchestration.settings import resolve_llm_config

    try:
        cfg = resolve_llm_config(env)
    except Exception as exc:
        return CheckRow("LLM router", "err", f"resolve_llm_config failed: {exc}")

    if not cfg.api_key or cfg.api_key in ("", "x402"):
        # ``x402`` is the ClawRouter literal — fine for local dev, but
        # surface it as WARN so production deploys notice if they forgot
        # to set a real key.
        return CheckRow(
            "LLM router",
            "warn",
            f"source={cfg.source} base_url={cfg.base_url} api_key={cfg.api_key!r} "
            "(ClawRouter literal — set OPENAI_API_KEY or OPENROUTER_API_KEY for live use)",
        )
    return CheckRow(
        "LLM router",
        "ok",
        f"source={cfg.source} base_url={cfg.base_url} key={_mask(cfg.api_key)}",
    )


def check_x402(env: dict[str, str]) -> CheckRow:
    """Surface X402_MODE + treasury + facilitator URL."""
    mode = (env.get("X402_MODE") or "stub").lower()
    treasury = env.get("GECKO_WALLET_ADDRESS")
    facilitator = env.get("X402_FACILITATOR_URL")

    if mode == "stub":
        return CheckRow(
            "x402 mode",
            "ok",
            f"mode=stub (treasury={treasury or '<unset>'}, facilitator={facilitator or '<default>'})",
        )

    # live / frames require both treasury and facilitator.
    missing: list[str] = []
    if not treasury:
        missing.append("GECKO_WALLET_ADDRESS")
    if not facilitator:
        missing.append("X402_FACILITATOR_URL")
    if missing:
        return CheckRow("x402 mode", "err", f"mode={mode} but missing {', '.join(missing)}")
    return CheckRow(
        "x402 mode",
        "ok",
        f"mode={mode}, treasury={treasury}, facilitator={facilitator}",
    )


def check_frames_wallet(
    env: dict[str, str],
    *,
    agent_token_reader: Any | None = None,
) -> CheckRow:
    """Report frames.ag wallet config — apiToken presence only.

    Resolution order (S9-DOCTOR-01, fixes F14):
      1. ``FRAMES_API_KEY`` / ``FRAMES_AG_API_TOKEN`` env var.
      2. ``apiToken`` from ``~/.agentwallet/config.json`` (where frames.ag's
         connect skill caches credentials).

    The token itself is never echoed; only its 4-char tail is shown for env
    diffability. ``agent_token_reader`` is the test seam so the file-system
    path can be stubbed.
    """
    from gecko_core.wallet import read_agent_token

    reader = agent_token_reader or read_agent_token
    token = env.get("FRAMES_API_KEY") or env.get("FRAMES_AG_API_TOKEN")
    source = "env"
    if not token:
        token = reader()
        source = "agent-config"
    base_url = env.get("FRAMES_AG_BASE_URL")
    mode = (env.get("X402_MODE") or "stub").lower()
    if not token:
        # Only required when X402_MODE=frames; otherwise it's informational.
        if mode == "frames":
            return CheckRow(
                "frames.ag wallet",
                "err",
                "X402_MODE=frames but no apiToken found in env "
                "(FRAMES_API_KEY / FRAMES_AG_API_TOKEN) or ~/.agentwallet/config.json",
            )
        return CheckRow(
            "frames.ag wallet",
            "warn",
            "no apiToken found (env or ~/.agentwallet/config.json) — "
            "required only when X402_MODE=frames",
        )
    return CheckRow(
        "frames.ag wallet",
        "ok",
        f"apiToken={_mask(token)} (source={source}), base_url={base_url or '<default>'}",
    )


# ---------------------------------------------------------------------------
# Top-level command
# ---------------------------------------------------------------------------


async def _run_all(env: dict[str, str], store: _MemoryStoreLike | None) -> list[CheckRow]:
    """Resolve every doctor row. Memory roundtrip is the only async/network
    call; the rest are pure env reads.
    """
    rows = [
        check_supabase_env(env),
        check_llm_resolution(env),
        check_x402(env),
        check_frames_wallet(env),
    ]
    # Only attempt the memory roundtrip when Supabase env is present —
    # otherwise the failure is double-counted with the env row.
    if rows[0].status == "ok":
        rows.append(await check_memory_roundtrip(store))
    else:
        rows.append(
            CheckRow(
                "Memory roundtrip",
                "warn",
                "skipped — Supabase env missing",
            )
        )
    return rows


def render_table(rows: list[CheckRow]) -> Table:
    table = Table(title="bb doctor — config + connectivity", show_lines=False)
    table.add_column("Check", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Detail", overflow="fold")
    for r in rows:
        table.add_row(r.name, _GLYPH[r.status], r.detail)
    return table


@click.command("doctor")
def doctor_cmd() -> None:
    """Resolve env, ping the memory table, and print pass/fail per check.

    Exit code: 0 if no checks failed, 1 otherwise. Warnings do not affect
    exit code.
    """
    # S9-DOCTOR-01 (F13): explicitly load `.env` from the current working
    # directory before reading os.environ. The CLI group only loads a `.env`
    # when --env-file is passed or ~/.gecko/.env exists, so a fresh shell
    # in a project directory was previously seeing FAIL on every env check.
    #
    # We scope to ``cwd / .env`` (rather than dotenv's default upward walk)
    # so behavior is predictable: doctor reads the same file the operator
    # is staring at in their project root. ``override=False`` so values
    # already in the shell win over .env.
    from pathlib import Path

    cwd_env = Path.cwd() / ".env"
    if cwd_env.is_file():
        load_dotenv(dotenv_path=cwd_env, override=False)
    rows = asyncio.run(_run_all(dict(os.environ), store=None))
    console.print(render_table(rows))

    failed = [r for r in rows if r.status == "err"]
    if failed:
        console.print(f"[red]{len(failed)} check(s) failed.[/red]")
        sys.exit(1)
    console.print("[green]All checks passed.[/green]")


__all__ = [
    "CheckRow",
    "check_frames_wallet",
    "check_llm_resolution",
    "check_memory_roundtrip",
    "check_supabase_env",
    "check_x402",
    "doctor_cmd",
    "render_table",
]
