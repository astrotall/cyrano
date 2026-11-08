from __future__ import annotations

import logging

from cyrano.config import ConfigError, load_settings
from cyrano.domain.autopilot import (
    JOB_MAX_TRIES,
    JOB_TIMEOUT_SECONDS,
    WORKER_HEALTH_CHECK_INTERVAL_SECONDS,
    WORKER_MAX_JOBS,
)
from cyrano.logging import build_log_config, configure_logging
from cyrano.worker.composition import WorkerResources, build_worker_resources
from cyrano.worker.jobs import (
    PROACTIVE_RUNNER_KEY,
    RUNNER_KEY,
    SUMMARY_UPDATER_KEY,
    proactive_to_contact,
    reply_to_contact,
    update_contact_summary,
)
from cyrano.worker.queue import redis_settings

logger = logging.getLogger("cyrano.worker")

RESOURCES_KEY = "worker_resources"
KEEP_NO_RESULT = 0

try:
    _settings = load_settings()
except ConfigError as exc:
    raise SystemExit(str(exc)) from None


LOG_CONFIG = build_log_config(_settings.log_level, _settings.library_log_levels)


async def startup(ctx: dict[str, object]) -> None:
    configure_logging(_settings.log_level, _settings.library_log_levels)
    resources = build_worker_resources(_settings)
    ctx[RESOURCES_KEY] = resources
    ctx[RUNNER_KEY] = resources.runner
    ctx[SUMMARY_UPDATER_KEY] = resources.summary_updater
    ctx[PROACTIVE_RUNNER_KEY] = resources.proactive_runner
    logger.info("cyrano worker started", extra={"service": "worker", **_settings.safe_summary()})


async def shutdown(ctx: dict[str, object]) -> None:
    resources = ctx.pop(RESOURCES_KEY, None)
    ctx.pop(RUNNER_KEY, None)
    ctx.pop(SUMMARY_UPDATER_KEY, None)
    ctx.pop(PROACTIVE_RUNNER_KEY, None)
    if isinstance(resources, WorkerResources):
        await resources.aclose()
    logger.info("cyrano worker stopped", extra={"service": "worker"})


class WorkerSettings:
    functions = (reply_to_contact, update_contact_summary, proactive_to_contact)
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = redis_settings(_settings)
    max_tries = JOB_MAX_TRIES
    job_timeout = JOB_TIMEOUT_SECONDS
    keep_result = KEEP_NO_RESULT
    max_jobs = WORKER_MAX_JOBS
    retry_jobs = True
    health_check_interval = WORKER_HEALTH_CHECK_INTERVAL_SECONDS
