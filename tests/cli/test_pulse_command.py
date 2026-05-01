"""Tests for `gecko pulse` CLI command (S4-ADVISOR-05 + S14-PULSE-01)."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from click.testing import CliRunner
from gecko_cli.main import cli
from gecko_core.models import Verdict
from gecko_core.orchestration.advisor.models import (
    PANEL_VOICE_ORDER,
    AdvisorPanel,
    AdvisorVoice,
    PulseDelta,
    PulsePanel,
    PulseResult,
)


def _canned_panel(sid: UUID) -> AdvisorPanel:
    voices = [
        AdvisorVoice(
            role=role,
            model_used=f"stub/{role.value}",
            output_md="x",
            closing_line=f"{role.value}: ship it now",
            tokens_in=1,
            tokens_out=1,
            cost_usd=0.0,
        )
        for role in PANEL_VOICE_ORDER
    ]
    return AdvisorPanel(
        session_id=str(sid),
        voices=voices,
        total_cost_usd=0.0,
        generated_at=datetime.now().astimezone(),
    )


def _canned_pulse_result(parent_sid: UUID, pulse_sid: UUID) -> PulseResult:
    panel = _canned_panel(pulse_sid)
    return PulseResult(
        parent_session_id=str(parent_sid),
        pulse_session_id=str(pulse_sid),
        idea="An AI agent marketplace for legal contract review",
        verdict=Verdict.REFINE,
        gap_classification="Partial:segment",
        panel=panel,
        citations=[],
        summary_bullets=[
            "ceo: refine pricing for the SMB wedge",
            "cto: keep core stack, swap auth provider",
            "product_manager: narrow ICP to design teams",
        ],
    )


def _canned_legacy_pulse(sid: UUID, *, changed: bool) -> PulsePanel:
    panel = _canned_panel(sid)
    deltas = [
        PulseDelta(
            role=role,
            previous_closing_line=f"{role.value} prev" if changed else None,
            current_closing_line=f"{role.value}: ship it now",
            changed=changed,
            reason="closing line shifted vs prior pulse" if changed else "no prior pulse on file",
        )
        for role in PANEL_VOICE_ORDER
    ]
    return PulsePanel(panel=panel, deltas=deltas, previous_panel_at=None)


def test_pulse_v14_renders_verdict_and_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    """S14-PULSE-01: bb pulse <session_id> renders verdict + gap + bullets."""
    parent_sid = uuid4()
    pulse_sid = uuid4()

    async def _fake(parent_session_id: UUID | str, **_: Any) -> PulseResult:
        return _canned_pulse_result(parent_sid, pulse_sid)

    import gecko_core.orchestration.advisor as advisor_core

    monkeypatch.setattr(advisor_core, "run_pulse_v14", _fake)
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["pulse", str(parent_sid)],
        env={"COLUMNS": "240"},
    )
    assert result.exit_code == 0, result.output
    assert "Pulse" in result.output
    assert "REFINE" in result.output  # verdict surfaced
    assert "Partial:segment" in result.output  # gap surfaced
    assert "ceo:" in result.output  # summary bullet


def test_pulse_legacy_project_id_renders_delta_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy --project-id surface still renders the delta table."""
    pid = uuid4()

    async def _fake(
        session_id: UUID | str | None = None,
        *,
        project_id: UUID | str | None = None,
        **_: Any,
    ) -> PulsePanel:
        # Use a deterministic sid for the panel.
        return _canned_legacy_pulse(uuid4(), changed=True)

    import gecko_core.orchestration.advisor as advisor_core

    monkeypatch.setattr(advisor_core, "run_pulse", _fake)
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["pulse", "--project-id", str(pid)],
        env={"COLUMNS": "240"},
    )
    assert result.exit_code == 0, result.output
    assert "Pulse" in result.output
    assert "yes" in result.output.lower()
