from __future__ import annotations

import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from cyrano.health import default_heartbeat_path

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

DEFAULT_ENV_FILE = ".env"
DEFAULT_LEOMATCH_BOT_USERNAME = "leomatchbot"
BOT_USERNAME_PATTERN = re.compile(r"[A-Za-z0-9_]{1,32}")
BOT_USERNAME_PREFIX = "@"
DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
HTTP_SCHEMES = frozenset({"http", "https"})
MIN_LLM_TEMPERATURE = 0.0
MAX_LLM_TEMPERATURE = 2.0
DEFAULT_LLM_REPLY_TEMPERATURE = 1.0
DEFAULT_LLM_SUMMARY_TEMPERATURE = 0.2
DEFAULT_LLM_REPLY_MAX_TOKENS = 400
DEFAULT_LLM_SUMMARY_MAX_TOKENS = 1500
DEFAULT_STYLE_EXAMPLES_MAX = 8
MIN_STYLE_EXAMPLES_MAX = 1
MAX_STYLE_EXAMPLES_MAX = 50
DEFAULT_REPLY_HISTORY_MESSAGES = 20
MIN_REPLY_HISTORY_MESSAGES = 1
MAX_REPLY_HISTORY_MESSAGES = 100
DEFAULT_REPLY_PROMPT_BUDGET_CHARS = 24000
MIN_REPLY_PROMPT_BUDGET_CHARS = 4000
MAX_REPLY_PROMPT_BUDGET_CHARS = 200000
DEFAULT_AUTOPILOT_DEBOUNCE_SECONDS = 12
MIN_AUTOPILOT_DEBOUNCE_SECONDS = 0
MAX_AUTOPILOT_DEBOUNCE_SECONDS = 300
DEFAULT_AUTOPILOT_DEBOUNCE_ONGOING_SECONDS = 4
MIN_AUTOPILOT_DEBOUNCE_ONGOING_SECONDS = 0
MAX_AUTOPILOT_DEBOUNCE_ONGOING_SECONDS = 300
DEFAULT_AUTOPILOT_MAX_BURST_WAIT_SECONDS = 30
MIN_AUTOPILOT_MAX_BURST_WAIT_SECONDS = 1
MAX_AUTOPILOT_MAX_BURST_WAIT_SECONDS = 300
DEFAULT_SUMMARY_EVERY_MESSAGES = 12
MIN_SUMMARY_EVERY_MESSAGES = 4
MAX_SUMMARY_EVERY_MESSAGES = 200
MIN_SEND_LIMIT = 1
DEFAULT_SEND_LIMIT_MINUTE = 4
DEFAULT_SEND_LIMIT_HOUR = 40
DEFAULT_SEND_LIMIT_DAY = 200
DEFAULT_SEND_LIMIT_CONTACT_MINUTE = 2
DEFAULT_SEND_LIMIT_CONTACT_HOUR = 15
DEFAULT_SEND_LIMIT_CONTACT_DAY = 60
SEND_LIMIT_ORDER = (
    ("send_limit_minute", "send_limit_hour"),
    ("send_limit_hour", "send_limit_day"),
    ("send_limit_contact_minute", "send_limit_contact_hour"),
    ("send_limit_contact_hour", "send_limit_contact_day"),
    ("send_limit_contact_minute", "send_limit_minute"),
    ("send_limit_contact_hour", "send_limit_hour"),
    ("send_limit_contact_day", "send_limit_day"),
)
AUTOPILOT_TIMING_ORDER = (
    ("autopilot_debounce_ongoing_seconds", "autopilot_debounce_seconds"),
    ("autopilot_debounce_seconds", "autopilot_max_burst_wait_seconds"),
)

