from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import SecretStr

from cyrano.config import LOGGED_SETTINGS, ConfigError, Settings, load_settings
from tests.conftest import PERSONAL_SETTING_NAMES, PERSONAL_VALUES, REQUIRED_ENV, SECRET_VALUES
from tests.privacy import FAKE_API_ID, FAKE_OWNER_TELEGRAM_ID, assert_absent


def test_loads_from_env(full_env: dict[str, str]) -> None:
    settings = load_settings(env_file=None)

    assert settings.telegram_api_id == FAKE_API_ID
    assert settings.owner_telegram_id == FAKE_OWNER_TELEGRAM_ID
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
        assert_absent(value, rendered)


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
        "leomatch_bot_username",
        "deepseek_base_url",
        "style_examples_max",
        "reply_history_messages",
        "reply_prompt_budget_chars",
        "autopilot_enabled",
        "early_read_enabled",
        "autopilot_debounce_seconds",
        "autopilot_debounce_ongoing_seconds",
        "autopilot_max_burst_wait_seconds",
        "summary_enabled",
        "summary_every_messages",
        "send_limit_minute",
        "send_limit_hour",
        "send_limit_day",
        "send_limit_contact_minute",
        "send_limit_contact_hour",
        "send_limit_contact_day",
    }

    assert set(Settings.model_fields) == set(LOGGED_SETTINGS) | withheld


def test_settings_are_immutable(full_env: dict[str, str]) -> None:
    settings = Settings(_env_file=None)

    with pytest.raises(ValueError, match="frozen"):
        settings.log_level = "DEBUG"  # type: ignore[misc]


def test_the_leomatch_bot_username_defaults_to_leomatchbot(full_env: dict[str, str]) -> None:
    assert load_settings(env_file=None).leomatch_bot_username == "leomatchbot"


@pytest.mark.parametrize(
    ("raw", "expected"), [("MyMatchBot", "MyMatchBot"), (" @other_bot ", "other_bot")]
)
def test_the_leomatch_bot_username_can_be_overridden(
    full_env: dict[str, str], monkeypatch: pytest.MonkeyPatch, raw: str, expected: str
) -> None:
    monkeypatch.setenv("LEOMATCH_BOT_USERNAME", raw)

    assert load_settings(env_file=None).leomatch_bot_username == expected


