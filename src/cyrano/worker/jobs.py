"""The Arq job functions. They only call the services; nothing here sends to a contact."""

from __future__ import annotations

from arq import Retry

from cyrano.domain.autopilot import ReplyJobRequest
from cyrano.domain.errors import RetryJob
from cyrano.services.reply_job import ReplyJobRunner

RUNNER_KEY = "reply_job_runner"
JOB_TRY_KEY = "job_try"
FIRST_ATTEMPT = 1
MISSING_RUNNER_MESSAGE = "the worker context has no reply job runner"


def runner_from(ctx: dict[str, object]) -> ReplyJobRunner:
    runner = ctx.get(RUNNER_KEY)
    if not isinstance(runner, ReplyJobRunner):
        raise RuntimeError(MISSING_RUNNER_MESSAGE)
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
