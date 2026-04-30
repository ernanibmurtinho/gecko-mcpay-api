"""bb / gecko CLI — thin wrapper over gecko-core.

All business logic lives in `gecko_core`. This module:
  - parses CLI args
  - calls into gecko_core
  - renders results with rich
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import click
from dotenv import load_dotenv
from gecko_core._logging import install as install_redaction

from gecko_cli.commands.advise import advise_cmd
from gecko_cli.commands.ask import ask_cmd
from gecko_cli.commands.classify import classify_cmd
from gecko_cli.commands.memory import memory_cmd
from gecko_cli.commands.plan import plan_cmd
from gecko_cli.commands.precedents import precedents_cmd
from gecko_cli.commands.pricing import pricing_cmd
from gecko_cli.commands.project import project_cmd
from gecko_cli.commands.pulse import pulse_cmd
from gecko_cli.commands.research import research_cmd
from gecko_cli.commands.resume import resume_cmd
from gecko_cli.commands.route import route_cmd
from gecko_cli.commands.scaffold import scaffold_cmd
from gecko_cli.commands.sources import sources_cmd
from gecko_cli.commands.sprint_review import sprint_review_cmd


@click.group()
@click.option(
    "--env-file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Path to .env file. Defaults to ~/.gecko/.env if present.",
)
@click.option(
    "--yes",
    "-y",
    "yes",
    is_flag=True,
    default=False,
    help="Auto-confirm interactive prompts (scripted runs).",
)
@click.option(
    "--non-interactive",
    "non_interactive",
    is_flag=True,
    default=False,
    help=(
        "Disable all prompts. Implies --yes. Errors fast if the command would "
        "otherwise need user input."
    ),
)
@click.pass_context
def cli(
    ctx: click.Context,
    env_file: Path | None,
    yes: bool,
    non_interactive: bool,
) -> None:
    """Gecko — Builder Bootstrap Platform."""
    if env_file:
        load_dotenv(env_file)
    else:
        default = Path.home() / ".gecko" / ".env"
        if default.exists():
            load_dotenv(default)

    # S8-LOG-01 — install the redaction filter BEFORE any HTTP client logs
    # fire. httpcore/hpack dump auth headers + Bearer tokens at DEBUG; the
    # filter scrubs them at the root logger.
    level_name = (os.environ.get("LOG_LEVEL") or "WARNING").upper()
    level = getattr(logging, level_name, logging.WARNING)
    logging.basicConfig(level=level)
    install_redaction(level=level)

    # Stash bypass flags so subcommands and the shared `_prompt.confirm()`
    # helper can honor them. --non-interactive implies --yes.
    ctx.ensure_object(dict)
    # Keep these independent: --non-interactive implies --yes via the helpers
    # in `_prompt`, but the raw flags stay separate so strict-mode checks
    # (e.g. default=False prompts) can still error out.
    ctx.obj["yes"] = yes
    ctx.obj["non_interactive"] = non_interactive


cli.add_command(research_cmd)
cli.add_command(ask_cmd)
cli.add_command(sources_cmd)
cli.add_command(project_cmd)
cli.add_command(classify_cmd)
cli.add_command(precedents_cmd)
cli.add_command(route_cmd)
cli.add_command(scaffold_cmd)
cli.add_command(advise_cmd)
cli.add_command(plan_cmd)
cli.add_command(pulse_cmd)
cli.add_command(memory_cmd)
cli.add_command(resume_cmd)
cli.add_command(pricing_cmd)
cli.add_command(sprint_review_cmd)


# Back-compat alias — older docs reference `main`.
main = cli


if __name__ == "__main__":
    cli()
