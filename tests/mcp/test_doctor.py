"""Tests for `gecko-mcp doctor`.

The doctor is the canary for a clean Phase 8 install — these tests pin its
behaviour (exit codes, redaction, named-missing-vars) because the install
docs in `gecko-mcpay-skills` script around them.
"""

from __future__ import annotations

from typing import Any

import pytest
from gecko_mcp.doctor import (
    REQUIRED_ENV_VARS,
    REQUIRED_EXTENSIONS,
    REQUIRED_FUNCTIONS,
    REQUIRED_TABLES,
    SERVER_SIDE_ENV_VARS,
    CheckResult,
    _is_thin_client,
    check_mcp_registered,
    run_doctor,
)


class _FakeSupabase:
    """Stand-in supabase client. Returns a manifest dict from `rpc`."""

    def __init__(self, manifest: dict[str, list[str]]) -> None:
        self._manifest = manifest

    def rpc(self, fn: str, params: dict[str, object]) -> Any:
        if fn == "gecko_doctor_ping":
            return {"ok": True}
        if fn == "gecko_doctor_manifest":
            return self._manifest
        raise RuntimeError(f"unexpected rpc: {fn}")


# Sentinel keys present in server-stack run_doctor tests.
# EMBED_PROVIDER defaults to voyage so VOYAGE_API_KEY must be set for exit_code 0.
_VOYAGE_KEY = "pa-test-key-ok-1234"
_OPENAI_KEY = "sk-test-openai-postgres-ann-ok-123456"


def test_doctor_thin_client_passes_with_empty_env() -> None:
    """Thin-client install: no server-side keys required — doctor must pass."""
    exit_code, report = run_doctor(environ={}, supabase_client=None)
    # Server-side keys (SUPABASE_URL etc.) surface as INFO, never FAIL.
    assert exit_code == 0, report
    assert "doctor: OK" in report
    # Server-side vars are mentioned as INFO so the user is aware.
    for var in SERVER_SIDE_ENV_VARS:
        assert var in report, f"doctor must mention server-side var {var}"


def test_doctor_fails_when_required_env_missing() -> None:
    """If REQUIRED_ENV_VARS is non-empty, missing vars should fail the doctor."""
    if not REQUIRED_ENV_VARS:
        pytest.skip("REQUIRED_ENV_VARS is empty (thin-client model — no required keys)")
    exit_code, report = run_doctor(environ={}, supabase_client=None)
    assert exit_code == 1
    assert "doctor: FAIL" in report
    for var in REQUIRED_ENV_VARS:
        assert var in report, f"doctor must name the missing var {var}"


def test_doctor_redacts_secrets_in_report() -> None:
    # Sanity: even when server-side vars are present, their values must not
    # be echoed — only the var name surfaces.
    env = {
        "SUPABASE_URL": "https://x.supabase.co",
        # missing the rest
    }
    _, report = run_doctor(environ=env, supabase_client=None)
    assert "https://x.supabase.co" not in report  # only the var name should appear
    assert "SUPABASE_URL" in report  # confirms we mention it without the value


def test_doctor_x402_default_stub_is_ok() -> None:
    # Include server-side creds so Supabase probes are exercised via the
    # injected fake client.
    env = {var: "x" for var in SERVER_SIDE_ENV_VARS} | {
        "VOYAGE_API_KEY": _VOYAGE_KEY,
        "OPENAI_API_KEY": _OPENAI_KEY,
    }
    manifest = {
        "extensions": list(REQUIRED_EXTENSIONS),
        "tables": list(REQUIRED_TABLES),
        "functions": list(REQUIRED_FUNCTIONS),
    }
    exit_code, report = run_doctor(environ=env, supabase_client=_FakeSupabase(manifest))
    assert exit_code == 0, report
    assert "doctor: OK" in report
    assert "defaulting to 'stub'" in report


def test_doctor_x402_invalid_value_fails() -> None:
    env = {var: "x" for var in SERVER_SIDE_ENV_VARS} | {
        "X402_MODE": "bogus",
        "OPENAI_API_KEY": _OPENAI_KEY,
    }
    exit_code, report = run_doctor(environ=env, supabase_client=_FakeSupabase({}))
    assert exit_code == 1
    assert "X402_MODE" in report


