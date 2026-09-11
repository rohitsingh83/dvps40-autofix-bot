"""
config.py — Centralised, validated settings via pydantic-settings.

All environment variables are read from the OS environment or a .env file
(loaded automatically by pydantic-settings).  Every field is annotated with
a description so that developers can run
    python -c "from config import settings; print(settings.model_json_schema())"
to get a self-documenting schema.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Optional

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application-wide configuration loaded from environment variables / .env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",  # silently discard unknown env vars
    )

    # ------------------------------------------------------------------ #
    # Telegram
    # ------------------------------------------------------------------ #
    telegram_bot_token: SecretStr = Field(
        ...,
        description="HTTP API token from @BotFather.",
    )
    telegram_chat_id: str = Field(
        ...,
        description=(
            "Destination chat/group/channel ID where incident reports are sent. "
            "Can be a numeric ID (e.g. -1001234567890) or a @username."
        ),
    )

    # ------------------------------------------------------------------ #
    # GitHub
    # ------------------------------------------------------------------ #
    github_token: SecretStr = Field(
        ...,
        description="Personal Access Token (PAT) or GitHub App installation token.",
    )
    github_repo: str = Field(
        ...,
        description='Full repository slug in "owner/repo" format.',
        pattern=r"^[^/]+/[^/]+$",
    )
    dev_branch: str = Field(
        default="dev",
        description="The protected development branch. PRs are always opened against this branch.",
    )

    # ------------------------------------------------------------------ #
    # OpenAI / LLM
    # ------------------------------------------------------------------ #
    openai_api_key: SecretStr = Field(
        ...,
        description="OpenAI API key used by LangChain / LiteLLM.",
    )
    openai_model: str = Field(
        default="gpt-4o",
        description="Model identifier forwarded to the chat-completion endpoint.",
    )
    openai_temperature: float = Field(
        default=0.2,
        ge=0.0,
        le=2.0,
        description="Sampling temperature for the LLM (lower = more deterministic).",
    )
    openai_max_tokens: int = Field(
        default=4096,
        ge=256,
        description="Maximum tokens for the LLM completion.",
    )

    # ------------------------------------------------------------------ #
    # Webhook security
    # ------------------------------------------------------------------ #
    webhook_secret: SecretStr = Field(
        ...,
        description=(
            "Shared HMAC-SHA256 secret used to verify Vercel and Railway payloads. "
            "Register the same value in each platform's webhook settings."
        ),
    )

    # ------------------------------------------------------------------ #
    # Application / server
    # ------------------------------------------------------------------ #
    app_host: str = Field(default="0.0.0.0", description="Bind address for uvicorn.")
    app_port: int = Field(default=8000, ge=1, le=65535, description="Bind port.")
    log_level: str = Field(default="INFO", description="Python logging level.")

    # ------------------------------------------------------------------ #
    # Optional feature flags
    # ------------------------------------------------------------------ #
    dry_run: bool = Field(
        default=False,
        description=(
            "When True, the agent analyses the error and formats the message but "
            "skips creating the GitHub branch / PR and sending the Telegram message. "
            "Useful for local testing."
        ),
    )

    # ------------------------------------------------------------------ #
    # Validators
    # ------------------------------------------------------------------ #
    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, v: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = v.upper()
        if upper not in allowed:
            raise ValueError(f"log_level must be one of {allowed}, got {v!r}")
        return upper

    @field_validator("dev_branch")
    @classmethod
    def _reject_protected_branches(cls, v: str) -> str:
        forbidden = {"main", "master", "production", "prod"}
        if v.lower() in forbidden:
            raise ValueError(
                f"dev_branch cannot be set to a protected branch name ({v!r}). "
                "Set DEV_BRANCH to your actual development branch (e.g. 'dev')."
            )
        return v


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached singleton Settings instance."""
    return Settings()


# Convenience alias used throughout the codebase:  from config import settings
settings: Settings = get_settings()
