"""Entry point for `gecko-mcp` command.

Subcommands:
    gecko-mcp serve [--env-file PATH]    — start MCP server over stdio
    gecko-mcp doctor                      — verify env vars + connectivity
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import click
from dotenv import load_dotenv

from gecko_mcp.doctor import run_doctor
from gecko_mcp.economics import economics as economics_cmd
from gecko_mcp.llm import llm as llm_group
from gecko_mcp.project import project as project_cmd
from gecko_mcp.quickstart import quickstart as quickstart_cmd
from gecko_mcp.server import serve
from gecko_mcp.wallet import wallet as wallet_group


@click.group()
@click.version_option(package_name="gecko-mcp", prog_name="gecko-mcp")
def main() -> None:
    """Gecko MCP server."""


@main.command(name="serve")
@click.option(
    "--env-file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Path to .env file. Defaults to ~/.gecko/.env if present.",
)
def serve_cmd(env_file: Path | None) -> None:
    """Start the MCP server over stdio."""
    _load_env(env_file)
    asyncio.run(serve())


@main.command(name="doctor")
@click.option(
    "--env-file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Path to .env file. Defaults to ~/.gecko/.env if present.",
)
def doctor_cmd(env_file: Path | None) -> None:
    """Verify env vars and connectivity. Exit non-zero on any failure."""
    _load_env(env_file)
    exit_code, report = run_doctor()
    stream = sys.stderr if exit_code != 0 else sys.stdout
    click.echo(report, file=stream)
    sys.exit(exit_code)


def _load_env(env_file: Path | None) -> None:
    if env_file is not None:
        load_dotenv(env_file)
        return
    # Walk up from CWD looking for a .env (project root), then fall back to ~/.gecko/.env.
    cwd_env = _find_dotenv_upwards(Path.cwd())
    if cwd_env is not None:
        load_dotenv(cwd_env)
        return
    default = Path.home() / ".gecko" / ".env"
    if default.exists():
        load_dotenv(default)


def _find_dotenv_upwards(start: Path) -> Path | None:
    for directory in (start, *start.parents):
        candidate = directory / ".env"
        if candidate.is_file():
            return candidate
    return None


main.add_command(wallet_group)
main.add_command(llm_group)
main.add_command(economics_cmd)
main.add_command(project_cmd)
main.add_command(quickstart_cmd)


if __name__ == "__main__":
    main()
