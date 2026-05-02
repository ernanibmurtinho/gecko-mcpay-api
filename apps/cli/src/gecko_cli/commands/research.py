"""`bb research` — discover, approve, pay, index, generate."""

from __future__ import annotations

import asyncio
from decimal import Decimal, InvalidOperation
from uuid import UUID

import click
import gecko_core
from gecko_core.models import SourceCandidate
from rich.console import Console
from rich.table import Table

from gecko_cli._prompt import assume_yes
from gecko_cli._prompt import confirm as prompt_confirm
from gecko_cli._render_compat import progress_context, render_research_result
from gecko_cli.commands.project import resolve_project_id

console = Console()


def _print_candidates(candidates: list[SourceCandidate]) -> None:
    table = Table(title=f"Proposed sources ({len(candidates)})")
    table.add_column("#", justify="right", style="dim")
    table.add_column("Score", justify="right")
    table.add_column("Type")
    table.add_column("URL", overflow="fold")
    for i, c in enumerate(candidates, 1):
        table.add_row(str(i), f"{c.score:.2f}", c.type, str(c.url))
    console.print(table)


async def _interactive_approval(candidates: list[SourceCandidate]) -> bool:
    _print_candidates(candidates)
    # `prompt_confirm` honors top-level --yes / --non-interactive flags.
    return prompt_confirm("Proceed with these sources?", default=True)


@click.command("research")
@click.option("--idea", required=True, help="Plain-language startup idea.")
@click.option("--tier", type=click.Choice(["basic", "pro"]), default="basic")
@click.option("--urls", multiple=True, help="Optional seed URLs (repeatable).")
@click.option("--yes", "-y", is_flag=True, help="Skip the approval prompt.")
@click.option(
    "--project",
    "project",
    default=None,
    help="Attach this run to a project (UUID or name; falls back to .gecko/project.json).",
)
@click.option(
    "--tier-preset",
    "tier_preset",
    type=click.Choice(["quality", "balanced", "budget", "free"]),
    default="balanced",
    help=(
        "User-facing cost/quality preset (S4-MATRIX-01). Drives per-agent "
        "model selection via the curated catalog."
    ),
)
@click.option(
    "--publish",
    "publish",
    is_flag=True,
    default=False,
    help=(
        "S14-PUB-01: after the verdict lands, publish the rendered "
        "ResearchResult as a publish.new artifact at $0.50 (override "
        "with --publish-price). Opt-in; default off."
    ),
)
@click.option(
    "--publish-price",
    "publish_price",
    type=str,
    default=None,
    help="Override the publish.new artifact price in USD (Decimal).",
)
@click.option(
    "--publish-as",
    "publish_as",
    type=str,
    default=None,
    help=(
        "Override the author wallet (Base 0x address) for the published "
        "artifact. Defaults to GECKO_WALLET_ADDRESS_BASE (Phase 1)."
    ),
)
@click.option(
    "--degraded",
    is_flag=True,
    default=False,
    help=(
        "S14-HARDEN-01: backend-down fallback. Synthesizes a validation "
        "report from the local SQLite cache (~/.gecko/cache.db) without "
        "touching Supabase or fresh sources. Skips the payment gate. "
        "Receipt carries a clear `mode: degraded` warning."
    ),
)
def research_cmd(
    idea: str,
    tier: str,
    urls: tuple[str, ...],
    yes: bool,
    project: str | None,
    tier_preset: str,
    publish: bool,
    publish_price: str | None,
    publish_as: str | None,
    degraded: bool,
) -> None:
    """Discover, index, generate. The main workflow."""
    seed = list(urls) if urls else None
    project_id = resolve_project_id(project)
    if project_id is not None:
        console.print(f"[dim]project: {project_id}[/dim]")

    parsed_publish_price: Decimal | None = None
    if publish_price is not None:
        try:
            parsed_publish_price = Decimal(publish_price)
        except (InvalidOperation, ValueError) as exc:
            raise click.BadParameter(f"--publish-price must be a Decimal: {exc}") from exc
        if parsed_publish_price <= 0:
            raise click.BadParameter("--publish-price must be > 0")

    # Top-level --yes / --non-interactive bubble through `assume_yes`; keep
    # the per-command --yes flag for back-compat.
    auto_approve = assume_yes(local=yes)

    with progress_context(console, "Researching") as progress:

        def _progress(msg: str) -> None:
            progress.update(msg)

        if degraded:
            # S14-HARDEN-01: skip the live workflow entirely. Cache-only
            # synthesis, no Supabase, no payment gate.
            from gecko_core.workflows import degraded_research

            console.print(
                "[bold yellow]Degraded mode[/bold yellow] — using local cache; "
                "no fresh sources will be fetched."
            )
            try:
                result = asyncio.run(degraded_research(idea=idea, progress_callback=_progress))
            except RuntimeError as exc:
                console.print(f"[red]degraded mode failed:[/red] {exc}")
                return
        else:
            result = asyncio.run(
                gecko_core.research(
                    idea=idea,
                    tier=tier,  # type: ignore[arg-type]
                    urls=seed,
                    auto_approve=auto_approve,
                    approval_callback=None if auto_approve else _interactive_approval,
                    progress_callback=_progress,
                    tier_preset=tier_preset,
                )
            )

    render_research_result(console, result)
    # S18-VERDICT-HASH-01 — deterministic hash printed in CLI footer.
    # Same idea + same retrieved citations + same verdict → same hash,
    # regardless of which storage backend (Supabase or Mongo) served it.
    from gecko_core.verdict_hash import verdict_hash_short

    short_hash = verdict_hash_short(idea, result)
    console.print(f"[dim]session_id: {result.session_id}  ·  {short_hash}[/dim]")

    if publish:
        # S14-PUB-01 — opt-in publish.new artifact upload. Runs AFTER the
        # main render so the founder sees the verdict first, then the
        # publish receipt block. Failures are surfaced verbatim and do
        # NOT roll back the research session — they paid for the verdict
        # already, the publish is incidental.
        from gecko_core.payments import publish_new as pn_mod

        try:
            artifact = asyncio.run(
                pn_mod.publish_artifact(
                    session_id=UUID(result.session_id),
                    idea=idea,
                    result=result,
                    price_usd=parsed_publish_price,
                    author_address=publish_as,
                )
            )
        except pn_mod.PublishNewError as exc:
            console.print(f"[red]publish.new error:[/red] {exc}")
            return

        console.print()
        console.print("[bold bright_blue]Published[/bold bright_blue]")
        console.print(f"  url:    [link={artifact.url}]{artifact.url}[/link]")
        console.print(f"  slug:   {artifact.slug}")
        console.print(f"  price:  ${artifact.price_usd}")
        console.print(f"  author: {artifact.author_address}")
        if artifact.is_stub:
            console.print("  [dim](stub mode — no on-chain settlement)[/dim]")
        elif artifact.tx_signature:
            console.print(f"  tx:     {artifact.tx_signature}")
