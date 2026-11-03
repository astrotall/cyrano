from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.privacy import FAKE_API_ID, FAKE_OWNER_TELEGRAM_ID

REQUIRED_ENV = {
    "TELEGRAM_API_ID": str(FAKE_API_ID),
    "TELEGRAM_API_HASH": "hash-secret-value",
    "USERBOT_SESSION_PATH": "/data/session/userbot",
    "BOT_TOKEN": "123:bot-token-secret",
    "OWNER_TELEGRAM_ID": str(FAKE_OWNER_TELEGRAM_ID),
    "DEEPSEEK_API_KEY": "sk-deepseek-secret",
    "DATABASE_URL": "postgresql+asyncpg://cyrano:db-password-secret@postgres:5432/cyrano",
    "REDIS_URL": "redis://:redis-password-secret@redis:6379/0",
}
OPTIONAL_ENV = (
    "LOG_LEVEL",
    "TIMEZONE",
    "MEDIA_DIR",
    "DEEPSEEK_MODEL",
    "DEEPSEEK_BASE_URL",
    "DEEPSEEK_THINKING",
    "LLM_REPLY_MODEL",
    "LLM_SUMMARY_MODEL",
    "LLM_REPLY_TEMPERATURE",
    "LLM_SUMMARY_TEMPERATURE",
    "LLM_REPLY_MAX_TOKENS",
    "LLM_SUMMARY_MAX_TOKENS",
    "TELETHON_LOG_LEVEL",
    "AIOGRAM_LOG_LEVEL",
    "HTTPX_LOG_LEVEL",
    "HEARTBEAT_PATH",
    "LEOMATCH_BOT_USERNAME",
    "STYLE_EXAMPLES_MAX",
    "REPLY_HISTORY_MESSAGES",
    "REPLY_PROMPT_BUDGET_CHARS",
    "AUTOPILOT_ENABLED",
)
TOUCHED_LOGGERS = ("telethon", "aiogram", "httpx", "httpcore", "arq", "arq.worker", "arq.jobs")
SECRET_VALUES = (
    "hash-secret-value",
    "bot-token-secret",
    "sk-deepseek-secret",
    "db-password-secret",
    "redis-password-secret",
)
PERSONAL_VALUES = (str(FAKE_API_ID), str(FAKE_OWNER_TELEGRAM_ID))
PERSONAL_SETTING_NAMES = ("telegram_api_id", "owner_telegram_id")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (*REQUIRED_ENV, *OPTIONAL_ENV):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def full_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    for name, value in REQUIRED_ENV.items():
        monkeypatch.setenv(name, value)
    return dict(REQUIRED_ENV)


@pytest.fixture
def heartbeat_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "heartbeat"
    monkeypatch.setenv("HEARTBEAT_PATH", str(path))
    return path


@pytest.fixture(autouse=True)
def restore_loggers() -> Iterator[None]:
    loggers = [logging.getLogger(), *(logging.getLogger(n) for n in TOUCHED_LOGGERS)]
    saved = [(lg, list(lg.handlers), lg.level, lg.propagate) for lg in loggers]
    yield
    for lg, handlers, level, propagate in saved:
        lg.handlers[:] = handlers
        lg.setLevel(level)
        lg.propagate = propagate