def test_doctor_passes_with_full_env_and_migrations() -> None:
    env = {var: "x" for var in SERVER_SIDE_ENV_VARS} | {
        "X402_MODE": "stub",
        "VOYAGE_API_KEY": _VOYAGE_KEY,
        "OPENAI_API_KEY": _OPENAI_KEY,
    }
    manifest = {
        "extensions": list(REQUIRED_EXTENSIONS),
        "tables": list(REQUIRED_TABLES),
        "functions": list(REQUIRED_FUNCTIONS),
    }
    exit_code, report = run_doctor(environ=env, supabase_client=_FakeSupabase(manifest))
    assert exit_code == 0, report
    assert "doctor: OK" in report
    for tbl in REQUIRED_TABLES:
        assert tbl in report


def test_doctor_fails_when_migrations_missing() -> None:
    env = {var: "x" for var in SERVER_SIDE_ENV_VARS} | {
        "X402_MODE": "stub",
        "VOYAGE_API_KEY": _VOYAGE_KEY,
        "OPENAI_API_KEY": _OPENAI_KEY,
    }
    manifest: dict[str, list[str]] = {"extensions": [], "tables": [], "functions": []}
    exit_code, report = run_doctor(environ=env, supabase_client=_FakeSupabase(manifest))
    assert exit_code == 1
    for tbl in REQUIRED_TABLES:
        assert f"missing table: {tbl}" in report
    for ext in REQUIRED_EXTENSIONS:
        assert f"missing extension: {ext}" in report
    for fn in REQUIRED_FUNCTIONS:
        assert f"missing function: {fn}" in report


def test_doctor_gecko_api_unset_is_info_only(monkeypatch: pytest.MonkeyPatch) -> None:
    from gecko_mcp.doctor import check_gecko_api

    result = check_gecko_api(environ={})
    assert result.ok is True
    assert result.info is True
    assert "unset" in result.detail


def test_doctor_gecko_api_unreachable_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    from gecko_mcp.doctor import check_gecko_api

    def _raise(*_args: object, **_kwargs: object) -> object:
        raise ConnectionError("nope")

    monkeypatch.setattr("httpx.get", _raise)
    result = check_gecko_api(environ={"GECKO_API_URL": "http://127.0.0.1:1"})
    assert result.ok is False
    assert "unreachable" in result.detail


def test_doctor_gecko_api_reachable_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx
    from gecko_mcp.doctor import check_gecko_api

    def _ok(*_args: object, **_kwargs: object) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok", "payments": "stub"})

    monkeypatch.setattr("httpx.get", _ok)
    result = check_gecko_api(environ={"GECKO_API_URL": "http://localhost:8000"})
    assert result.ok is True
    assert result.info is False
    assert "reachable" in result.detail
    assert "payments=stub" in result.detail


def test_voyage_check_skipped_when_reranker_off() -> None:
    """S19-VOYAGE-API-KEY-01 — reranker unset → INFO row only, no api_key row."""
    from gecko_mcp.doctor import check_voyage_api_key

    rows = check_voyage_api_key(environ={})
    names = {r.name for r in rows}
    assert "reranker:kind" in names
    assert "voyage:api_key" not in names
    [info] = [r for r in rows if r.name == "reranker:kind"]
    assert info.ok is True
    assert info.info is True
    assert info.detail == "none"


def test_voyage_check_fails_when_key_missing() -> None:
    """GECKO_RERANKER=voyage with no VOYAGE_API_KEY → exit 1, names the var."""
    env = {var: "x" for var in SERVER_SIDE_ENV_VARS} | {
        "X402_MODE": "stub",
        "GECKO_RERANKER": "voyage",
    }
    manifest = {
        "extensions": list(REQUIRED_EXTENSIONS),
        "tables": list(REQUIRED_TABLES),
        "functions": list(REQUIRED_FUNCTIONS),
    }
    exit_code, report = run_doctor(environ=env, supabase_client=_FakeSupabase(manifest))
    assert exit_code == 1, report
    assert "VOYAGE_API_KEY" in report
    assert "voyage:api_key" in report


