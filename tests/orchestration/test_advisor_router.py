"""S8-CONFIG-01 — advisor + basic orchestration honor LLM_ROUTER.

The advisor panel and basic-tier research previously read
``GECKO_LLM_ENDPOINT`` / ``GECKO_LLM_API_KEY`` directly, ignoring the
``LLM_ROUTER`` plane that Pro debate uses. After S8-CONFIG-01 they share
the same resolver as Pro debate.
"""

from __future__ import annotations

from gecko_core.orchestration.settings import (
    OrchestrationSettings,
    resolve_llm_config,
)


def _settings_with(**overrides: object) -> OrchestrationSettings:
    """Build an OrchestrationSettings instance without reading the real env."""
    base: dict[str, object] = {
        "chat_model": "openai/gpt-4o",
        "llm_endpoint": "http://localhost:8402/v1",
        "llm_api_key": "x402",
        "max_input_tokens": 60_000,
        "temperature": 0.3,
    }
    base.update(overrides)
    return OrchestrationSettings.model_construct(**base)


def test_resolve_llm_config_openrouter_via_router_env() -> None:
    """LLM_ROUTER=openrouter resolves to OpenRouter base_url + key, no override needed."""
    env = {
        "LLM_ROUTER": "openrouter",
        "OPENROUTER_API_KEY": "or-test-key",
    }
    cfg = resolve_llm_config(env, settings=_settings_with())

    assert cfg.base_url == "https://openrouter.ai/api/v1"
    assert cfg.api_key == "or-test-key"
    assert cfg.source == "router:openrouter"
    # OpenRouter requires referrer + title for provider attribution.
    assert "HTTP-Referer" in cfg.extra_headers
    assert "X-Title" in cfg.extra_headers


def test_resolve_llm_config_openai_via_router_env() -> None:
    env = {"LLM_ROUTER": "openai", "OPENAI_API_KEY": "sk-openai"}
    cfg = resolve_llm_config(env, settings=_settings_with())

    assert cfg.base_url == "https://api.openai.com/v1"
    assert cfg.api_key == "sk-openai"
    assert cfg.source == "router:openai"


def test_resolve_llm_config_legacy_when_router_unset() -> None:
    """No LLM_ROUTER → legacy GECKO_LLM_ENDPOINT/GECKO_LLM_API_KEY plane."""
    env: dict[str, str] = {}  # nothing set
    settings = _settings_with(
        llm_endpoint="https://my-proxy.example/v1",
        llm_api_key="legacy-key",
    )
    cfg = resolve_llm_config(env, settings=settings)

    assert cfg.base_url == "https://my-proxy.example/v1"
    assert cfg.api_key == "legacy-key"
    assert cfg.source == "legacy"


def test_resolve_llm_config_legacy_overrides_router_with_log(
    caplog: object,
) -> None:
    """When both LLM_ROUTER and GECKO_LLM_ENDPOINT are set, legacy wins for
    one release and we log INFO about the deprecation.
    """
    import logging

    env = {
        "LLM_ROUTER": "openrouter",
        "OPENROUTER_API_KEY": "or-test-key",
        "GECKO_LLM_ENDPOINT": "https://override.example/v1",
    }
    settings = _settings_with(
        llm_endpoint="https://override.example/v1",
        llm_api_key="override-key",
    )

    # caplog is the pytest fixture; mypy can't see its type via plain typing,
    # but the runtime API is `set_level` / `records`.
    caplog.set_level(logging.INFO, logger="gecko_core.orchestration.settings")  # type: ignore[attr-defined]
    cfg = resolve_llm_config(env, settings=settings)

    assert cfg.base_url == "https://override.example/v1"
    assert cfg.api_key == "override-key"
    assert cfg.source == "legacy"
    # Verify the deprecation breadcrumb is emitted.
    assert any(
        "GECKO_LLM_ENDPOINT" in rec.message and "LLM_ROUTER" in rec.message
        for rec in caplog.records  # type: ignore[attr-defined]
    )
