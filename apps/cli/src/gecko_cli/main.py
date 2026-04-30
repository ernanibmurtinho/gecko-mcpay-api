"""bb / gecko CLI — thin wrapper over gecko-core.

All business logic lives in `gecko_core`. This module:
  - parses CLI args
  - calls into gecko_core
  - renders results with rich
"""

from __future__ import annotations

from pathlib import Path

import click
from dotenv import load_dotenv

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


@click.group()
@click.option(
    "--env-file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Path to .env file. Defaults to ~/.gecko/.env if present.",
)
def cli(env_file: Path | None) -> None:
    """Gecko — Builder Bootstrap Platform."""
    if env_file:
        load_dotenv(env_file)
    else:
        default = Path.home() / ".gecko" / ".env"
        if default.exists():
            load_dotenv(default)


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


# Back-compat alias — older docs reference `main`.
main = cli


if __name__ == "__main__":
    cli()
