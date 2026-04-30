"""S8-CONFIG-02 — `bb doctor` resolves and validates the unified config plane.

Tests are pure: each row helper takes a dict and returns a CheckRow. The
memory roundtrip uses an in-memory fake that mirrors the parts of
``MemoryStore`` that the doctor touches (insert / list_by_scope / delete).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
from click.testing import CliRunner
from gecko_cli.commands.doctor import (
    check_frames_wallet,
    check_llm_resolution,
    check_memory_roundtrip,
    check_supabase_env,
    check_x402,
    doctor_cmd,
)

# ---------------------------------------------------------------------------
# Pure env-row tests
# ---------------------------------------------------------------------------


def test_check_supabase_env_ok() -> None:
    row = check_supabase_env(
        {"SUPABASE_URL": "https://abc.supabase.co", "SUPABASE_SERVICE_ROLE_KEY": "xyz1234"}
    )
    assert row.status == "ok"
    assert "abc.supabase.co" in row.detail
    # Secret value never echoed; only the tail.
    assert "xyz1234" not in row.detail
    assert "1234" in row.detail


def test_check_supabase_env_missing_url() -> None:
    row = check_supabase_env({})
    assert row.status == "err"
    assert "SUPABASE_URL" in row.detail


def test_check_supabase_env_missing_key() -> None:
    row = check_supabase_env({"SUPABASE_URL": "https://abc.supabase.co"})
    assert row.status == "err"
    assert "SERVICE_ROLE_KEY" in row.detail


def test_check_llm_resolution_via_router_env() -> None:
    row = check_llm_resolution({"LLM_ROUTER": "openrouter", "OPENROUTER_API_KEY": "or-test-12345"})
    assert row.status == "ok"
    assert "openrouter" in row.detail
    assert "or-test-12345" not in row.detail  # masked


def test_check_llm_resolution_warns_on_clawrouter_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No LLM_ROUTER + default x402 key → warn so prod deploys notice.

    We patch OrchestrationSettings construction so the test isn't sensitive
    to whatever GECKO_LLM_API_KEY the developer happens to have in their
    real .env / shell.
    """
    from gecko_core.orchestration import settings as settings_mod

    fake = settings_mod.OrchestrationSettings.model_construct(
        chat_model="openai/gpt-4o",
        llm_endpoint="http://localhost:8402/v1",
        llm_api_key="x402",
        max_input_tokens=60_000,
        temperature=0.3,
    )
    monkeypatch.setattr(settings_mod, "get_orchestration_settings", lambda: fake)
    row = check_llm_resolution({})
    assert row.status == "warn"


def test_check_x402_stub_default_ok() -> None:
    row = check_x402({})
    assert row.status == "ok"
    assert "stub" in row.detail


def test_check_x402_live_missing_treasury() -> None:
    row = check_x402({"X402_MODE": "live", "X402_FACILITATOR_URL": "https://x.com"})
    assert row.status == "err"
    assert "GECKO_WALLET_ADDRESS" in row.detail


def test_check_x402_live_complete() -> None:
    row = check_x402(
        {
            "X402_MODE": "live",
            "X402_FACILITATOR_URL": "https://facilitator.example",
            "GECKO_WALLET_ADDRESS": "SoLanaAddress",
        }
    )
    assert row.status == "ok"


def test_check_frames_wallet_warn_when_optional() -> None:
    # No env, no agent config file → warn (only required for X402_MODE=frames).
    row = check_frames_wallet({}, agent_token_reader=lambda: None)
    assert row.status == "warn"


def test_check_frames_wallet_err_when_mode_frames() -> None:
    row = check_frames_wallet({"X402_MODE": "frames"}, agent_token_reader=lambda: None)
    assert row.status == "err"


def test_check_frames_wallet_ok_with_token() -> None:
    row = check_frames_wallet(
        {"FRAMES_API_KEY": "fr-token-abc1234"},
        agent_token_reader=lambda: None,
    )
    assert row.status == "ok"
    assert "abc1234" not in row.detail  # masked
    assert "source=env" in row.detail


def test_check_frames_wallet_falls_back_to_agent_config() -> None:
    """S9-DOCTOR-01 / F14 — env unset, but ~/.agentwallet/config.json has the
    apiToken; doctor must report OK and surface the source.
    """
    row = check_frames_wallet({}, agent_token_reader=lambda: "agentwallet-tok-9999")
    assert row.status == "ok"
    assert "agentwallet-tok-9999" not in row.detail  # masked
    assert "source=agent-config" in row.detail


def test_check_frames_wallet_env_takes_precedence_over_file() -> None:
    """When both env and file are present, env wins (and source=env)."""
    row = check_frames_wallet(
        {"FRAMES_API_KEY": "env-tok-1111"},
        agent_token_reader=lambda: "file-tok-2222",
    )
    assert row.status == "ok"
    assert "source=env" in row.detail


def test_check_frames_wallet_err_when_mode_frames_and_neither_present() -> None:
    """F14 — X402_MODE=frames + nothing in env or file → err."""
    row = check_frames_wallet(
        {"X402_MODE": "frames"},
        agent_token_reader=lambda: None,
    )
    assert row.status == "err"
    assert "agentwallet" in row.detail.lower() or "config.json" in row.detail


# ---------------------------------------------------------------------------
# Memory roundtrip — uses a fake store
# ---------------------------------------------------------------------------