def test_voyage_check_passes_with_key() -> None:
    """Both env vars set → ok=True row, secret never appears in rendered output."""
    secret = "pa-test-secret-value-do-not-log"
    env = {var: "x" for var in SERVER_SIDE_ENV_VARS} | {
        "X402_MODE": "stub",
        "GECKO_RERANKER": "voyage",
        "VOYAGE_API_KEY": secret,
        "OPENAI_API_KEY": _OPENAI_KEY,
    }
    manifest = {
        "extensions": list(REQUIRED_EXTENSIONS),
        "tables": list(REQUIRED_TABLES),
        "functions": list(REQUIRED_FUNCTIONS),
    }
    exit_code, report = run_doctor(environ=env, supabase_client=_FakeSupabase(manifest))
    assert exit_code == 0, report
    assert "voyage:api_key" in report
    # The full secret must NEVER appear in rendered output.
    assert secret not in report
    # Only the prefix sentinel + last-4 suffix may surface.
    assert "pa-..." in report
    assert secret[-4:] in report  # last 4 chars are the only slice we expose


def test_embed_provider_voyage_default_no_key_thin_client() -> None:
    """S22-VOYAGE-EMBED — thin-client install: no server keys → key check skipped (INFO)."""
    from gecko_mcp.doctor import check_embed_provider

    # No server-side keys → thin-client mode → key validation skipped.
    rows = check_embed_provider(environ={})
    names = {r.name for r in rows}
    assert "embed:provider" in names
    # Key check must be INFO (skipped), not FAIL.
    fail_rows = [r for r in rows if not r.ok]
    assert not fail_rows, f"thin-client mode must not fail embed checks: {fail_rows}"


def test_embed_provider_voyage_default_no_key_server_stack() -> None:
    """S22-VOYAGE-EMBED — server-stack install: EMBED_PROVIDER=voyage, no key → FAIL."""
    from gecko_mcp.doctor import check_embed_provider

    # SUPABASE_URL present signals server stack → key validation is enforced.
    rows = check_embed_provider(environ={"SUPABASE_URL": "https://x.supabase.co"})
    names = {r.name for r in rows}
    assert "embed:provider" in names
    assert "embed:voyage_api_key" in names
    fail_row = next(r for r in rows if r.name == "embed:voyage_api_key")
    assert fail_row.ok is False
    assert "VOYAGE_API_KEY" in fail_row.detail


def test_embed_provider_voyage_with_key() -> None:
    """EMBED_PROVIDER=voyage + valid key on server stack → PASS, secret never in output."""
    from gecko_mcp.doctor import check_embed_provider

    secret = "pa-test-secret-do-not-log"
    rows = check_embed_provider(
        environ={
            "EMBED_PROVIDER": "voyage",
            "VOYAGE_API_KEY": secret,
            "SUPABASE_URL": "https://x.supabase.co",  # server-stack signal
            "OPENAI_API_KEY": "sk-test-openai-embed-12345678",
        }
    )
    fail_rows = [r for r in rows if not r.ok]
    assert not fail_rows, fail_rows
    key_row = next(r for r in rows if r.name == "embed:voyage_api_key")
    assert key_row.ok is True
    assert secret not in key_row.detail
    assert "pa-..." in key_row.detail
    assert secret[-4:] in key_row.detail


def test_embed_provider_voyage_supabase_without_openai_fails() -> None:
    """Voyage + Supabase without OPENAI_API_KEY → Postgres ANN cannot run."""
    from gecko_mcp.doctor import check_embed_provider

    rows = check_embed_provider(
        environ={
            "EMBED_PROVIDER": "voyage",
            "VOYAGE_API_KEY": "pa-test-has-voyage-only",
            "SUPABASE_URL": "https://x.supabase.co",
        }
    )
    fail = next(r for r in rows if r.name == "embed:openai_for_postgres_ann")
    assert fail.ok is False
    assert "OPENAI_API_KEY" in fail.detail


