"""Loading of environment settings (SPEC §4.1) and feeds.toml (SPEC §4.2)."""

import tomllib
from collections.abc import Mapping
from pathlib import Path

from pydantic import SecretStr

from daily_news.errors import ConfigError
from daily_news.models import DEFAULT_GEMINI_MODEL, FeedsFile, Settings


def _env(environ: Mapping[str, str], name: str) -> str | None:
    """The stripped value, or None when unset or empty."""
    value = environ.get(name, "").strip()
    return value or None


def load_settings(environ: Mapping[str, str]) -> Settings:
    webhook_url = _env(environ, "DISCORD_WEBHOOK_URL")
    if webhook_url is None:
        raise ConfigError("DISCORD_WEBHOOK_URL is not set")
    api_key = _env(environ, "GEMINI_API_KEY")
    return Settings(
        discord_webhook_url=SecretStr(webhook_url),
        gemini_api_key=SecretStr(api_key) if api_key is not None else None,
        gemini_model=_env(environ, "GEMINI_MODEL") or DEFAULT_GEMINI_MODEL,
    )


def require_gemini_key(settings: Settings) -> SecretStr:
    """The API key, which is required unless ``--no-llm`` is given."""
    if settings.gemini_api_key is None:
        raise ConfigError("GEMINI_API_KEY is not set (required unless --no-llm)")
    return settings.gemini_api_key


def load_feeds(path: Path) -> FeedsFile:
    with path.open("rb") as f:
        return FeedsFile.model_validate(tomllib.load(f))
