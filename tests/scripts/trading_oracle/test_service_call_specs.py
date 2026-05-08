"""Tests for the per-service x402 call-spec registry.

Light fakes only — see feedback_lighter_tests.md. We exercise the pure
matcher + body builders directly; no network, no httpx.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

# Load the spec module by file path — scripts/ isn't on sys.path by default.
# Same pattern as tests/scripts/trading_oracle/test_probe_service.py.
_SCRIPT_PATH = (
    Path(__file__).resolve().parents[3] / "scripts" / "trading_oracle" / "service_call_specs.py"
)
_spec = importlib.util.spec_from_file_location("trading_oracle_service_call_specs", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
specs_mod = importlib.util.module_from_spec(_spec)
sys.modules["trading_oracle_service_call_specs"] = specs_mod
_spec.loader.exec_module(specs_mod)

find_spec_for = specs_mod.find_spec_for


def test_finds_exa_spec() -> None:
    endpoints = [
        {"url": "https://api.exa.ai/contents", "method": "POST"},
        {"url": "https://api.exa.ai/search", "method": "POST"},
    ]
    spec, ep = find_spec_for("exa-ai", endpoints)
    assert spec is not None
    assert ep is not None
    assert ep["url"] == "https://api.exa.ai/search"
    assert spec.body_builder is not None
    body = spec.body_builder("hello", {})
    assert body is not None
    assert body["query"] == "hello"


def test_finds_chat_completions_for_venice() -> None:
    endpoints = [{"url": "https://api.venice.ai/api/v1/chat/completions", "method": "POST"}]
    spec, _ep = find_spec_for("docs-anthropic-com", endpoints)
    assert spec is not None
    assert spec.method == "POST"
    assert spec.body_builder is not None
    body = spec.body_builder("solana defi prompt", {})
    assert body is not None
    assert "messages" in body
    assert body["messages"][0]["content"] == "solana defi prompt"


def test_no_spec_returns_none_none() -> None:
    endpoints = [{"url": "https://random.example/foo", "method": "GET"}]
    spec, ep = find_spec_for("random-service", endpoints)
    assert spec is None and ep is None


def test_anthropic_messages_for_bankr() -> None:
    endpoints = [{"url": "https://llm.bankr.bot/v1/messages", "method": "POST"}]
    spec, _ep = find_spec_for("docs-anthropic-com", endpoints)
    assert spec is not None
    assert spec.body_builder is not None
    body = spec.body_builder("p", {})
    assert body is not None
    assert "max_tokens" in body
