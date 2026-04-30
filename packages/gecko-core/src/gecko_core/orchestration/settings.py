"""Orchestration settings.

Kept separate from `IngestionSettings` so generation knobs (model, prompt
caps) rotate independently from data-pipeline credentials.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class OrchestrationSettings(BaseSettings):
    """LLM-side knobs for the basic + (later) pro orchestration paths."""

    # ClawRouter is the v3 default. Pin a JSON-mode-safe model for orchestration
    # steps that depend on response_format=json_object — `blockrun/auto` may
    # route to a model that breaks JSON mode. Override per-deploy via env.
    chat_model: str = Field("openai/gpt-4o", alias="CHAT_MODEL")

    # OpenAI-compatible base_url. Default points at the local ClawRouter proxy
    # (`npx @blockrun/clawrouter`) on port 8402. Set to OpenAI's URL for the v2
    # fallback path, or to any OpenAI-compatible gateway.
    llm_endpoint: str = Field("http://localhost:8402/v1", alias="GECKO_LLM_ENDPOINT")

    # Auth header value sent to the LLM endpoint. ClawRouter accepts the literal
    # "x402" because payments are wallet-signed. For OpenAI fallback, set to
    # the real API key.
    llm_api_key: str = Field("x402", alias="GECKO_LLM_API_KEY")

    # Defensive cap on prompt input tokens. Truncation happens in
    # orchestration.basic before the OpenAI call. 60k leaves comfortable
    # headroom under the 128k context window for response + system prompt.
    max_input_tokens: int = Field(60_000, alias="ORCH_MAX_INPUT_TOKENS")

    # Sampling. Low for grounded, deterministic-ish output.
    temperature: float = Field(0.3, alias="ORCH_TEMPERATURE")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )


@lru_cache(maxsize=1)
def get_orchestration_settings() -> OrchestrationSettings:
    return OrchestrationSettings()  # type: ignore[call-arg]


@dataclass(frozen=True)
class LLMClientConfig:
    """Resolved LLM client surface used by single-call orchestration paths
    (advisor panel, basic-tier research, scaffold, flywheel).

    Unifies the two historical config planes:
      - ``LLM_ROUTER`` (Pro debate, S4) — selects router by name and pulls
        base_url/api_key from per-router settings.
      - ``GECKO_LLM_ENDPOINT`` / ``GECKO_LLM_API_KEY`` (advisor, S3) — direct
        OpenAI-compatible base_url + auth header.

    Resolution order (S8-CONFIG-01):
      1. If ``LLM_ROUTER`` is set in env, route via ``resolve_router`` (the
         same path Pro debate uses). Default model comes from the router's
         per-tier preset; callers that need a specific model override
         ``OrchestrationSettings.chat_model``.
      2. Otherwise fall back to ``OrchestrationSettings`` (legacy
         ``GECKO_LLM_ENDPOINT``/``GECKO_LLM_API_KEY``).

    ``GECKO_LLM_ENDPOINT`` remains a backward-compat override for one
    release: when ``LLM_ROUTER`` is set AND the operator has also set
    ``GECKO_LLM_ENDPOINT`` explicitly (i.e. a non-default value), we log at
    INFO that the legacy override is still in effect. The override wins so
    nothing breaks; the log is a deprecation breadcrumb.
    """

    base_url: str
    api_key: str
    extra_headers: dict[str, str]
    chat_model: str
    temperature: float
    max_input_tokens: int
    source: str  # "router:<name>" or "legacy"


def resolve_llm_config(
    environ: dict[str, str] | None = None,
    *,
    settings: OrchestrationSettings | None = None,
) -> LLMClientConfig:
    """Single LLM-resolution path used by every non-Pro-debate orchestrator.

    See ``LLMClientConfig`` for the precedence rules. Pure: never reads the
    environment again past the optional ``environ`` parameter, and never
    raises on missing legacy env (legacy defaults remain "x402" / clawrouter
    so unit tests that don't set anything still construct a client).
    """
    env = environ if environ is not None else dict(os.environ)
    s = settings or get_orchestration_settings()

    # Did the operator opt into the unified router plane?
    router_env = (env.get("LLM_ROUTER") or "").strip().lower()
    legacy_endpoint_set = "GECKO_LLM_ENDPOINT" in env and env["GECKO_LLM_ENDPOINT"].strip() != ""

    if router_env:
        # Lazy import — `pro.router` pulls in routing.catalog which is a
        # heavier graph than orchestration.settings should require at import
        # time.
        from gecko_core.orchestration.pro.router import resolve_router

        rc = resolve_router(env)

        if legacy_endpoint_set:
            # Backward-compat override path. Legacy env wins for a release;
            # surface this so operators know to drop it.
            logger.info(
                "GECKO_LLM_ENDPOINT is set alongside LLM_ROUTER=%s; "
                "legacy override wins for this release. "
                "Drop GECKO_LLM_ENDPOINT/GECKO_LLM_API_KEY and rely on "
                "LLM_ROUTER going forward.",
                router_env,
            )
            return LLMClientConfig(
                base_url=s.llm_endpoint,
                api_key=s.llm_api_key,
                extra_headers={},
                chat_model=s.chat_model,
                temperature=s.temperature,
                max_input_tokens=s.max_input_tokens,
                source="legacy",
            )

        return LLMClientConfig(
            base_url=rc.base_url,
            api_key=rc.api_key,
            extra_headers=dict(rc.extra_headers),
            chat_model=s.chat_model,
            temperature=s.temperature,
            max_input_tokens=s.max_input_tokens,
            source=f"router:{rc.router}",
        )

    # No LLM_ROUTER → legacy plane. Unchanged from pre-S8 behavior.
    return LLMClientConfig(
        base_url=s.llm_endpoint,
        api_key=s.llm_api_key,
        extra_headers={},
        chat_model=s.chat_model,
        temperature=s.temperature,
        max_input_tokens=s.max_input_tokens,
        source="legacy",
    )


__all__ = [
    "LLMClientConfig",
    "OrchestrationSettings",
    "get_orchestration_settings",
    "resolve_llm_config",
]
