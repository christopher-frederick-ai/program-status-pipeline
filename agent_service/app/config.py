"""Service configuration, read from environment variables (or a .env file in dev)."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Anthropic
    anthropic_api_key: str = ""
    # Current Sonnet. (claude-sonnet-4-5 is listed as retiring no sooner than 2026-09-29.)
    anthropic_model: str = "claude-sonnet-5"
    anthropic_timeout_seconds: float = 60.0
    default_max_tokens: int = 2048

    # Service auth: n8n must send this value in the X-Service-Token header.
    service_shared_secret: str = ""

    # Where agent instruction files live.
    agents_dir: Path = Field(default=Path(__file__).resolve().parent.parent / "agents")

    # Guardrail on request size (characters of serialized input).
    max_input_chars: int = 100_000

    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
