from __future__ import annotations

import json
import logging
import logging.config

import pytest
from arq.logs import default_log_config
from pydantic import SecretStr

from cyrano.logging import build_log_config, configure_logging


def _lines(capsys: pytest.CaptureFixture[str]) -> list[dict[str, object]]:
    out = capsys.readouterr().out
    return [json.loads(line) for line in out.splitlines() if line]


def test_emits_json_lines_with_extra_fields(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO")

    logging.getLogger("cyrano.test").info("hello", extra={"service": "app", "n": 3})

    (record,) = _lines(capsys)
    assert record["msg"] == "hello"
    assert record["level"] == "INFO"
    assert record["logger"] == "cyrano.test"
    assert record["service"] == "app"
    assert record["n"] == 3
    assert "ts" in record


def test_respects_level(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("WARNING")
    log = logging.getLogger("cyrano.test")

    log.info("quiet")
    log.warning("loud")

    assert [r["msg"] for r in _lines(capsys)] == ["loud"]


def test_secretstr_extra_is_masked(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO")

    logging.getLogger("cyrano.test").info("x", extra={"token": SecretStr("super-secret")})

    out = capsys.readouterr().out
    assert "super-secret" not in out
    assert "**********" in out


def test_reconfigure_does_not_duplicate_handlers(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO")
    configure_logging("INFO")

    logging.getLogger("cyrano.test").info("once")

    assert len(_lines(capsys)) == 1


def test_library_loggers_get_their_own_level(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("DEBUG", {"telethon": "WARNING", "httpx": "ERROR"})

    logging.getLogger("telethon.network").info("telethon chatter")
    logging.getLogger("telethon.network").warning("telethon problem")
    logging.getLogger("httpx").warning("httpx warning")
    logging.getLogger("cyrano.test").debug("ours")

    assert [r["msg"] for r in _lines(capsys)] == ["telethon problem", "ours"]


def test_library_level_can_be_more_verbose_than_root(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("WARNING", {"aiogram": "DEBUG"})

    logging.getLogger("aiogram.dispatcher").debug("detail")
    logging.getLogger("cyrano.test").info("hidden")

    assert [r["msg"] for r in _lines(capsys)] == ["detail"]


def test_library_levels_default_to_none(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO")

    logging.getLogger("telethon").info("visible")

    assert [r["msg"] for r in _lines(capsys)] == ["visible"]


def test_arq_default_config_is_plain_text(capsys: pytest.CaptureFixture[str]) -> None:
    logging.config.dictConfig(default_log_config(verbose=False))

    logging.getLogger("arq.worker").info("Starting worker")

    assert "Starting worker" in capsys.readouterr().err.strip().split(": ", 1)[1]


def test_arq_lines_go_through_json_formatter_via_custom_log_dict(
    capsys: pytest.CaptureFixture[str],
) -> None:
    logging.config.dictConfig(build_log_config("INFO", {"httpx": "WARNING"}))

    logging.getLogger("arq.worker").info("Starting worker for 1 functions: noop")
    logging.getLogger("arq.jobs").info("job done", extra={"job_id": "abc"})

    first, second = _lines(capsys)
    assert first["msg"] == "Starting worker for 1 functions: noop"
    assert first["logger"] == "arq.worker"
    assert first["level"] == "INFO"
    assert second["job_id"] == "abc"


def test_arq_lines_are_json_after_configure_logging_overrides_arq_default(
    capsys: pytest.CaptureFixture[str],
) -> None:
    logging.config.dictConfig(default_log_config(verbose=False))
    configure_logging("INFO")
    capsys.readouterr()

    logging.getLogger("arq.worker").info("recording health")

    (record,) = _lines(capsys)
    assert record["msg"] == "recording health"