class _FakeMemoryStore:
    """In-memory ``MemoryStore`` look-alike for the roundtrip check."""

    def __init__(self, *, fail_read: bool = False) -> None:
        self._rows: dict[UUID, Any] = {}
        self._fail_read = fail_read

    async def insert(self, **kwargs: Any) -> UUID:
        import datetime as _dt

        from gecko_core.memory.models import MemoryEntry

        entry_id = uuid4()
        entry = MemoryEntry(
            id=entry_id,
            scope=kwargs["scope"],
            entry_type=kwargs["entry_type"],
            key=kwargs.get("key"),
            value=kwargs["value"],
            tx_signature=None,
            created_at=_dt.datetime.now(_dt.UTC),
        )
        self._rows[entry_id] = entry
        return entry_id

    async def list_by_scope(self, **kwargs: Any) -> list[Any]:
        if self._fail_read:
            return []
        scope = kwargs["scope"]
        return [r for r in self._rows.values() if r.scope == scope]

    async def delete(self, entry_id: UUID) -> bool:
        return self._rows.pop(entry_id, None) is not None


@pytest.mark.asyncio
async def test_check_memory_roundtrip_ok() -> None:
    store = _FakeMemoryStore()
    row = await check_memory_roundtrip(store)  # type: ignore[arg-type]
    assert row.status == "ok"


@pytest.mark.asyncio
async def test_check_memory_roundtrip_detects_write_read_split() -> None:
    """F7 — when a CLI write isn't visible to the same process's read, fail loud."""
    store = _FakeMemoryStore(fail_read=True)
    row = await check_memory_roundtrip(store)  # type: ignore[arg-type]
    assert row.status == "err"
    assert "F7" in row.detail or "different Supabase" in row.detail


# ---------------------------------------------------------------------------
# End-to-end CLI invocation — patch the memory store + env
# ---------------------------------------------------------------------------


def test_doctor_cmd_exits_0_on_all_green(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory
) -> None:
    """Happy path: all env present + memory roundtrip succeeds → exit 0."""
    fake = _FakeMemoryStore()

    monkeypatch.setattr(
        "gecko_core.memory.store.MemoryStore.from_env",
        classmethod(lambda cls: fake),
    )
    # Minimal env to satisfy the row checks.
    monkeypatch.setenv("SUPABASE_URL", "https://abc.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test-key-12345")
    monkeypatch.setenv("LLM_ROUTER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-12345")
    monkeypatch.delenv("X402_MODE", raising=False)
    monkeypatch.delenv("FRAMES_API_KEY", raising=False)
    # chdir to a clean dir — doctor now calls load_dotenv() and we don't
    # want the repo-root .env to alter the test fixture.
    monkeypatch.chdir(str(tmp_path))  # type: ignore[arg-type]

    result = CliRunner().invoke(doctor_cmd, [])
    assert result.exit_code == 0, result.output
    assert "All checks passed" in result.output


def test_doctor_cmd_loads_dotenv_from_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory
) -> None:
    """S9-DOCTOR-01 / F13 — `bb doctor` must pick up a project-local `.env`
    even when invoked outside the click group (e.g. directly imported by
    tests, or run with no --env-file). Before the fix, SUPABASE_URL=...
    in `.env` showed up as missing.
    """
    import os as _os
    from pathlib import Path as _Path

    cwd = _Path(str(tmp_path))  # type: ignore[arg-type]
    env_file = cwd / ".env"
    env_file.write_text(
        "SUPABASE_URL=https://from-dotenv.supabase.co\n"
        "SUPABASE_SERVICE_ROLE_KEY=rolekey-fromfile-9999\n"
        "LLM_ROUTER=openai\n"
        "OPENAI_API_KEY=sk-fromdotenv-9999\n"
    )

    fake = _FakeMemoryStore()
    monkeypatch.setattr(
        "gecko_core.memory.store.MemoryStore.from_env",
        classmethod(lambda cls: fake),
    )
    # Strip the relevant vars from os.environ so the only way they can reach
    # the doctor is through load_dotenv() reading the cwd .env file.
    for var in (
        "SUPABASE_URL",
        "SUPABASE_SERVICE_ROLE_KEY",
        "LLM_ROUTER",
        "OPENAI_API_KEY",
        "GECKO_LLM_ENDPOINT",
        "GECKO_LLM_API_KEY",
        "X402_MODE",
        "FRAMES_API_KEY",
        "FRAMES_AG_API_TOKEN",
    ):
        monkeypatch.delenv(var, raising=False)

    monkeypatch.chdir(cwd)
    result = CliRunner().invoke(doctor_cmd, [])
    # Proof: load_dotenv() ran before _run_all sampled os.environ. We assert
    # on os.environ directly (Rich may truncate long URLs in the table cell).
    assert result.exit_code == 0, result.output
    assert _os.environ.get("SUPABASE_URL") == "https://from-dotenv.supabase.co"
    assert _os.environ.get("OPENAI_API_KEY") == "sk-fromdotenv-9999"


def test_doctor_cmd_exits_1_on_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory
) -> None:
    """Missing SUPABASE_URL → exit 1 and the failed row count is printed."""
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    # Doctor now runs load_dotenv() on its own cwd. chdir to a clean dir
    # so the repo-root `.env` doesn't repopulate SUPABASE_URL.
    monkeypatch.chdir(str(tmp_path))  # type: ignore[arg-type]

    result = CliRunner().invoke(doctor_cmd, [])
    assert result.exit_code == 1
    assert "FAIL" in result.output or "failed" in result.output
