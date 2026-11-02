"""Application configuration loaded from environment variables."""

from __future__ import annotations

from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from cyrano.health import default_heartbeat_path

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

DEFAULT_ENV_FILE = ".env"

LOGGED_SETTINGS = frozenset(
    {
        "log_level",
        "timezone",
        "media_dir",
        "deepseek_model",
        "telethon_log_level",
        "aiogram_log_level",
        "httpx_log_level",
        "heartbeat_path",
    }
)


class ConfigError(Exception):
    pass


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", env_ignore_empty=True, frozen=True)

    telegram_api_id: int
    telegram_api_hash: SecretStr
    userbot_session_path: Path
    bot_token: SecretStr
    owner_telegram_id: int
    deepseek_api_key: SecretStr
    database_url: SecretStr
    redis_url: SecretStr

    log_level: LogLevel = "INFO"
    timezone: str = "Europe/Moscow"
    media_dir: Path = Path("media")
    deepseek_model: str = "deepseek-chat"
    telethon_log_level: LogLevel = "WARNING"
    aiogram_log_level: LogLevel = "WARNING"
    httpx_log_level: LogLevel = "WARNING"
    heartbeat_path: Path = Field(default_factory=default_heartbeat_path)

    @field_validator(
        "log_level",
        "telethon_log_level",
        "aiogram_log_level",
        "httpx_log_level",
        mode="before",
    )
    @classmethod
    def _normalize_log_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @field_validator("timezone")
    @classmethod
    def _check_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            msg = f"unknown IANA timezone {value!r} (expected e.g. 'Europe/Moscow')"
            raise ValueError(msg) from exc
        return value

    @field_validator("database_url")
    @classmethod
    def _check_database_url(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().startswith("postgresql+asyncpg://"):
            msg = "must be a PostgreSQL URL using the asyncpg driver (postgresql+asyncpg://...)"
            raise ValueError(msg)
        return value

    @field_validator("redis_url")
    @classmethod
    def _check_redis_url(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().startswith(("redis://", "rediss://")):
            msg = "must be a Redis URL (redis://... or rediss://...)"
            raise ValueError(msg)
        return value

    @property
    def library_log_levels(self) -> dict[str, LogLevel]:
        return {
            "telethon": self.telethon_log_level,
            "aiogram": self.aiogram_log_level,
            "httpx": self.httpx_log_level,
            "httpcore": self.httpx_log_level,
        }

    @property
    def tzinfo(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def safe_summary(self) -> dict[str, str]:
        dumped = self.model_dump(include=set(LOGGED_SETTINGS))
        return {name: str(value) for name, value in dumped.items()}


def _format_validation_error(exc: ValidationError) -> str:
    missing: list[str] = []
    invalid: list[tuple[str, str]] = []
    for err in exc.errors(include_input=False):
        name = ".".join(str(part) for part in err["loc"]).upper()
        if err["type"] == "missing":
            missing.append(name)
        else:
            invalid.append((name, err["msg"]))

    lines = ["Invalid configuration."]
    if missing:
        lines.append("Missing required environment variables:")
        lines.extend(f"  - {name}" for name in sorted(missing))
    if invalid:
        lines.append("Invalid values:")
        lines.extend(f"  - {name}: {msg}" for name, msg in sorted(invalid))
    lines.append("See .env.example for the full list of variables.")
    return "\n".join(lines)


def load_settings(env_file: str | Path | None = DEFAULT_ENV_FILE) -> Settings:
    try:
        return Settings(_env_file=env_file)
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(exc)) from None


__all__ = ["ConfigError", "LogLevel", "Settings", "load_settings"]
