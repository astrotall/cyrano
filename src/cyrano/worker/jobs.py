from __future__ import annotations

import logging

from arq import Retry

from cyrano.domain.autopilot import ReplyJobRequest
from cyrano.domain.enums import SummaryReason
from cyrano.domain.errors import RetryJob
from cyrano.domain.proactive import ProactiveJobRequest
from cyrano.domain.summary import SummaryJobRequest
from cyrano.services.proactive_job import ProactiveJobRunner
from cyrano.services.reply_job import ReplyJobRunner
from cyrano.services.summary_updater import SummaryUpdater

logger = logging.getLogger("cyrano.worker.jobs")

RUNNER_KEY = "reply_job_runner"
SUMMARY_UPDATER_KEY = "summary_updater"
PROACTIVE_RUNNER_KEY = "proactive_job_runner"
JOB_TRY_KEY = "job_try"
FIRST_ATTEMPT = 1
MISSING_RUNNER_MESSAGE = "the worker context has no reply job runner"
MISSING_UPDATER_MESSAGE = "the worker context has no summary updater"
MISSING_PROACTIVE_RUNNER_MESSAGE = "the worker context has no proactive job runner"


def runner_from(ctx: dict[str, object]) -> ReplyJobRunner:
    runner = ctx.get(RUNNER_KEY)
    if not isinstance(runner, ReplyJobRunner):
        raise RuntimeError(MISSING_RUNNER_MESSAGE)
    return runner


def updater_from(ctx: dict[str, object]) -> SummaryUpdater:
    updater = ctx.get(SUMMARY_UPDATER_KEY)
    if not isinstance(updater, SummaryUpdater):
        raise RuntimeError(MISSING_UPDATER_MESSAGE)
    return updater


def proactive_runner_from(ctx: dict[str, object]) -> ProactiveJobRunner:
    runner = ctx.get(PROACTIVE_RUNNER_KEY)
    if not isinstance(runner, ProactiveJobRunner):
        raise RuntimeError(MISSING_PROACTIVE_RUNNER_MESSAGE)
    return runner


def attempt_from(ctx: dict[str, object]) -> int:
    attempt = ctx.get(JOB_TRY_KEY)
    return attempt if isinstance(attempt, int) else FIRST_ATTEMPT


async def reply_to_contact(ctx: dict[str, object], contact_id: int, trigger_message_id: int) -> str:
    request = ReplyJobRequest(contact_id=contact_id, trigger_message_id=trigger_message_id)
    runner = runner_from(ctx)
    try:
        result = await runner.run(
            request.contact_id, request.trigger_message_id, attempt=attempt_from(ctx)
        )
    except RetryJob as exc:
        raise Retry(defer=exc.defer_seconds) from None
    return result.reason.value


async def proactive_to_contact(
    ctx: dict[str, object], contact_id: int, last_message_id: int
) -> str:
    request = ProactiveJobRequest(contact_id=contact_id, last_message_id=last_message_id)
    runner = proactive_runner_from(ctx)
    try:
        result = await runner.run(
            request.contact_id, request.last_message_id, attempt=attempt_from(ctx)
        )
    except RetryJob as exc:
        raise Retry(defer=exc.defer_seconds) from None
    return result.reason.value


async def update_contact_summary(
    ctx: dict[str, object], contact_id: int, up_to_message_id: int
) -> str:
    request = SummaryJobRequest(contact_id=contact_id, up_to_message_id=up_to_message_id)
    updater = updater_from(ctx)
    try:
        result = await updater.update(request.contact_id)
    except Exception as exc:
        logger.error(
            "summary job failed",
            extra={
                "contact_id": request.contact_id,
                "up_to_message_id": request.up_to_message_id,
                "error": type(exc).__name__,
            },
        )
        return SummaryReason.UNEXPECTED_ERROR.value
    logger.info(
        "summary job finished",
        extra={
            "contact_id": request.contact_id,
            "up_to_message_id": request.up_to_message_id,
            "outcome": result.outcome.value,
            "reason": result.reason.value,
        },
    )
    return result.reason.value
