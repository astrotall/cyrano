"""JSON-lines logging setup."""

from __future__ import annotations

import json
import logging
import logging.config
from collections.abc import Mapping
from datetime import UTC, datetime

from pydantic import SecretStr

_RESERVED = frozenset(vars(logging.makeLogRecord({}))) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = "**********" if isinstance(value, SecretStr) else value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def build_log_config(
    level: str = "INFO", library_levels: Mapping[str, str] | None = None
) -> dict[str, object]:
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {"json": {"()": JsonFormatter}},
        "handlers": {
            "stdout": {
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stdout",
                "formatter": "json",
            }
        },
        "root": {"level": level.upper(), "handlers": ["stdout"]},
        "loggers": {name: {"level": lvl.upper()} for name, lvl in (library_levels or {}).items()},
    }


def configure_logging(level: str = "INFO", library_levels: Mapping[str, str] | None = None) -> None:
    logging.config.dictConfig(build_log_config(level, library_levels))
