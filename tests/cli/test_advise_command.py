"""Tests for `gecko advise` CLI command (S4-ADVISOR-03)."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
from click.testing import CliRunner
from gecko_cli.main import cli
from gecko_core.orchestration.advisor import AdvisorSessionNotFoundError
from gecko_core.orchestration.advisor.models import AdvisorVoice
from gecko_core.routing.catalog import AgentRole


def test_advise_runs_single_voice(monkeypatch: pytest.MonkeyPatch) -> None:
    sid = uuid4()
    canned = AdvisorVoice(
        role=AgentRole.ceo,
        model_used="moonshotai/kimi-k2.6",
        output_md="## CEO output\nbody body body\n",
        closing_line="Strategic priority: lock the LOI with Carta.",
        tokens_in=120,
        tokens_out=240,
        cost_usd=0.0034,
    )

    async def _fake(session_id: UUID | str, voice: str | Any, **_: Any) -> AdvisorVoice:
        assert UUID(str(session_id)) == sid
        return canned

    import gecko_core.orchestration.advisor as advisor_core

    monkeypatch.setattr(advisor_core, "generate_voice", _fake)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["advise", str(sid), "--voice", "ceo"],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0, result.output
    assert "Strategic priority" in result.output
    assert "kimi-k2.6" in result.output


def test_advise_session_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    sid = uuid4()

    async def _fake(*_a: Any, **_kw: Any) -> AdvisorVoice:
        raise AdvisorSessionNotFoundError(f"session {sid} not found")

    import gecko_core.orchestration.advisor as advisor_core

    monkeypatch.setattr(advisor_core, "generate_voice", _fake)

    runner = CliRunner()
    result = runner.invoke(cli, ["advise", str(sid), "--voice", "cto"])
    assert result.exit_code == 1
    assert "Not found" in result.output
