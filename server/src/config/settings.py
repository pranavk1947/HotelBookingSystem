"""Process settings, read once from the environment / .env."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings
from pydantic_settings import SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]
SERVER_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env"), env_file_encoding="utf-8", extra="ignore"
    )

    anthropic_api_key: str = ""
    # Overridable so a different account or model tier needs no code change.
    anthropic_model: str = "claude-sonnet-5"

    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"

    # "auto" resolves to whichever key is present; Anthropic wins if both are.
    llm_provider: str = "auto"

    max_tool_rounds: int = 6
    max_tokens: int = 2000
    # Caps token growth on a long negotiation.
    max_history_messages: int = 24

    configs_dir: Path = SERVER_ROOT / "configs"
    client_dir: Path = REPO_ROOT / "client"

    @property
    def has_api_key(self) -> bool:
        """Either provider is enough to run a conversation."""
        return bool(self.anthropic_api_key.strip() or self.openai_api_key.strip())

    @property
    def provider(self) -> str:
        """The provider a turn will actually use."""
        if self.llm_provider != "auto":
            return self.llm_provider
        if self.openai_api_key.strip() and not self.anthropic_api_key.strip():
            return "openai"
        return "anthropic"

    @property
    def active_model(self) -> str:
        return (
            self.openai_model if self.provider == "openai" else self.anthropic_model
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