def test_embed_provider_openai_no_key() -> None:
    """EMBED_PROVIDER=openai with no OPENAI_API_KEY → FAIL when server stack present."""
    from gecko_mcp.doctor import check_embed_provider

    # SUPABASE_URL signals server stack → key validation enforced.
    rows = check_embed_provider(
        environ={"EMBED_PROVIDER": "openai", "SUPABASE_URL": "https://x.supabase.co"}
    )
    fail_row = next((r for r in rows if not r.ok), None)
    assert fail_row is not None
    assert "OPENAI_API_KEY" in fail_row.detail


def test_embed_provider_openai_with_key() -> None:
    """EMBED_PROVIDER=openai + OPENAI_API_KEY set → PASS on server stack."""
    from gecko_mcp.doctor import check_embed_provider

    rows = check_embed_provider(
        environ={
            "EMBED_PROVIDER": "openai",
            "OPENAI_API_KEY": "sk-test-key-1234",
            "SUPABASE_URL": "https://x.supabase.co",  # server-stack signal
        }
    )
    fail_rows = [r for r in rows if not r.ok]
    assert not fail_rows, fail_rows


def test_embed_provider_unknown_fails() -> None:
    """Unrecognised EMBED_PROVIDER → FAIL with helpful message (server stack)."""
    from gecko_mcp.doctor import check_embed_provider

    # Server-stack signal so the provider validation is enforced.
    rows = check_embed_provider(
        environ={"EMBED_PROVIDER": "cohere", "SUPABASE_URL": "https://x.supabase.co"}
    )
    fail_rows = [r for r in rows if not r.ok]
    assert fail_rows
    assert "cohere" in fail_rows[0].detail


def test_embed_provider_in_run_doctor_fails_without_voyage_key() -> None:
    """run_doctor exit_code=1 when EMBED_PROVIDER=voyage and VOYAGE_API_KEY missing."""
    # Server-side creds present so Supabase probes run via injected fake.
    env = {var: "x" for var in SERVER_SIDE_ENV_VARS} | {
        "X402_MODE": "stub",
        "EMBED_PROVIDER": "voyage",
    }
    manifest = {
        "extensions": list(REQUIRED_EXTENSIONS),
        "tables": list(REQUIRED_TABLES),
        "functions": list(REQUIRED_FUNCTIONS),
    }
    exit_code, report = run_doctor(environ=env, supabase_client=_FakeSupabase(manifest))
    assert exit_code == 1, report
    assert "embed:voyage_api_key" in report


