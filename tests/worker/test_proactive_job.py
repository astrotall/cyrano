from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from arq import Retry
from pydantic import ValidationError

from cyrano.config import load_settings
from cyrano.domain.enums import ProactiveJobOutcome, ProactiveJobReason
from cyrano.domain.errors import RetryJob
from cyrano.domain.proactive import PROACTIVE_JOB_NAME, ProactiveJobResult
from cyrano.services.proactive_job import ProactiveJobRunner
from cyrano.worker import queue as queue_module
from cyrano.worker.jobs import JOB_TRY_KEY, PROACTIVE_RUNNER_KEY, proactive_to_contact
from cyrano.worker.queue import ArqProactiveJobQueue, ArqReplyJobQueue

QUEUED = ProactiveJobResult(outcome=ProactiveJobOutcome.QUEUED, reason=ProactiveJobReason.QUEUED)


class _Runner(ProactiveJobRunner):
    def __init__(self, outcome: ProactiveJobResult | Exception) -> None:
        self.outcome = outcome
        self.calls: list[tuple[int, int, int]] = []

    async def run(
        self, contact_id: int, last_message_id: int, *, attempt: int = 1
    ) -> ProactiveJobResult:
        self.calls.append((contact_id, last_message_id, attempt))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


async def test_the_job_runs_the_runner_with_the_attempt_and_returns_the_reason() -> None:
    runner = _Runner(QUEUED)

    result = await proactive_to_contact({PROACTIVE_RUNNER_KEY: runner, JOB_TRY_KEY: 3}, 7, 41)

    assert result == ProactiveJobReason.QUEUED.value
    assert runner.calls == [(7, 41, 3)]


async def test_a_retry_becomes_an_arq_retry_with_the_delay() -> None:
    runner = _Runner(RetryJob(300, "LLMUnavailableError"))

    with pytest.raises(Retry) as raised:
        await proactive_to_contact({PROACTIVE_RUNNER_KEY: runner}, 7, 41)

    assert raised.value.defer_score == 300_000


async def test_a_context_without_the_runner_is_an_error() -> None:
    with pytest.raises(RuntimeError, match="no proactive job runner"):
        await proactive_to_contact({}, 7, 41)


@pytest.mark.parametrize(("contact_id", "last_id"), [(0, 41), (7, 0), (-3, 2)])
async def test_invalid_job_arguments_are_refused_before_the_runner(
    contact_id: int, last_id: int
) -> None:
    runner = _Runner(QUEUED)

    with pytest.raises(ValidationError):
        await proactive_to_contact({PROACTIVE_RUNNER_KEY: runner}, contact_id, last_id)

    assert runner.calls == []


@dataclass
class _Pool:
    jobs: list[tuple[str, tuple[object, ...], dict[str, object]]] = field(default_factory=list)

    async def enqueue_job(self, function: str, *args: object, **kwargs: object) -> None:
        self.jobs.append((function, args, kwargs))

    async def aclose(self) -> None:
        return None


async def test_the_proactive_queue_uses_the_shared_pool_and_the_deterministic_id(
    full_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = _Pool()
    connects: list[int] = []

    async def create_pool(settings: object) -> _Pool:
        connects.append(1)
        return pool

    monkeypatch.setattr(queue_module, "create_pool", create_pool)
    replies = ArqReplyJobQueue(load_settings(env_file=None))
    proactive = ArqProactiveJobQueue(replies)

    await proactive.enqueue(7, 41)
    await replies.enqueue(7, 42, 12)
    await proactive.enqueue(7, 41)

    assert pool.jobs[0] == (PROACTIVE_JOB_NAME, (7, 41), {"_job_id": "proactive-7-41"})
    assert pool.jobs[2] == pool.jobs[0]
    assert connects == [1]
