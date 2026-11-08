from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from collections.abc import Iterator

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from cyrano.bot.app import NoticeBot
from cyrano.domain.autopilot import JOB_MAX_TRIES, JOB_TIMEOUT_SECONDS, REPLY_JOB_NAME
from cyrano.domain.proactive import PROACTIVE_JOB_NAME
from cyrano.domain.summary import SUMMARY_JOB_NAME
from cyrano.llm.factory import LLMClientFactory
from cyrano.logging import JsonFormatter
from cyrano.services.proactive_job import ProactiveJobRunner
from cyrano.services.reply_job import ReplyJobRunner
from cyrano.services.summary_updater import SummaryUpdater
from cyrano.worker.jobs import PROACTIVE_RUNNER_KEY, RUNNER_KEY, SUMMARY_UPDATER_KEY
from tests.conftest import PERSONAL_SETTING_NAMES, PERSONAL_VALUES, SECRET_VALUES
from tests.privacy import assert_absent

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
        assert_absent(value, out)
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


def test_worker_settings_register_the_reply_and_the_summary_job_with_their_limits(
    full_env: dict[str, str],
) -> None:
    import cyrano.worker.settings as module
    from cyrano.worker.jobs import proactive_to_contact, reply_to_contact, update_contact_summary

    settings = module.WorkerSettings

    assert settings.functions == (reply_to_contact, update_contact_summary, proactive_to_contact)
    assert reply_to_contact.__qualname__ == REPLY_JOB_NAME
    assert update_contact_summary.__qualname__ == SUMMARY_JOB_NAME
    assert proactive_to_contact.__qualname__ == PROACTIVE_JOB_NAME
    assert not hasattr(module, "noop")
    assert settings.max_tries == JOB_MAX_TRIES == 4
    assert settings.keep_result == 0
    assert settings.job_timeout == JOB_TIMEOUT_SECONDS == 300
    assert 1 <= settings.max_jobs <= 4
    assert settings.retry_jobs is True
    assert settings.health_check_interval == 60


async def test_startup_builds_the_runner_and_shutdown_closes_everything(
    full_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import cyrano.worker.settings as module

    closed: list[str] = []

    async def close_bot(self: NoticeBot) -> None:
        closed.append("bot")

    async def close_llm(self: LLMClientFactory) -> None:
        closed.append("llm")

    async def dispose(self: AsyncEngine, close: bool = True) -> None:
        closed.append("engine")

    monkeypatch.setattr(NoticeBot, "close", close_bot)
    monkeypatch.setattr(LLMClientFactory, "aclose", close_llm)
    monkeypatch.setattr(AsyncEngine, "dispose", dispose)
    ctx: dict[str, object] = {}

    await module.startup(ctx)
    runner = ctx[RUNNER_KEY]
    updater = ctx[SUMMARY_UPDATER_KEY]
    proactive_runner = ctx[PROACTIVE_RUNNER_KEY]
    await module.shutdown(ctx)

    assert isinstance(runner, ReplyJobRunner)
    assert isinstance(updater, SummaryUpdater)
    assert isinstance(proactive_runner, ProactiveJobRunner)
    assert closed == ["bot", "llm", "engine"]
    assert ctx == {}


async def test_a_failing_bot_close_still_closes_the_client_and_the_engine(
    full_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from cyrano.config import load_settings
    from cyrano.worker.composition import build_worker_resources

    closed: list[str] = []

    async def close_bot(self: NoticeBot) -> None:
        raise RuntimeError("invented close failure")

    async def close_llm(self: LLMClientFactory) -> None:
        closed.append("llm")

    async def dispose(self: AsyncEngine, close: bool = True) -> None:
        closed.append("engine")

    monkeypatch.setattr(NoticeBot, "close", close_bot)
    monkeypatch.setattr(LLMClientFactory, "aclose", close_llm)
    monkeypatch.setattr(AsyncEngine, "dispose", dispose)
    resources = build_worker_resources(load_settings(env_file=None))

    with pytest.raises(RuntimeError):
        await resources.aclose()

    assert closed == ["llm", "engine"]


def test_the_worker_never_imports_telethon(full_env: dict[str, str]) -> None:
    probe = (
        "import sys, cyrano.worker.settings; "
        "assert not [name for name in sys.modules if name.split('.')[0] == 'telethon']"
    )
    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        env={**os.environ, **full_env},
        cwd="/",
        timeout=60,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr[-400:]
