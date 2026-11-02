"""Arq worker settings. Start with ``arq cyrano.worker.settings.WorkerSettings``."""

from __future__ import annotations

import logging

from arq.connections import RedisSettings

from cyrano.config import ConfigError, load_settings
from cyrano.logging import build_log_config, configure_logging

logger = logging.getLogger("cyrano.worker")

try:
    _settings = load_settings()
except ConfigError as exc:
    raise SystemExit(str(exc)) from None


LOG_CONFIG = build_log_config(_settings.log_level, _settings.library_log_levels)


async def startup(ctx: dict[str, object]) -> None:
    configure_logging(_settings.log_level, _settings.library_log_levels)
    logger.info("cyrano worker started", extra={"service": "worker", **_settings.safe_summary()})


async def shutdown(ctx: dict[str, object]) -> None:
    logger.info("cyrano worker stopped", extra={"service": "worker"})


async def noop(ctx: dict[str, object]) -> None:
    return None


class WorkerSettings:
    functions = (noop,)
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(_settings.redis_url.get_secret_value())
    health_check_interval = 60
