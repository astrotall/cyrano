from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import SecretStr

from cyrano.config import LOGGED_SETTINGS, ConfigError, Settings, load_settings
from tests.conftest import PERSONAL_SETTING_NAMES, PERSONAL_VALUES, REQUIRED_ENV, SECRET_VALUES


def test_loads_from_env(full_env: dict[str, str]) -> None:
    settings = load_settings(env_file=None)

    assert settings.telegram_api_id == 2468013
    assert settings.owner_telegram_id == 987654321
    assert settings.userbot_session_path == Path("/data/session/userbot")
    assert settings.bot_token.get_secret_value() == "123:bot-token-secret"
    assert settings.database_url.get_secret_value().startswith("postgresql+asyncpg://")


def test_optional_defaults(full_env: dict[str, str]) -> None:
    settings = load_settings(env_file=None)

    assert settings.log_level == "INFO"
    assert settings.timezone == "Europe/Moscow"
    assert settings.tzinfo.key == "Europe/Moscow"
    assert settings.media_dir == Path("media")
    assert settings.deepseek_model == "deepseek-chat"


def test_library_log_levels_default_to_warning(full_env: dict[str, str]) -> None:
    settings = load_settings(env_file=None)

    assert settings.library_log_levels == {
        "telethon": "WARNING",
        "aiogram": "WARNING",
        "httpx": "WARNING",
        "httpcore": "WARNING",
    }


def test_library_log_levels_are_configurable(
    full_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TELETHON_LOG_LEVEL", "debug")
    monkeypatch.setenv("AIOGRAM_LOG_LEVEL", "ERROR")
    monkeypatch.setenv("HTTPX_LOG_LEVEL", "INFO")

    levels = load_settings(env_file=None).library_log_levels

    assert levels["telethon"] == "DEBUG"
    assert levels["aiogram"] == "ERROR"
    assert levels["httpx"] == levels["httpcore"] == "INFO"


def test_library_log_levels_do_not_follow_log_level(
    full_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")

    settings = load_settings(env_file=None)

    assert settings.log_level == "DEBUG"
    assert settings.telethon_log_level == "WARNING"


def test_heartbeat_path_default_and_override(
    full_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert load_settings(env_file=None).heartbeat_path.name == "cyrano-heartbeat"

    monkeypatch.setenv("HEARTBEAT_PATH", "/run/cyrano/beat")

    assert load_settings(env_file=None).heartbeat_path == Path("/run/cyrano/beat")


def test_optional_overrides(full_env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "debug")
    monkeypatch.setenv("TIMEZONE", "UTC")
    monkeypatch.setenv("MEDIA_DIR", "/data/media")
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-reasoner")

    settings = load_settings(env_file=None)

    assert settings.log_level == "DEBUG"
    assert settings.timezone == "UTC"
    assert settings.media_dir == Path("/data/media")
    assert settings.deepseek_model == "deepseek-reasoner"


def test_reads_env_file_and_env_wins(
    full_env: dict[str, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("LOG_LEVEL=WARNING\nTIMEZONE=UTC\nPOSTGRES_USER=ignored-extra\n")
    monkeypatch.setenv("LOG_LEVEL", "ERROR")

    settings = load_settings(env_file=env_file)

    assert settings.log_level == "ERROR"
    assert settings.timezone == "UTC"


def test_missing_variables_listed_in_readable_error(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_settings(env_file=None)

    message = str(excinfo.value)
    assert "Missing required environment variables" in message
    for name in REQUIRED_ENV:
        assert f"  - {name}" in message
    assert "Settings section of README.md" in message
    assert ".env.example is the template to copy to .env" in message
    assert "Traceback" not in message
    assert "ValidationError" not in message


def test_only_the_missing_variable_is_reported(
    full_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BOT_TOKEN")

    with pytest.raises(ConfigError) as excinfo:
        load_settings(env_file=None)

    message = str(excinfo.value)
    assert "  - BOT_TOKEN" in message
    assert "  - DEEPSEEK_API_KEY" not in message


def test_empty_value_counts_as_missing(
    full_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BOT_TOKEN", "")

    with pytest.raises(ConfigError, match="BOT_TOKEN"):
        load_settings(env_file=None)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("TELEGRAM_API_ID", "not-a-number"),
        ("LOG_LEVEL", "LOUD"),
        ("TELETHON_LOG_LEVEL", "LOUD"),
        ("AIOGRAM_LOG_LEVEL", "LOUD"),
        ("HTTPX_LOG_LEVEL", "LOUD"),
        ("TIMEZONE", "Mars/Olympus"),
        ("DATABASE_URL", "postgresql://user:pw-secret@host/db"),
        ("REDIS_URL", "http://localhost"),
    ],
)
def test_invalid_values_are_reported(
    full_env: dict[str, str], monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises(ConfigError) as excinfo:
        load_settings(env_file=None)

    message = str(excinfo.value)
    assert "Invalid values" in message
    assert f"  - {name}:" in message
    assert "pw-secret" not in message


def test_secrets_not_exposed_in_repr_str_or_dumps(full_env: dict[str, str]) -> None:
    settings = load_settings(env_file=None)

    rendered = [
        repr(settings),
        str(settings),
        settings.model_dump_json(),
        str(settings.model_dump()),
        str(settings.safe_summary()),
    ]
    for text in rendered:
        for secret in SECRET_VALUES:
            assert secret not in text
    assert isinstance(settings.bot_token, SecretStr)


def test_safe_summary_has_only_the_allowlisted_settings(full_env: dict[str, str]) -> None:
    summary = load_settings(env_file=None).safe_summary()

    assert set(summary) == set(LOGGED_SETTINGS)
    assert summary["log_level"] == "INFO"
    assert summary["media_dir"] == "media"


def test_safe_summary_has_no_secret_and_no_personal_identifier(full_env: dict[str, str]) -> None:
    settings = load_settings(env_file=None)
    summary = settings.safe_summary()

    for name in ("telegram_api_hash", "bot_token", "deepseek_api_key", "database_url", "redis_url"):
        assert name not in summary
    for name in PERSONAL_SETTING_NAMES:
        assert name not in summary
    rendered = json.dumps(summary)
    for value in (*SECRET_VALUES, *PERSONAL_VALUES):
        assert value not in rendered


def test_every_setting_is_either_logged_or_deliberately_withheld() -> None:
    withheld = {
        "telegram_api_id",
        "telegram_api_hash",
        "userbot_session_path",
        "bot_token",
        "owner_telegram_id",
        "deepseek_api_key",
        "database_url",
        "redis_url",
    }

    assert set(Settings.model_fields) == set(LOGGED_SETTINGS) | withheld


def test_settings_are_immutable(full_env: dict[str, str]) -> None:
    settings = Settings(_env_file=None)

    with pytest.raises(ValueError, match="frozen"):
        settings.log_level = "DEBUG"  # type: ignore[misc]
