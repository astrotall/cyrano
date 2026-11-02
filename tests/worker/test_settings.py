from __future__ import annotations

import importlib
import json
import sys
from collections.abc import Iterator

import pytest

from cyrano.logging import JsonFormatter
from tests.conftest import PERSONAL_SETTING_NAMES, PERSONAL_VALUES, SECRET_VALUES

PACKAGE_NAME = "cyrano.worker"
MODULE_NAME = "cyrano.worker.settings"
ATTRIBUTE_NAME = "settings"


@pytest.fixture(autouse=True)
def fresh_worker_settings_module(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.chdir("/")
    package = importlib.import_module(PACKAGE_NAME)
    saved_module = sys.modules.pop(MODULE_NAME, None)
    saved_attribute = package.__dict__.pop(ATTRIBUTE_NAME, None)
    yield
    sys.modules.pop(MODULE_NAME, None)
    package.__dict__.pop(ATTRIBUTE_NAME, None)
    if saved_module is not None:
        sys.modules[MODULE_NAME] = saved_module
    if saved_attribute is not None:
        package.__dict__[ATTRIBUTE_NAME] = saved_attribute


def test_worker_settings_use_redis_url_from_config(full_env: dict[str, str]) -> None:
    import cyrano.worker.settings as module

    redis_settings = module.WorkerSettings.redis_settings

    assert redis_settings.host == "redis"
    assert redis_settings.port == 6379
    assert redis_settings.database == 0
    assert len(module.WorkerSettings.functions) >= 1


async def test_startup_logs_without_secrets_or_personal_identifiers(
    full_env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    import cyrano.worker.settings as module

    await module.WorkerSettings.on_startup({})
    await module.WorkerSettings.on_shutdown({})

    out = capsys.readouterr().out
    records = [json.loads(line) for line in out.splitlines() if line]
    assert [r["msg"] for r in records] == ["cyrano worker started", "cyrano worker stopped"]
    for value in (*SECRET_VALUES, *PERSONAL_VALUES):
        assert value not in out
    for name in PERSONAL_SETTING_NAMES:
        assert name not in records[0]


def test_import_without_config_exits_with_readable_message() -> None:
    with pytest.raises(SystemExit) as excinfo:
        importlib.import_module(MODULE_NAME)

    assert "Missing required environment variables" in str(excinfo.value)
    assert MODULE_NAME not in sys.modules


def test_log_config_for_arq_cli_uses_json_formatter_and_library_levels(
    full_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("TELETHON_LOG_LEVEL", "ERROR")
    import cyrano.worker.settings as module

    config = module.LOG_CONFIG

    assert config["root"] == {"level": "DEBUG", "handlers": ["stdout"]}
    assert config["loggers"] == {
        "telethon": {"level": "ERROR"},
        "aiogram": {"level": "WARNING"},
        "httpx": {"level": "WARNING"},
        "httpcore": {"level": "WARNING"},
    }
    assert config["formatters"] == {"json": {"()": JsonFormatter}}


def test_health_check_interval_is_set(full_env: dict[str, str]) -> None:
    import cyrano.worker.settings as module

    assert module.WorkerSettings.health_check_interval == 60
