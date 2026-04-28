"""Tests for `gecko-mcp project <project_id>` CLI command (S2-09).

Strategy: monkeypatch `GeckoAPIClient` inside the project command module
with a fake whose `get_project_economics` is an AsyncMock. We don't go
near httpx — that wiring is covered by the api_client tests.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from click.testing import CliRunner
from gecko_mcp import project as project_module
from gecko_mcp.api_client import GeckoAPIError


def _payload(
    *,
    name: str = "my-side-project",
    wallet_address: str | None = "8QURsr1234567890abcdefghijklmnoNQxV",
    privy_id: str | None = "wallet_abc123",
    balance: str | None = "5.000000",
    cap: str = "5.00",
    spent: str = "0.85",
    remaining: str = "4.15",
    sessions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "project_id": "6d7a3000-0000-0000-0000-000000000000",
        "name": name,
        "wallet": {
            "privy_wallet_id": privy_id,
            "privy_wallet_address": wallet_address,
            "balance_usdc": balance,
        },
        "budget": {
            "cap_usd": cap,
            "spent_usd": spent,
            "remaining_usd": remaining,
        },
        "recent_sessions": sessions
        if sessions is not None
        else [
            {
                "session_id": "sess-1",
                "tier": "basic",
                "cost_usd": "0.0122",
                "paid_usd": "0.10",
                "x402_tx_signature": "4fdh11111111111111111111111111111111F1Ee",
                "network": "solana-devnet",
                "created_at": "2026-04-28T14:30:00Z",
            },
            {
                "session_id": "sess-2",
                "tier": "pro",
                "cost_usd": "0.5000",
                "paid_usd": "0.75",
                "x402_tx_signature": "2ovo2222222222222222222222222222222VVb7a",
                "network": "solana-devnet",
                "created_at": "2026-04-28T13:50:00Z",
            },
        ],
    }


def _fake_client(method: AsyncMock) -> MagicMock:
    fake = MagicMock()
    fake.get_project_economics = method
    fake.aclose = AsyncMock(return_value=None)
    return fake


def test_project_command_renders_full_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    method = AsyncMock(return_value=_payload())
    monkeypatch.setattr(project_module, "GeckoAPIClient", lambda *a, **kw: _fake_client(method))

    runner = CliRunner()
    result = runner.invoke(project_module.project, ["6d7a3000-0000-0000-0000-000000000000"])

    assert result.exit_code == 0, result.output
    out = result.output
    assert "PROJECT  my-side-project" in out
    assert "wallet:  8QURsr...NQxV (privy)" in out
    assert "balance: $5.00 USDC" in out
    assert "$5.00 cap, $0.85 spent ($4.15 remaining)" in out
    assert "RECENT SESSIONS" in out
    # Tier column padded to 5 chars; tx truncated; network labelled.
    assert "basic  2026-04-28 14:30  $0.10  ->  4fdh...F1Ee (devnet)" in out
    assert "pro    2026-04-28 13:50  $0.75  ->  2ovo...Vb7a (devnet)" in out


def test_project_command_404_exits_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    method = AsyncMock(side_effect=GeckoAPIError("project 'abc' not found"))
    monkeypatch.setattr(project_module, "GeckoAPIClient", lambda *a, **kw: _fake_client(method))

    runner = CliRunner()
    result = runner.invoke(project_module.project, ["abc"])

    assert result.exit_code == 2, result.output
    assert "not found" in (result.output + (result.stderr if result.stderr_bytes else ""))


def test_project_command_no_wallet_provisioned(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _payload(wallet_address=None, privy_id=None, balance=None)
    method = AsyncMock(return_value=payload)
    monkeypatch.setattr(project_module, "GeckoAPIClient", lambda *a, **kw: _fake_client(method))

    runner = CliRunner()
    result = runner.invoke(project_module.project, ["6d7a3000-0000-0000-0000-000000000000"])

    assert result.exit_code == 0, result.output
    assert "no wallet provisioned yet" in result.output
    # Balance line is suppressed when no wallet exists.
    assert "balance:" not in result.output


def test_project_command_empty_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _payload(sessions=[])
    method = AsyncMock(return_value=payload)
    monkeypatch.setattr(project_module, "GeckoAPIClient", lambda *a, **kw: _fake_client(method))

    runner = CliRunner()
    result = runner.invoke(project_module.project, ["6d7a3000-0000-0000-0000-000000000000"])

    assert result.exit_code == 0, result.output
    assert "no sessions yet" in result.output


def test_project_command_balance_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wallet present but RPC returned null — render as 'unavailable'."""
    payload = _payload(balance=None)
    method = AsyncMock(return_value=payload)
    monkeypatch.setattr(project_module, "GeckoAPIClient", lambda *a, **kw: _fake_client(method))

    runner = CliRunner()
    result = runner.invoke(project_module.project, ["6d7a3000-0000-0000-0000-000000000000"])

    assert result.exit_code == 0, result.output
    assert "balance: unavailable" in result.output