LOGGED_SETTINGS = frozenset(
    {
        "log_level",
        "timezone",
        "media_dir",
        "deepseek_model",
        "deepseek_thinking",
        "llm_reply_model",
        "llm_summary_model",
        "llm_reply_temperature",
        "llm_summary_temperature",
        "llm_reply_max_tokens",
        "llm_summary_max_tokens",
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
    deepseek_base_url: str = DEFAULT_DEEPSEEK_BASE_URL
    deepseek_thinking: bool = False
    llm_reply_model: str | None = None
    llm_summary_model: str | None = None
    llm_reply_temperature: float = Field(
        default=DEFAULT_LLM_REPLY_TEMPERATURE, ge=MIN_LLM_TEMPERATURE, le=MAX_LLM_TEMPERATURE
    )
    llm_summary_temperature: float = Field(
        default=DEFAULT_LLM_SUMMARY_TEMPERATURE, ge=MIN_LLM_TEMPERATURE, le=MAX_LLM_TEMPERATURE
    )
    llm_reply_max_tokens: int = Field(default=DEFAULT_LLM_REPLY_MAX_TOKENS, gt=0)
    llm_summary_max_tokens: int = Field(default=DEFAULT_LLM_SUMMARY_MAX_TOKENS, gt=0)
    telethon_log_level: LogLevel = "WARNING"
    aiogram_log_level: LogLevel = "WARNING"
    httpx_log_level: LogLevel = "WARNING"
    heartbeat_path: Path = Field(default_factory=default_heartbeat_path)
    leomatch_bot_username: str = DEFAULT_LEOMATCH_BOT_USERNAME
    style_examples_max: int = Field(
        default=DEFAULT_STYLE_EXAMPLES_MAX, ge=MIN_STYLE_EXAMPLES_MAX, le=MAX_STYLE_EXAMPLES_MAX
    )
    reply_history_messages: int = Field(
        default=DEFAULT_REPLY_HISTORY_MESSAGES,
        ge=MIN_REPLY_HISTORY_MESSAGES,
        le=MAX_REPLY_HISTORY_MESSAGES,
    )
    reply_prompt_budget_chars: int = Field(
        default=DEFAULT_REPLY_PROMPT_BUDGET_CHARS,
        ge=MIN_REPLY_PROMPT_BUDGET_CHARS,
        le=MAX_REPLY_PROMPT_BUDGET_CHARS,
    )
    autopilot_enabled: bool = False
    autopilot_debounce_seconds: int = Field(
        default=DEFAULT_AUTOPILOT_DEBOUNCE_SECONDS,
        ge=MIN_AUTOPILOT_DEBOUNCE_SECONDS,
        le=MAX_AUTOPILOT_DEBOUNCE_SECONDS,
    )
    autopilot_debounce_ongoing_seconds: int = Field(
        default=DEFAULT_AUTOPILOT_DEBOUNCE_ONGOING_SECONDS,
        ge=MIN_AUTOPILOT_DEBOUNCE_ONGOING_SECONDS,
        le=MAX_AUTOPILOT_DEBOUNCE_ONGOING_SECONDS,
    )
    autopilot_max_burst_wait_seconds: int = Field(
        default=DEFAULT_AUTOPILOT_MAX_BURST_WAIT_SECONDS,
        ge=MIN_AUTOPILOT_MAX_BURST_WAIT_SECONDS,
        le=MAX_AUTOPILOT_MAX_BURST_WAIT_SECONDS,
    )
    summary_enabled: bool = True
    summary_every_messages: int = Field(
        default=DEFAULT_SUMMARY_EVERY_MESSAGES,
        ge=MIN_SUMMARY_EVERY_MESSAGES,
        le=MAX_SUMMARY_EVERY_MESSAGES,
    )
    send_limit_minute: int = Field(default=DEFAULT_SEND_LIMIT_MINUTE, ge=MIN_SEND_LIMIT)
    send_limit_hour: int = Field(default=DEFAULT_SEND_LIMIT_HOUR, ge=MIN_SEND_LIMIT)
    send_limit_day: int = Field(default=DEFAULT_SEND_LIMIT_DAY, ge=MIN_SEND_LIMIT)
    send_limit_contact_minute: int = Field(
        default=DEFAULT_SEND_LIMIT_CONTACT_MINUTE, ge=MIN_SEND_LIMIT
    )
    send_limit_contact_hour: int = Field(default=DEFAULT_SEND_LIMIT_CONTACT_HOUR, ge=MIN_SEND_LIMIT)
    send_limit_contact_day: int = Field(default=DEFAULT_SEND_LIMIT_CONTACT_DAY, ge=MIN_SEND_LIMIT)

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

    @field_validator("leomatch_bot_username")
    @classmethod
    def _check_leomatch_bot_username(cls, value: str) -> str:
        name = value.strip().removeprefix(BOT_USERNAME_PREFIX)
        if BOT_USERNAME_PATTERN.fullmatch(name) is None:
            msg = "must be a Telegram username of 1 to 32 letters, digits or underscores"
            raise ValueError(msg)
        return name

    @field_validator("deepseek_model", "llm_reply_model", "llm_summary_model")
    @classmethod
    def _check_model_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        name = value.strip()
        if not name:
            msg = "must not be empty"
            raise ValueError(msg)
        return name

    @field_validator("deepseek_base_url")
    @classmethod
    def _check_deepseek_base_url(cls, value: str) -> str:
        url = value.strip()
        parts = urlsplit(url)
        if parts.scheme not in HTTP_SCHEMES or not parts.netloc:
            msg = "must be an http:// or https:// URL"
            raise ValueError(msg)
        return url

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

    @model_validator(mode="after")
    def _check_setting_order(self) -> Settings:
        broken = [
            f"{lower.upper()} must not be above {upper.upper()}"
            for lower, upper in (*SEND_LIMIT_ORDER, *AUTOPILOT_TIMING_ORDER)
            if getattr(self, lower) > getattr(self, upper)
        ]
        if broken:
            raise ValueError("; ".join(broken))
        return self

    @property
    def reply_model(self) -> str:
        return self.llm_reply_model or self.deepseek_model

    @property
    def summary_model(self) -> str:
        return self.llm_summary_model or self.deepseek_model

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
        lines.extend(
            f"  - {name}: {msg}" if name else f"  - {msg}" for name, msg in sorted(invalid)
        )
    lines.append(
        "The variables are described in the Settings section of README.md; "
        ".env.example is the template to copy to .env."
    )
    return "\n".join(lines)


def load_settings(env_file: str | Path | None = DEFAULT_ENV_FILE) -> Settings:
    try:
        return Settings(_env_file=env_file)
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(exc)) from None


__all__ = ["ConfigError", "LogLevel", "Settings", "load_settings"]
