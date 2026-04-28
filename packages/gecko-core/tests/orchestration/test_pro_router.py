"""Unit tests for the LLM router resolution (S1-01)."""

from __future__ import annotations

import pytest


def test_default_router_is_openai() -> None:
    from gecko_core.orchestration.pro.router import resolve_router

    cfg = resolve_router({"OPENAI_API_KEY": "sk-test"})
    assert cfg.router == "openai"
    assert cfg.base_url == "https://api.openai.com/v1"
    assert cfg.api_key == "sk-test"
    assert cfg.extra_headers == {}


def test_openai_router_requires_openai_key() -> None:
    from gecko_core.orchestration.pro.router import resolve_router

    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        resolve_router({"LLM_ROUTER": "openai"})


def test_openrouter_router_resolves_with_referer_and_title() -> None:
    from gecko_core.orchestration.pro.router import resolve_router

    cfg = resolve_router({"LLM_ROUTER": "openrouter", "OPENROUTER_API_KEY": "or-test"})
    assert cfg.router == "openrouter"
    assert cfg.base_url == "https://openrouter.ai/api/v1"
    assert cfg.api_key == "or-test"
    # OpenRouter analytics headers must be present.
    assert "HTTP-Referer" in cfg.extra_headers
    assert "X-Title" in cfg.extra_headers


def test_openrouter_router_raises_on_missing_key() -> None:
    """Fail fast at startup, not at first agent reply."""
    from gecko_core.orchestration.pro.router import resolve_router

    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        resolve_router({"LLM_ROUTER": "openrouter"})


def test_clawrouter_router_default_url_no_auth() -> None:
    from gecko_core.orchestration.pro.router import resolve_router

    cfg = resolve_router({"LLM_ROUTER": "clawrouter"})
    assert cfg.router == "clawrouter"
    assert cfg.base_url == "http://localhost:8402/v1"
    assert cfg.api_key == "noop"


def test_clawrouter_url_override() -> None:
    from gecko_core.orchestration.pro.router import resolve_router

    cfg = resolve_router({"LLM_ROUTER": "clawrouter", "CLAWROUTER_URL": "http://10.0.0.5:8402/v1"})
    assert cfg.base_url == "http://10.0.0.5:8402/v1"


def test_unknown_router_raises() -> None:
    from gecko_core.orchestration.pro.router import resolve_router

    with pytest.raises(ValueError, match="LLM_ROUTER"):
        resolve_router({"LLM_ROUTER": "vertex"})


def test_llm_config_for_model_includes_headers_for_openrouter() -> None:
    from gecko_core.orchestration.pro.router import resolve_router

    cfg = resolve_router({"LLM_ROUTER": "openrouter", "OPENROUTER_API_KEY": "or-x"})
    llm_config = cfg.llm_config_for_model("openai/gpt-4o-mini")
    entry = llm_config["config_list"][0]
    assert entry["model"] == "openai/gpt-4o-mini"
    assert entry["api_key"] == "or-x"
    assert entry["base_url"] == "https://openrouter.ai/api/v1"
    assert "default_headers" in entry
    assert entry["default_headers"]["X-Title"]


def test_llm_config_for_model_omits_headers_for_openai() -> None:
    from gecko_core.orchestration.pro.router import resolve_router

    cfg = resolve_router({"OPENAI_API_KEY": "sk-x"})
    llm_config = cfg.llm_config_for_model("gpt-4o-mini")
    entry = llm_config["config_list"][0]
    assert "default_headers" not in entry