def test_doctor_cli_passes_with_no_server_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end: thin-client with no server-side keys → exit 0 (doctor: OK).

    This is the key regression guard: an external user who only has
    GECKO_API_URL + X402_MODE must be able to run `gecko-mcp doctor` and see
    green without supplying SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, or
    TAVILY_API_KEY.
    """
    from click.testing import CliRunner
    from gecko_mcp.cli import main

    # Clear all server-side keys so the test is a clean thin-client simulation.
    for var in (
        *SERVER_SIDE_ENV_VARS,
        "X402_MODE",
        "VOYAGE_API_KEY",
        "EMBED_PROVIDER",
        "GECKO_API_URL",
        "GECKO_RERANKER",
        # Force supabase chunk store so the test doesn't try to reach MongoDB Atlas.
        # An external user won't have GECKO_CHUNK_STORE set at all — the default is
        # supabase which returns a single INFO row without any network probe.
        "GECKO_CHUNK_STORE",
        "MONGODB_URI",
    ):
        monkeypatch.delenv(var, raising=False)
    # Don't auto-load ~/.gecko/.env during the test.
    monkeypatch.setattr("gecko_mcp.cli._load_env", lambda _: None)

    runner = CliRunner()
    result = runner.invoke(main, ["doctor"], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    assert "doctor: OK" in result.output
    # Server-side vars must be mentioned as INFO (not FAIL).
    for var in SERVER_SIDE_ENV_VARS:
        assert var in result.output


# ---------------------------------------------------------------------------
# S22-MCP-06 — _is_thin_client() and thin-client doctor gating
# ---------------------------------------------------------------------------


def test_is_thin_client_unset_returns_false() -> None:
    """No GECKO_API_URL → local dev mode, not thin client."""
    assert _is_thin_client({}) is False


def test_is_thin_client_localhost_returns_false() -> None:
    """localhost GECKO_API_URL → local dev mode, not thin client."""
    assert _is_thin_client({"GECKO_API_URL": "http://localhost:8000"}) is False


def test_is_thin_client_127_returns_false() -> None:
    """127.0.0.1 GECKO_API_URL → local dev mode, not thin client."""
    assert _is_thin_client({"GECKO_API_URL": "http://127.0.0.1:8000"}) is False


def test_is_thin_client_remote_returns_true() -> None:
    """Remote GECKO_API_URL → thin client."""
    assert _is_thin_client({"GECKO_API_URL": "https://api.geckovision.tech"}) is True


def test_is_thin_client_staging_returns_true() -> None:
    """Staging GECKO_API_URL → thin client (not localhost)."""
    assert _is_thin_client({"GECKO_API_URL": "https://staging.geckovision.tech"}) is True


def test_run_doctor_thin_client_skips_server_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    """S22-MCP-06: when GECKO_API_URL is remote, server-side checks must not appear.

    The core bug was: with server-side env vars present (developer machine),
    _is_thin_client() returned False and VOYAGE_API_KEY/embed checks ran.
    With the new detection (GECKO_API_URL remote = thin client), those checks
    are skipped even when the developer has local .env keys.
    """
    import httpx

    # Simulate a developer machine that has local server keys (like in .env)
    # but has GECKO_API_URL pointing at the remote production API.
    env = {
        "GECKO_API_URL": "https://api.geckovision.tech",
        # Developer machine has these locally but thin-client doctor must ignore them.
        # They must NOT appear in the report (thin-client gate skips server-side checks).
        "SUPABASE_URL": "https://x.supabase.co",
        "SUPABASE_SERVICE_ROLE_KEY": "service-role-key",
        "TAVILY_API_KEY": "tvly-key",
        "VOYAGE_API_KEY": "pa-test-key-1234",
    }

    # Stub out the healthz probe so we don't make a real network call.
    def _ok(*_args: object, **_kwargs: object) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok"})

    monkeypatch.setattr("httpx.get", _ok)

    exit_code, report = run_doctor(environ=env, supabase_client=None)
    assert exit_code == 0, report
    # Thin client: server-side section skipped entirely — keys don't appear.
    assert "SUPABASE_URL" not in report
    assert "TAVILY_API_KEY" not in report
    # No embed/reranker/chunk-store/clawrouter rows for thin clients.
    assert "embed:" not in report
    assert "reranker:" not in report
    assert "chunk_store:" not in report
    assert "llm:clawrouter" not in report
    # Supabase probe must NOT run even though creds are in env.
    assert "supabase:" not in report


def test_run_doctor_local_dev_still_shows_server_checks() -> None:
    """When GECKO_API_URL is absent/localhost, server-side checks still run."""
    # Empty env = local dev = _is_thin_client() False.
    exit_code, report = run_doctor(environ={}, supabase_client=None)
    assert exit_code == 0, report
    # Server-side vars surface as INFO in local dev mode.
    for var in SERVER_SIDE_ENV_VARS:
        assert var in report, f"local dev mode must surface {var}"


# ---------------------------------------------------------------------------
# S25-DOC-01/02/04 — thin-client hardening
# ---------------------------------------------------------------------------


def _mock_healthy_api(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch httpx.get to return a healthy gecko-api response."""
    import httpx

    def _ok(*_a: object, **_kw: object) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok", "payments": "stub"})

    monkeypatch.setattr("httpx.get", _ok)


