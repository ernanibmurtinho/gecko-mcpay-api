"""`gecko pulse` — re-run the advisor panel and surface a verdict.

Two surfaces co-exist on this command:

  * **S14-PULSE-01 (default when SESSION_ID is given):** runs the v14 pulse
    engine, which creates a NEW session row with ``phase="during_build"``
    and ``parent_session_id=<input>``, re-runs the 5-voice advisor panel
    against the parent session's RAG context, and renders a verdict +
    gap_classification + 3-bullet summary + fresh citations. The pulse
    settles via x402 at ``$0.50/call`` — when the user has remaining
    12-pack credits (S14-PULSE-02), the credit is decremented FIRST and
    no per-call settle fires.

  * **Legacy (--project-id only):** falls through to the older
    ``run_pulse`` (S5-API-02) which walks pulse history across the
    project for closing-line delta detection.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from uuid import UUID

import click
from rich.console import Console
from rich.table import Table

if TYPE_CHECKING:
    from gecko_core.orchestration.advisor.models import PulseResult

console = Console()


@click.command("pulse")
@click.argument("session_id", required=False)
@click.option(
    "--project-id",
    "project_id",
    default=None,
    help="Project UUID — walks pulse history across all of the project's sessions.",
)
@click.option(
    "--tier-preset",
    "tier_preset",
    type=click.Choice(("quality", "balanced", "budget", "free")),
    default="balanced",
    show_default=True,
)
@click.option(
    "--user-wallet",
    "user_wallet",
    default=None,
    help=(
        "Wallet address for the 12-pack credits ledger lookup (S14-PULSE-02). "
        "If unset, the call falls through to per-call settle ($0.50)."
    ),
)
def pulse_cmd(
    session_id: str | None,
    project_id: str | None,
    tier_preset: str,
    user_wallet: str | None,
) -> None:
    """Re-run the panel and surface a verdict.

    Pass either SESSION_ID (S14 surface) or --project-id (legacy
    closing-line delta surface). When both are given, SESSION_ID wins
    and runs the v14 engine.
    """
    from gecko_core.orchestration.advisor import (
        AdvisorSessionNotFoundError,
        run_pulse,
    )
    from gecko_core.routing.catalog import Tier

    if not session_id and not project_id:
        raise click.BadParameter("either SESSION_ID or --project-id is required")

    sid: UUID | None = None
    if session_id:
        try:
            sid = UUID(session_id)
        except ValueError as exc:
            raise click.BadParameter(f"session_id is not a valid UUID: {exc}") from exc

    pid: UUID | None = None
    if project_id:
        try:
            pid = UUID(project_id)
        except ValueError as exc:
            raise click.BadParameter(f"project-id is not a valid UUID: {exc}") from exc

    try:
        tier = Tier(tier_preset)
    except ValueError as exc:
        raise click.BadParameter(f"unknown tier_preset: {exc}") from exc

    # S14 surface: when a session_id is provided, run the v14 pulse engine.
    if sid is not None:
        try:
            v14 = asyncio.run(_run_v14_with_credits(sid, tier, user_wallet))
        except AdvisorSessionNotFoundError as exc:
            console.print(f"[red]Not found:[/red] {exc}")
            raise SystemExit(1) from exc

        from gecko_cli.render import render_pulse_result

        render_pulse_result(v14, console=console)
        console.print(f"[dim]total cost: ${v14.panel.total_cost_usd:.4f}[/dim]")
        return

    # Legacy --project-id-only path: closing-line deltas.
    try:
        result = asyncio.run(run_pulse(session_id=None, project_id=pid, tier_preset=tier))
    except AdvisorSessionNotFoundError as exc:
        console.print(f"[red]Not found:[/red] {exc}")
        raise SystemExit(1) from exc

    table = Table(title=f"Pulse — session {result.panel.session_id}")
    table.add_column("Voice")
    table.add_column("Changed?")
    table.add_column("Now")
    table.add_column("Was")
    for d in result.deltas:
        table.add_row(
            d.role.value,
            "yes" if d.changed else "no",
            d.current_closing_line,
            d.previous_closing_line or "—",
        )
    console.print(table)
    console.print(f"[dim]total cost: ${result.panel.total_cost_usd:.4f}[/dim]")


async def _run_v14_with_credits(
    parent_session_id: UUID,
    tier_preset: object,
    user_wallet: str | None,
) -> PulseResult:
    """Run v14 pulse, decrementing a 12-pack credit first if available.

    Stub-mode-friendly: ``decrement_pulse_credit`` is a best-effort call
    on the credits ledger; failures fall through to the v14 run without
    per-call settle (CLI does not gate-charge — the gecko-api ``/pulse``
    HTTP route owns that). The decrement call is purely informational at
    the CLI layer; it surfaces ``credits_remaining_after`` on the result
    so the renderer can show the running balance.
    """
    from gecko_core.orchestration.advisor import run_pulse_v14
    from gecko_core.payments.pulse_credits import (
        decrement_pulse_credit,
    )

    credits_after: int | None = None
    if user_wallet:
        try:
            credits_after = await decrement_pulse_credit(user_wallet=user_wallet)
        except Exception:  # pragma: no cover — best-effort
            credits_after = None

    result = await run_pulse_v14(
        parent_session_id=parent_session_id,
        tier_preset=tier_preset,  # type: ignore[arg-type]
    )
    if credits_after is not None:
        # PulseResult is frozen-friendly via pydantic; rebuild with the
        # credit count surfaced for rendering.
        result = result.model_copy(update={"credits_remaining_after": credits_after})
    return result