def test_an_empty_leomatch_bot_username_counts_as_unset(
    full_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEOMATCH_BOT_USERNAME", "")

    assert load_settings(env_file=None).leomatch_bot_username == "leomatchbot"


@pytest.mark.parametrize("raw", ["has space", "bad-name!", "@", "x" * 33, "t.me/leomatchbot"])
def test_an_invalid_leomatch_bot_username_is_rejected_without_echoing_it(
    full_env: dict[str, str], monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("LEOMATCH_BOT_USERNAME", raw)

    with pytest.raises(ConfigError) as raised:
        load_settings(env_file=None)

    assert "LEOMATCH_BOT_USERNAME" in str(raised.value)
    assert raw not in str(raised.value)


def test_style_examples_max_defaults_to_eight(full_env: dict[str, str]) -> None:
    assert load_settings(env_file=None).style_examples_max == 8


@pytest.mark.parametrize("value", ["1", "8", "50"])
def test_style_examples_max_can_be_overridden(
    full_env: dict[str, str], monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("STYLE_EXAMPLES_MAX", value)

    assert load_settings(env_file=None).style_examples_max == int(value)


def test_an_empty_style_examples_max_counts_as_unset(
    full_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STYLE_EXAMPLES_MAX", "")

    assert load_settings(env_file=None).style_examples_max == 8


@pytest.mark.parametrize("value", ["0", "-1", "51", "many", "2.5"])
def test_an_invalid_style_examples_max_is_a_readable_error_without_the_value(
    full_env: dict[str, str], monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("STYLE_EXAMPLES_MAX", value)

    with pytest.raises(ConfigError) as caught:
        load_settings(env_file=None)

    message = str(caught.value)
    assert "STYLE_EXAMPLES_MAX" in message
    assert f": {value}" not in message
    assert "'" + value + "'" not in message


def test_the_reply_settings_default_to_twenty_messages_and_24000_characters(
    full_env: dict[str, str],
) -> None:
    settings = load_settings(env_file=None)

    assert settings.reply_history_messages == 20
    assert settings.reply_prompt_budget_chars == 24000


@pytest.mark.parametrize(
    ("name", "value", "field"),
    [
        ("REPLY_HISTORY_MESSAGES", "1", "reply_history_messages"),
        ("REPLY_HISTORY_MESSAGES", "100", "reply_history_messages"),
        ("REPLY_PROMPT_BUDGET_CHARS", "4000", "reply_prompt_budget_chars"),
        ("REPLY_PROMPT_BUDGET_CHARS", "200000", "reply_prompt_budget_chars"),
    ],
)
def test_the_reply_settings_accept_their_bounds(
    full_env: dict[str, str], monkeypatch: pytest.MonkeyPatch, name: str, value: str, field: str
) -> None:
    monkeypatch.setenv(name, value)

    assert getattr(load_settings(env_file=None), field) == int(value)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("REPLY_HISTORY_MESSAGES", "0"),
        ("REPLY_HISTORY_MESSAGES", "101"),
        ("REPLY_HISTORY_MESSAGES", "-3"),
        ("REPLY_HISTORY_MESSAGES", "twenty"),
        ("REPLY_HISTORY_MESSAGES", "2.5"),
        ("REPLY_PROMPT_BUDGET_CHARS", "3999"),
        ("REPLY_PROMPT_BUDGET_CHARS", "200001"),
        ("REPLY_PROMPT_BUDGET_CHARS", "big"),
    ],
)
def test_an_invalid_reply_setting_is_a_readable_error_without_the_value(
    full_env: dict[str, str], monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises(ConfigError) as caught:
        load_settings(env_file=None)

    message = str(caught.value)
    assert name in message
    assert f": {value}" not in message
    assert "'" + value + "'" not in message


def test_empty_reply_settings_count_as_unset(
    full_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REPLY_HISTORY_MESSAGES", "")
    monkeypatch.setenv("REPLY_PROMPT_BUDGET_CHARS", "")

    settings = load_settings(env_file=None)

    assert (settings.reply_history_messages, settings.reply_prompt_budget_chars) == (20, 24000)


def test_autopilot_is_off_unless_it_is_switched_on(full_env: dict[str, str]) -> None:
    assert load_settings(env_file=None).autopilot_enabled is False


@pytest.mark.parametrize(
    ("value", "expected"), [("true", True), ("1", True), ("false", False), ("0", False)]
)
def test_autopilot_reads_booleans(
    full_env: dict[str, str], monkeypatch: pytest.MonkeyPatch, value: str, expected: bool
) -> None:
    monkeypatch.setenv("AUTOPILOT_ENABLED", value)

    assert load_settings(env_file=None).autopilot_enabled is expected


def test_a_nonsense_autopilot_value_is_a_readable_error_without_the_value(
    full_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTOPILOT_ENABLED", "nonsense-marker-zq")

    with pytest.raises(ConfigError) as raised:
        load_settings(env_file=None)

    assert "AUTOPILOT_ENABLED" in str(raised.value)
    assert "nonsense-marker-zq" not in str(raised.value)


def test_the_debounce_defaults_to_twelve_seconds(full_env: dict[str, str]) -> None:
    assert load_settings(env_file=None).autopilot_debounce_seconds == 12


@pytest.mark.parametrize("value", ["0", "12", "300"])
def test_the_debounce_accepts_its_range(
    full_env: dict[str, str], monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("AUTOPILOT_DEBOUNCE_SECONDS", value)
    monkeypatch.setenv("AUTOPILOT_DEBOUNCE_ONGOING_SECONDS", value)
    monkeypatch.setenv("AUTOPILOT_MAX_BURST_WAIT_SECONDS", "300")

    assert load_settings(env_file=None).autopilot_debounce_seconds == int(value)


@pytest.mark.parametrize("value", ["-1", "301", "soon-marker-zq"])
def test_a_debounce_outside_its_range_is_a_readable_error_without_the_value(
    full_env: dict[str, str], monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("AUTOPILOT_DEBOUNCE_SECONDS", value)

    with pytest.raises(ConfigError) as raised:
        load_settings(env_file=None)

    assert "AUTOPILOT_DEBOUNCE_SECONDS" in str(raised.value)
    assert value not in str(raised.value)


def test_an_empty_debounce_falls_back_to_the_default(
    full_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTOPILOT_DEBOUNCE_SECONDS", "")

    assert load_settings(env_file=None).autopilot_debounce_seconds == 12


ONGOING_DEBOUNCE_VARIABLE = "AUTOPILOT_DEBOUNCE_ONGOING_SECONDS"
MAX_BURST_WAIT_VARIABLE = "AUTOPILOT_MAX_BURST_WAIT_SECONDS"
DEBOUNCE_VARIABLE = "AUTOPILOT_DEBOUNCE_SECONDS"


def test_the_ongoing_debounce_and_the_maximum_wait_have_their_defaults_and_stay_unlogged(
    full_env: dict[str, str],
) -> None:
    settings = load_settings(env_file=None)

    assert settings.autopilot_debounce_ongoing_seconds == 4
    assert settings.autopilot_max_burst_wait_seconds == 30
    assert "autopilot_debounce_ongoing_seconds" not in settings.safe_summary()
    assert "autopilot_max_burst_wait_seconds" not in settings.safe_summary()


@pytest.mark.parametrize(
    ("name", "value", "debounce", "attribute"),
    [
        (ONGOING_DEBOUNCE_VARIABLE, "0", "12", "autopilot_debounce_ongoing_seconds"),
        (ONGOING_DEBOUNCE_VARIABLE, "300", "300", "autopilot_debounce_ongoing_seconds"),
        (MAX_BURST_WAIT_VARIABLE, "1", "1", "autopilot_max_burst_wait_seconds"),
        (MAX_BURST_WAIT_VARIABLE, "300", "12", "autopilot_max_burst_wait_seconds"),
    ],
)
def test_the_ongoing_debounce_and_the_maximum_wait_accept_their_bounds(
    full_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
    debounce: str,
    attribute: str,
) -> None:
    monkeypatch.setenv(DEBOUNCE_VARIABLE, debounce)
    monkeypatch.setenv(ONGOING_DEBOUNCE_VARIABLE, "0")
    monkeypatch.setenv(MAX_BURST_WAIT_VARIABLE, "300")
    monkeypatch.setenv(name, value)

    assert getattr(load_settings(env_file=None), attribute) == int(value)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        (ONGOING_DEBOUNCE_VARIABLE, "-1"),
        (ONGOING_DEBOUNCE_VARIABLE, "301"),
        (ONGOING_DEBOUNCE_VARIABLE, "quick-marker-zq"),
        (MAX_BURST_WAIT_VARIABLE, "0"),
        (MAX_BURST_WAIT_VARIABLE, "301"),
        (MAX_BURST_WAIT_VARIABLE, "2.5"),
        (MAX_BURST_WAIT_VARIABLE, "long-marker-zq"),
    ],
)
def test_an_ongoing_debounce_or_a_maximum_wait_out_of_range_is_readable_without_the_value(
    full_env: dict[str, str], monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises(ConfigError) as raised:
        load_settings(env_file=None)

    assert name in str(raised.value)
    assert value not in str(raised.value)


@pytest.mark.parametrize(
    ("debounce", "ongoing", "max_wait", "rule"),
    [
        ("21", "22", "300", f"{ONGOING_DEBOUNCE_VARIABLE} must not be above {DEBOUNCE_VARIABLE}"),
        ("31", "4", "29", f"{DEBOUNCE_VARIABLE} must not be above {MAX_BURST_WAIT_VARIABLE}"),
    ],
)
def test_the_debounce_ordering_rules_name_the_variables_and_never_the_values(
    full_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    debounce: str,
    ongoing: str,
    max_wait: str,
    rule: str,
) -> None:
    monkeypatch.setenv(DEBOUNCE_VARIABLE, debounce)
    monkeypatch.setenv(ONGOING_DEBOUNCE_VARIABLE, ongoing)
    monkeypatch.setenv(MAX_BURST_WAIT_VARIABLE, max_wait)

    with pytest.raises(ConfigError) as raised:
        load_settings(env_file=None)

    message = str(raised.value)
    assert rule in message
    for value in (debounce, ongoing, max_wait):
        assert value not in message


@pytest.mark.parametrize(("debounce", "ongoing", "max_wait"), [("12", "12", "12"), ("0", "0", "1")])
def test_equal_debounces_and_a_maximum_wait_equal_to_the_debounce_are_accepted(
    full_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    debounce: str,
    ongoing: str,
    max_wait: str,
) -> None:
    monkeypatch.setenv(DEBOUNCE_VARIABLE, debounce)
    monkeypatch.setenv(ONGOING_DEBOUNCE_VARIABLE, ongoing)
    monkeypatch.setenv(MAX_BURST_WAIT_VARIABLE, max_wait)

    settings = load_settings(env_file=None)

    assert (
        settings.autopilot_debounce_seconds,
        settings.autopilot_debounce_ongoing_seconds,
        settings.autopilot_max_burst_wait_seconds,
    ) == (int(debounce), int(ongoing), int(max_wait))


def test_a_normal_debounce_above_the_default_maximum_wait_needs_the_maximum_wait_raised(
    full_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(DEBOUNCE_VARIABLE, "45")

    with pytest.raises(ConfigError) as raised:
        load_settings(env_file=None)

    assert f"{DEBOUNCE_VARIABLE} must not be above {MAX_BURST_WAIT_VARIABLE}" in str(raised.value)


SEND_LIMIT_VARIABLES = (
    "SEND_LIMIT_MINUTE",
    "SEND_LIMIT_HOUR",
    "SEND_LIMIT_DAY",
    "SEND_LIMIT_CONTACT_MINUTE",
    "SEND_LIMIT_CONTACT_HOUR",
    "SEND_LIMIT_CONTACT_DAY",
)


def test_the_send_limits_have_conservative_defaults_and_stay_out_of_the_log_line(
    full_env: dict[str, str],
) -> None:
    settings = load_settings(env_file=None)

    assert (
        settings.send_limit_minute,
        settings.send_limit_hour,
        settings.send_limit_day,
        settings.send_limit_contact_minute,
        settings.send_limit_contact_hour,
        settings.send_limit_contact_day,
    ) == (4, 40, 200, 2, 15, 60)
    assert not any(name.startswith("send_limit") for name in settings.safe_summary())


@pytest.mark.parametrize("name", SEND_LIMIT_VARIABLES)
@pytest.mark.parametrize("value", ["0", "-7", "many-marker-zq", "2.5"])
def test_a_send_limit_below_one_or_not_a_number_is_a_readable_error_without_the_value(
    full_env: dict[str, str], monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises(ConfigError) as caught:
        load_settings(env_file=None)

    message = str(caught.value)
    assert name in message
    assert value not in message


@pytest.mark.parametrize("name", SEND_LIMIT_VARIABLES)
def test_an_empty_send_limit_counts_as_unset(
    full_env: dict[str, str], monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.setenv(name, "")

    assert load_settings(env_file=None).send_limit_day == 200


@pytest.mark.parametrize(
    ("lower", "upper", "lower_value", "upper_value"),
    [
        ("SEND_LIMIT_MINUTE", "SEND_LIMIT_HOUR", 77001, 77000),
        ("SEND_LIMIT_HOUR", "SEND_LIMIT_DAY", 77001, 77000),
        ("SEND_LIMIT_CONTACT_MINUTE", "SEND_LIMIT_CONTACT_HOUR", 77001, 77000),
        ("SEND_LIMIT_CONTACT_HOUR", "SEND_LIMIT_CONTACT_DAY", 77001, 77000),
        ("SEND_LIMIT_CONTACT_MINUTE", "SEND_LIMIT_MINUTE", 77001, 77000),
        ("SEND_LIMIT_CONTACT_HOUR", "SEND_LIMIT_HOUR", 77001, 77000),
        ("SEND_LIMIT_CONTACT_DAY", "SEND_LIMIT_DAY", 77001, 77000),
    ],
)
def test_every_ordering_rule_names_the_two_variables_and_never_the_values(
    full_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    lower: str,
    upper: str,
    lower_value: int,
    upper_value: int,
) -> None:
    for name in SEND_LIMIT_VARIABLES:
        monkeypatch.setenv(name, "90000")
    monkeypatch.setenv(lower, str(lower_value))
    monkeypatch.setenv(upper, str(upper_value))

    with pytest.raises(ConfigError) as caught:
        load_settings(env_file=None)

    message = str(caught.value)
    assert f"{lower} must not be above {upper}" in message
    assert str(lower_value) not in message
    assert str(upper_value) not in message
    assert "90000" not in message


def test_all_broken_ordering_rules_are_listed_at_once(
    full_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SEND_LIMIT_MINUTE", "500")
    monkeypatch.setenv("SEND_LIMIT_CONTACT_DAY", "500")

    with pytest.raises(ConfigError) as caught:
        load_settings(env_file=None)

    message = str(caught.value)
    assert "SEND_LIMIT_MINUTE must not be above SEND_LIMIT_HOUR" in message
    assert "SEND_LIMIT_CONTACT_DAY must not be above SEND_LIMIT_DAY" in message
    assert "500" not in message


@pytest.mark.parametrize(
    "values",
    [
        {
            "SEND_LIMIT_MINUTE": "7",
            "SEND_LIMIT_HOUR": "7",
            "SEND_LIMIT_DAY": "7",
            "SEND_LIMIT_CONTACT_MINUTE": "7",
            "SEND_LIMIT_CONTACT_HOUR": "7",
            "SEND_LIMIT_CONTACT_DAY": "7",
        },
        {
            "SEND_LIMIT_MINUTE": "1",
            "SEND_LIMIT_HOUR": "1",
            "SEND_LIMIT_DAY": "1",
            "SEND_LIMIT_CONTACT_MINUTE": "1",
            "SEND_LIMIT_CONTACT_HOUR": "1",
            "SEND_LIMIT_CONTACT_DAY": "1",
        },
    ],
)
def test_equal_values_at_every_boundary_are_accepted(
    full_env: dict[str, str], monkeypatch: pytest.MonkeyPatch, values: dict[str, str]
) -> None:
    for name, value in values.items():
        monkeypatch.setenv(name, value)

    settings = load_settings(env_file=None)

    assert settings.send_limit_contact_day == int(values["SEND_LIMIT_CONTACT_DAY"])


SUMMARY_ENABLED_VARIABLE = "SUMMARY_ENABLED"
SUMMARY_EVERY_VARIABLE = "SUMMARY_EVERY_MESSAGES"


def test_the_summary_settings_have_their_defaults_and_stay_unlogged(
    full_env: dict[str, str],
) -> None:
    settings = load_settings(env_file=None)

    assert settings.summary_enabled is True
    assert settings.summary_every_messages == 12
    assert "summary_enabled" not in settings.safe_summary()
    assert "summary_every_messages" not in settings.safe_summary()


@pytest.mark.parametrize(("value", "expected"), [("false", False), ("0", False), ("true", True)])
def test_the_summary_switch_reads_a_boolean(
    full_env: dict[str, str], monkeypatch: pytest.MonkeyPatch, value: str, expected: bool
) -> None:
    monkeypatch.setenv(SUMMARY_ENABLED_VARIABLE, value)

    assert load_settings(env_file=None).summary_enabled is expected


@pytest.mark.parametrize("value", ["4", "200"])
def test_the_summary_threshold_accepts_its_bounds(
    full_env: dict[str, str], monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv(SUMMARY_EVERY_VARIABLE, value)

    assert load_settings(env_file=None).summary_every_messages == int(value)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        (SUMMARY_EVERY_VARIABLE, "3"),
        (SUMMARY_EVERY_VARIABLE, "201"),
        (SUMMARY_EVERY_VARIABLE, "12.5"),
        (SUMMARY_EVERY_VARIABLE, "every-marker-zq"),
        (SUMMARY_ENABLED_VARIABLE, "switch-marker-zq"),
    ],
)
def test_a_summary_setting_out_of_range_is_readable_without_the_value(
    full_env: dict[str, str], monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises(ConfigError) as raised:
        load_settings(env_file=None)

    assert name in str(raised.value)
    assert value not in str(raised.value)


EARLY_READ_VARIABLE = "EARLY_READ_ENABLED"


def test_the_early_read_is_on_by_default_and_stays_unlogged(full_env: dict[str, str]) -> None:
    settings = load_settings(env_file=None)

    assert settings.early_read_enabled is True
    assert "early_read_enabled" not in settings.safe_summary()


@pytest.mark.parametrize(("raw", "expected"), [("false", False), ("0", False), ("true", True)])
def test_the_early_read_can_be_switched(
    full_env: dict[str, str], monkeypatch: pytest.MonkeyPatch, raw: str, expected: bool
) -> None:
    monkeypatch.setenv(EARLY_READ_VARIABLE, raw)

    assert load_settings(env_file=None).early_read_enabled is expected


def test_an_empty_early_read_value_means_the_default(
    full_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(EARLY_READ_VARIABLE, "")

    assert load_settings(env_file=None).early_read_enabled is True


def test_a_nonsense_early_read_value_is_a_readable_error_without_the_value(
    full_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(EARLY_READ_VARIABLE, "sometimes-marker-zq")

    with pytest.raises(ConfigError) as raised:
        load_settings(env_file=None)

    assert EARLY_READ_VARIABLE in str(raised.value)
    assert "sometimes-marker-zq" not in str(raised.value)
