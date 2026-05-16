"""S24 WS-E — doctor surface additions.

The doctor was already comprehensive (env + supabase + mongo + voyage +
x402 + wallet + ClawRouter + MCP registration). WS-E adds:

  * ANTHROPIC_API_KEY surfaced as INFO under OPTIONAL_ENV_VARS
  * HELIUS_API_KEY surfaced as INFO under OPTIONAL_ENV_VARS
  * Cheap OpenAI /v1/models live probe under --live

These tests pin the new behaviour without re-asserting the long-standing
checks (covered by tests/mcp/test_doctor.py).
"""

from __future__ import annotations

from gecko_mcp.doctor import (
    OPTIONAL_ENV_VARS,
    check_openai_live,
    check_optional_env,
)


def test_anthropic_key_is_optional_info_when_unset() -> None:
    """ANTHROPIC_API_KEY surfaces as INFO with the eval-harness hint."""
    assert "ANTHROPIC_API_KEY" in OPTIONAL_ENV_VARS
    rows = check_optional_env({})
    anthropic_rows = [r for r in rows if r.name == "env:ANTHROPIC_API_KEY"]
    assert len(anthropic_rows) == 1
    row = anthropic_rows[0]
    assert row.ok is True
    assert row.info is True
    assert "eval-harness" in row.detail


def test_helius_key_is_optional_info_when_unset() -> None:
    """HELIUS_API_KEY surfaces as INFO with the trader-mode hint."""
    assert "HELIUS_API_KEY" in OPTIONAL_ENV_VARS
    rows = check_optional_env({})
    helius_rows = [r for r in rows if r.name == "env:HELIUS_API_KEY"]
    assert len(helius_rows) == 1
    assert "trader mode" in helius_rows[0].detail


def test_openai_live_skipped_when_key_unset() -> None:
    """No OPENAI_API_KEY → INFO row, never a FAIL."""
    row = check_openai_live({})
    assert row.ok is True
    assert row.info is True
    assert "skipped" in row.detail


def test_openai_live_redacts_key_on_failure(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A network failure must not echo OPENAI_API_KEY."""
    secret = "sk-secret-test-key-do-not-leak-1234"

    import httpx as real_httpx

    def _boom_get(*_a, **_k):  # type: ignore[no-untyped-def]
        # Even if the exception message echoes the key, the doctor must
        # surface only the class name — never the underlying ``str(exc)``.
        raise RuntimeError(f"connect failed; auth header was Bearer {secret}")

    monkeypatch.setattr(real_httpx, "get", _boom_get)
    row = check_openai_live({"OPENAI_API_KEY": secret})
    assert row.ok is False
    assert secret not in row.detail
    assert "OpenAI unreachable" in row.detail
