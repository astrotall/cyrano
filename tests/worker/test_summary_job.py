from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from cyrano.config import load_settings
from cyrano.domain.enums import SummaryOutcome, SummaryReason
from cyrano.domain.summary import SUMMARY_JOB_NAME, SummaryUpdateResult
from cyrano.services.summary_updater import SummaryUpdater
from cyrano.worker import queue as queue_module
from cyrano.worker.jobs import SUMMARY_UPDATER_KEY, update_contact_summary
from cyrano.worker.queue import ArqReplyJobQueue, ArqSummaryJobQueue
from tests.services.summary_fakes import InMemorySummaries, ScriptedJsonClient
from tests.userbot.fakes import InMemoryContacts

UPDATED = SummaryUpdateResult(outcome=SummaryOutcome.UPDATED, reason=SummaryReason.UPDATED)


class _Updater(SummaryUpdater):
    def __init__(self, outcome: SummaryUpdateResult | Exception) -> None:
        super().__init__(
            contacts=InMemoryContacts(),
            summaries=InMemorySummaries(),
            client=ScriptedJsonClient(),
            clock=lambda: datetime(2026, 11, 8, tzinfo=UTC),
        )
        self.outcome = outcome
        self.calls: list[int] = []

    async def update(self, contact_id: int) -> SummaryUpdateResult:
        self.calls.append(contact_id)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


async def test_the_job_updates_the_contact_and_returns_the_reason(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    updater = _Updater(UPDATED)

    result = await update_contact_summary({SUMMARY_UPDATER_KEY: updater}, 7, 41)

    assert result == SummaryReason.UPDATED.value
    assert updater.calls == [7]
    [line] = [r for r in caplog.records if r.getMessage() == "summary job finished"]
    assert line.__dict__["up_to_message_id"] == 41
    assert line.__dict__["outcome"] == "updated"


async def test_an_unexpected_exception_is_logged_by_class_and_never_raised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    updater = _Updater(RuntimeError("invented failure text-zq"))

    result = await update_contact_summary({SUMMARY_UPDATER_KEY: updater}, 7, 41)

    assert result == SummaryReason.UNEXPECTED_ERROR.value
    [line] = [r for r in caplog.records if r.getMessage() == "summary job failed"]
    assert line.__dict__["error"] == "RuntimeError"
    assert "text-zq" not in caplog.text


async def test_a_context_without_an_updater_is_an_error() -> None:
    with pytest.raises(RuntimeError, match="no summary updater"):
        await update_contact_summary({}, 7, 41)


@pytest.mark.parametrize(("contact_id", "up_to"), [(0, 41), (7, 0), (-3, 2)])
async def test_invalid_job_arguments_are_refused_before_the_updater(
    contact_id: int, up_to: int
) -> None:
    updater = _Updater(UPDATED)

    with pytest.raises(ValidationError):
        await update_contact_summary({SUMMARY_UPDATER_KEY: updater}, contact_id, up_to)

    assert updater.calls == []


@dataclass
class _Pool:
    jobs: list[tuple[str, tuple[object, ...], dict[str, object]]] = field(default_factory=list)

    async def enqueue_job(self, function: str, *args: object, **kwargs: object) -> None:
        self.jobs.append((function, args, kwargs))

    async def aclose(self) -> None:
        return None


async def test_the_summary_queue_uses_the_shared_pool_and_the_deterministic_id(
    full_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = _Pool()
    connects: list[int] = []

    async def create_pool(settings: object) -> _Pool:
        connects.append(1)
        return pool

    monkeypatch.setattr(queue_module, "create_pool", create_pool)
    replies = ArqReplyJobQueue(load_settings(env_file=None))
    summaries = ArqSummaryJobQueue(replies)

    await summaries.enqueue(7, 41)
    await replies.enqueue(7, 42, 12)
    await summaries.enqueue(7, 41)

    assert pool.jobs[0] == (SUMMARY_JOB_NAME, (7, 41), {"_job_id": "summary-7-41"})
    assert pool.jobs[2] == pool.jobs[0]
    assert connects == [1]