def test_thin_client_fails_without_wallet(monkeypatch: pytest.MonkeyPatch) -> None:
    """S25-DOC-02: thin-client + no wallet → payments:wallet_required FAIL."""
    _mock_healthy_api(monkeypatch)
    # No wallet addresses returned by check_wallet.
    monkeypatch.setattr("gecko_mcp.doctor.check_wallet", lambda: [])
    # MCP registration check is advisory (INFO) — stub it to avoid subprocess.
    monkeypatch.setattr(
        "gecko_mcp.doctor.check_mcp_registered",
        lambda: CheckResult(name="mcp:registered", ok=True, detail="test", info=True),
    )
    exit_code, report = run_doctor(
        environ={"GECKO_API_URL": "https://api.geckovision.tech"},
        supabase_client=None,
    )
    assert exit_code == 1, report
    assert "wallet_required" in report
    assert "gecko-mcp wallet new" in report


def test_thin_client_passes_with_wallet(monkeypatch: pytest.MonkeyPatch) -> None:
    """S25-DOC-02: thin-client + wallet present → passes."""
    _mock_healthy_api(monkeypatch)
    monkeypatch.setattr(
        "gecko_mcp.doctor.check_wallet",
        lambda: [
            CheckResult(name="payments:provider", ok=True, detail="frames", info=True),
            CheckResult(name="payments:solana", ok=True, detail="SomeAddr123", info=True),
        ],
    )
    monkeypatch.setattr(
        "gecko_mcp.doctor.check_mcp_registered",
        lambda: CheckResult(name="mcp:registered", ok=True, detail="registered", info=False),
    )
    exit_code, report = run_doctor(
        environ={"GECKO_API_URL": "https://api.geckovision.tech"},
        supabase_client=None,
    )
    assert exit_code == 0, report
    assert "doctor: OK" in report


def test_thin_client_skips_server_side_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Thin-client mode must not surface server-side check names (SUPABASE_URL etc.)."""
    _mock_healthy_api(monkeypatch)
    monkeypatch.setattr(
        "gecko_mcp.doctor.check_wallet",
        lambda: [CheckResult(name="payments:solana", ok=True, detail="x", info=True)],
    )
    monkeypatch.setattr(
        "gecko_mcp.doctor.check_mcp_registered",
        lambda: CheckResult(name="mcp:registered", ok=True, detail="ok", info=True),
    )
    _, report = run_doctor(
        environ={"GECKO_API_URL": "https://api.geckovision.tech"},
        supabase_client=None,
    )
    # Server-side check names must NOT appear in a thin-client report.
    for var in SERVER_SIDE_ENV_VARS:
        assert var not in report, f"thin-client report must not contain {var}"


def test_check_mcp_registered_claude_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """S25-DOC-04: claude CLI absent → INFO (not FAIL)."""
    monkeypatch.setattr("shutil.which", lambda _cmd: None)
    result = check_mcp_registered()
    assert result.ok is True
    assert result.info is True
    assert "claude" in result.detail.lower()


def test_check_mcp_registered_gecko_listed(monkeypatch: pytest.MonkeyPatch) -> None:
    """S25-DOC-04: claude mcp list contains gecko → PASS."""
    import shutil

    monkeypatch.setattr(
        "shutil.which", lambda cmd: "/usr/bin/claude" if cmd == "claude" else shutil.which(cmd)
    )
    monkeypatch.setattr("gecko_mcp.doctor._run_claude_mcp_list", lambda: "gecko  gecko-mcp serve\n")
    result = check_mcp_registered()
    assert result.ok is True
    assert result.info is False
    assert "registered" in result.detail


def test_check_mcp_registered_gecko_not_listed(monkeypatch: pytest.MonkeyPatch) -> None:
    """S25-DOC-04: claude found but gecko not in list → INFO with hint."""
    import shutil

    monkeypatch.setattr(
        "shutil.which", lambda cmd: "/usr/bin/claude" if cmd == "claude" else shutil.which(cmd)
    )
    monkeypatch.setattr("gecko_mcp.doctor._run_claude_mcp_list", lambda: "some-other-server\n")
    result = check_mcp_registered()
    assert result.ok is True
    assert result.info is True
    assert "claude mcp add gecko" in result.detail
