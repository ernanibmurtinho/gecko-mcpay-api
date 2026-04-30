"""Tests for the POST /pulse endpoint (S8-API-01).

Free in stub mode and free by default in live mode (PULSE_CALL_PRICE
defaults to "$0.00" -> not registered on x402). Asserts:

    1. Returns the PulsePanel JSON.
    2. Missing both session_id and project_id -> 400.
    3. Invalid session_id -> 400.
    4. project_id wins when both are given.
    5. /.well-known/x402 does NOT advertise /pulse with the default $0 price.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("X402_MODE", "stub")
os.environ.setdefault("GECKO_WALLET_ADDRESS", "STUB_TEST_WALLET")
os.environ.pop("PULSE_CALL_PRICE", None)


@pytest.fixture
def client() -> Iterator[TestClient]:
    os.environ.pop("PULSE_CALL_PRICE", None)
    for mod in [m for m in sys.modules if m.startswith("gecko_api")]:
        sys.modules.pop(mod, None)

    from gecko_api.main import app
    from gecko_core.orchestration.advisor.models import (
        PANEL_VOICE_ORDER,
        AdvisorPanel,
        AdvisorVoice,
        PulseDelta,
        PulsePanel,
    )

    voices = [
        AdvisorVoice(
            role=role,
            model_used=f"stub/{role.value}",
            output_md=f"# {role.value}",
            closing_line=f"closing-{role.value}",
            tokens_in=10,
            tokens_out=20,
            cost_usd=0.0,
        )
        for role in PANEL_VOICE_ORDER
    ]
    deltas = [
        PulseDelta(
            role=role,
            previous_closing_line=None,
            current_closing_line=f"closing-{role.value}",
            changed=False,
            reason=None,
        )
        for role in PANEL_VOICE_ORDER
    ]
    panel = AdvisorPanel(
        session_id="00000000-0000-0000-0000-000000000111",
        voices=voices,
        total_cost_usd=0.0,
        generated_at=datetime.now(tz=UTC),
    )
    fake_pulse = PulsePanel(
        panel=panel,
        deltas=deltas,
        previous_panel_at=None,
    )

    with (
        patch(
            "gecko_core.orchestration.advisor.run_pulse",
            new=AsyncMock(return_value=fake_pulse),
        ),
        TestClient(app) as c,
    ):
        yield c


def test_pulse_returns_panel_for_session(client: TestClient) -> None:
    r = client.post(
        "/pulse",
        json={"session_id": "00000000-0000-0000-0000-000000000111"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # PulsePanel nests AdvisorPanel under "panel".
    assert body["panel"]["session_id"] == "00000000-0000-0000-0000-000000000111"
    assert len(body["panel"]["voices"]) == 5
    assert len(body["deltas"]) == 5


def test_pulse_returns_panel_for_project(client: TestClient) -> None:
    r = client.post(
        "/pulse",
        json={"project_id": "00000000-0000-0000-0000-000000000222"},
    )
    assert r.status_code == 200, r.text


def test_pulse_requires_session_or_project(client: TestClient) -> None:
    r = client.post("/pulse", json={})
    assert r.status_code == 400, r.text
    assert "session_id or project_id" in r.text


def test_pulse_invalid_session_id(client: TestClient) -> None:
    r = client.post("/pulse", json={"session_id": "not-a-uuid"})
    assert r.status_code == 400, r.text
    assert "invalid session_id" in r.text


def test_pulse_passes_project_id_to_run_pulse() -> None:
    for mod in [m for m in sys.modules if m.startswith("gecko_api")]:
        sys.modules.pop(mod, None)
    from gecko_api.main import app
    from gecko_core.orchestration.advisor.models import (
        PANEL_VOICE_ORDER,
        AdvisorPanel,
        AdvisorVoice,
        PulsePanel,
    )

    voices = [
        AdvisorVoice(
            role=role,
            model_used="stub",
            output_md="",
            closing_line="x",
            tokens_in=0,
            tokens_out=0,
            cost_usd=0.0,
        )
        for role in PANEL_VOICE_ORDER
    ]
    panel = AdvisorPanel(
        session_id="00000000-0000-0000-0000-000000000111",
        voices=voices,
        total_cost_usd=0.0,
        generated_at=datetime.now(tz=UTC),
    )
    fake = PulsePanel(panel=panel, deltas=[], previous_panel_at=None)
    captured: dict[str, object] = {}

    async def _spy(**kwargs: object) -> PulsePanel:
        captured.update(kwargs)
        return fake

    with (
        patch("gecko_core.orchestration.advisor.run_pulse", new=_spy),
        TestClient(app) as c,
    ):
        r = c.post(
            "/pulse",
            json={
                "session_id": "00000000-0000-0000-0000-000000000111",
                "project_id": "00000000-0000-0000-0000-000000000333",
            },
        )
    assert r.status_code == 200, r.text
    # Both are forwarded; the core ``run_pulse`` resolves project_id
    # precedence internally (project_id wins).
    assert captured["session_id"] == UUID("00000000-0000-0000-0000-000000000111")
    assert captured["project_id"] == UUID("00000000-0000-0000-0000-000000000333")


def test_well_known_does_not_advertise_pulse_at_default_price(client: TestClient) -> None:
    r = client.get("/.well-known/x402")
    assert r.status_code == 200
    routes = {entry["route"] for entry in r.json()["routes"]}
    assert "POST /pulse" not in routes
