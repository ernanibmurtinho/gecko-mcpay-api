"""Ingestion-side settings.

Kept separate from `SupabaseSettings` because OpenAI + Tavily are external
service credentials with different rotation/scoping concerns from the DB.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class IngestionSettings(BaseSettings):
    openai_api_key: SecretStr | None = Field(default=None, alias="OPENAI_API_KEY")
    # Optional so gecko-mcp (thin HTTP client) can import gecko-core without
    # TAVILY_API_KEY. Server-side callers (gecko-api, bb CLI) still validate
    # at call time in discovery.py before invoking Tavily.
    tavily_api_key: SecretStr | None = Field(default=None, alias="TAVILY_API_KEY")
    embed_model: str = Field("voyage-3-large", alias="EMBED_MODEL")
    embed_provider: str = Field("voyage", alias="EMBED_PROVIDER")
    voyage_api_key: SecretStr | None = Field(default=None, alias="VOYAGE_API_KEY")
    deepgram_api_key: SecretStr | None = Field(default=None, alias="DEEPGRAM_API_KEY")
    deepgram_max_audio_minutes: int = Field(default=30, alias="DEEPGRAM_MAX_AUDIO_MIN")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )


@lru_cache(maxsize=1)
def get_ingestion_settings() -> IngestionSettings:
    return IngestionSettings()
