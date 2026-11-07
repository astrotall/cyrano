from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from arq import Retry
from pydantic import ValidationError

from cyrano.config import load_settings
from cyrano.domain.autopilot import REPLY_JOB_NAME, ReplyJobResult
from cyrano.domain.enums import ReplyJobOutcome, ReplyJobReason
from cyrano.domain.errors import RetryJob
from cyrano.services.reply_job import ReplyJobRunner
from cyrano.worker import queue as queue_module
from cyrano.worker.jobs import JOB_TRY_KEY, RUNNER_KEY, reply_to_contact
from cyrano.worker.queue import ArqReplyJobQueue

QUEUED = ReplyJobResult(outcome=ReplyJobOutcome.QUEUED, reason=ReplyJobReason.QUEUED, row_id=3)


class _Runner(ReplyJobRunner):
    def __init__(self, outcome: ReplyJobResult | RetryJob) -> None:
        self.outcome = outcome
        self.calls: list[tuple[int, int, int]] = []

    async def run(
        self, contact_id: int, trigger_message_id: int, *, attempt: int = 1
    ) -> ReplyJobResult:
        self.calls.append((contact_id, trigger_message_id, attempt))
        if isinstance(self.outcome, RetryJob):
            raise self.outcome
        return self.outcome


async def test_the_job_calls_the_runner_with_the_try_number() -> None:
    runner = _Runner(QUEUED)

    result = await reply_to_contact({RUNNER_KEY: runner, JOB_TRY_KEY: 3}, 7, 41)

    assert result == ReplyJobReason.QUEUED.value
    assert runner.calls == [(7, 41, 3)]


async def test_a_retry_becomes_an_arq_retry_with_the_defer() -> None:
    runner = _Runner(RetryJob(300, "LLMUnavailableError"))

    with pytest.raises(Retry) as raised:
        await reply_to_contact({RUNNER_KEY: runner, JOB_TRY_KEY: 2}, 7, 41)

    assert raised.value.defer_score == 300_000


async def test_a_missing_try_number_counts_as_the_first() -> None:
    runner = _Runner(QUEUED)

    await reply_to_contact({RUNNER_KEY: runner}, 7, 41)

    assert runner.calls == [(7, 41, 1)]


async def test_a_context_without_a_runner_is_an_error() -> None:
    with pytest.raises(RuntimeError, match="no reply job runner"):
        await reply_to_contact({}, 7, 41)


@pytest.mark.parametrize(("contact_id", "trigger_id"), [(0, 41), (7, -1)])
async def test_invalid_job_arguments_are_refused_before_the_runner(
    contact_id: int, trigger_id: int
) -> None:
    runner = _Runner(QUEUED)

    with pytest.raises(ValidationError):
        await reply_to_contact({RUNNER_KEY: runner}, contact_id, trigger_id)

    assert runner.calls == []


@dataclass
class _Pool:
    jobs: list[tuple[str, tuple[object, ...], dict[str, object]]] = field(default_factory=list)
    closed: int = 0

    async def enqueue_job(self, function: str, *args: object, **kwargs: object) -> None:
        self.jobs.append((function, args, kwargs))

    async def aclose(self) -> None:
        self.closed += 1


async def test_the_queue_enqueues_with_the_deterministic_job_id_and_the_defer(
    full_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = _Pool()
    connects: list[int] = []

    async def create_pool(settings: object) -> _Pool:
        connects.append(getattr(settings, "conn_retries", -1))
        return pool

    monkeypatch.setattr(queue_module, "create_pool", create_pool)
    queue = ArqReplyJobQueue(load_settings(env_file=None))

    await queue.start()
    await queue.enqueue(7, 41, 12)
    await queue.enqueue(7, 41, 12)
    await queue.close()
    await queue.close()

    assert pool.jobs == [
        (REPLY_JOB_NAME, (7, 41), {"_job_id": "reply-7-41", "_defer_by": 12}),
        (REPLY_JOB_NAME, (7, 41), {"_job_id": "reply-7-41", "_defer_by": 12}),
    ]
    assert connects == [1]
    assert pool.closed == 1


async def test_a_failed_start_is_logged_and_the_next_enqueue_connects_again(
    full_env: dict[str, str], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    pool = _Pool()
    attempts: list[int] = []

    async def create_pool(settings: object) -> _Pool:
        attempts.append(1)
        if len(attempts) == 1:
            raise ConnectionError("redis-password-secret refused")
        return pool

    monkeypatch.setattr(queue_module, "create_pool", create_pool)
    queue = ArqReplyJobQueue(load_settings(env_file=None))

    await queue.start()
    await queue.enqueue(7, 41, 0)

    assert len(attempts) == 2
    assert len(pool.jobs) == 1
    [record] = [r for r in caplog.records if r.getMessage() == "reply job queue is not connected"]
    assert record.__dict__["error"] == "ConnectionError"
    assert "redis-password-secret" not in caplog.text


async def test_a_close_without_a_pool_does_nothing(full_env: dict[str, str]) -> None:
    await ArqReplyJobQueue(load_settings(env_file=None)).close()
