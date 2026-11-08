from __future__ import annotations

import inspect
import logging
from datetime import UTC, datetime, timedelta

import pytest

from cyrano.db.repositories import (
    SqlAutopilotStateRepository,
    SqlContactRepository,
    SqlMessageRepository,
    SqlModeRepository,
    SqlSummaryRepository,
)
from cyrano.db.session import SessionFactory
from cyrano.domain.autopilot import SWEEP_INTERVAL_SECONDS
from cyrano.domain.enums import (
    AutopilotPauseReason,
    AutopilotStopReason,
    ContactStatus,
    MessageDirection,
)
from cyrano.domain.models import DEFAULT_MODE_NAME, ContactCreate, MessageCreate
from cyrano.domain.summary import (
    SUMMARY_FAILURE_COOLDOWN_SECONDS,
    SUMMARY_SWEEP_LIMIT,
    SummaryDue,
)
from cyrano.services.summary_sweeper import SummarySweeper
from tests.capped_loop import CappedLoop
from tests.privacy import FAKE_TELEGRAM_ID
from tests.services.outbox_fakes import FakeClock
from tests.services.summary_fakes import InMemorySummaries, RecordingSummaryQueue

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
EVERY = 4
DUE = [
    SummaryDue(contact_id=3, waiting=9, up_to_message_id=90),
    SummaryDue(contact_id=5, waiting=4, up_to_message_id=51),
]


def _sweeper(
    summaries: InMemorySummaries | SqlSummaryRepository,
    queue: RecordingSummaryQueue,
    *,
    enabled: bool = True,
    clock: FakeClock | None = None,
) -> SummarySweeper:
    return SummarySweeper(
        summaries,
        queue,
        enabled=enabled,
        every_messages=EVERY,
        clock=clock or FakeClock(NOW),
    )


async def test_every_due_contact_gets_one_job_with_its_newest_message() -> None:
    summaries = InMemorySummaries(DUE)
    queue = RecordingSummaryQueue()

    enqueued = await _sweeper(summaries, queue).sweep()

    assert enqueued == 2
    assert queue.jobs == [(3, 90), (5, 51)]
    assert summaries.due_calls == [
        (EVERY, timedelta(seconds=SUMMARY_FAILURE_COOLDOWN_SECONDS), NOW, SUMMARY_SWEEP_LIMIT)
    ]


async def test_nothing_is_read_or_enqueued_while_summaries_are_switched_off() -> None:
    summaries = InMemorySummaries(DUE)
    queue = RecordingSummaryQueue()

    assert await _sweeper(summaries, queue, enabled=False).sweep() == 0
    assert summaries.due_calls == []
    assert queue.jobs == []


def test_the_sweeper_knows_nothing_of_the_autopilot() -> None:
    parameters = set(inspect.signature(SummarySweeper).parameters)

    assert parameters == {
        "summaries",
        "queue",
        "enabled",
        "every_messages",
        "clock",
        "sleep",
        "interval",
        "limit",
    }


async def _active_contact_with_messages(session_factory: SessionFactory, count: int) -> int:
    contacts = SqlContactRepository(session_factory)
    modes = SqlModeRepository(session_factory)
    messages = SqlMessageRepository(session_factory)
    contact = await contacts.create(ContactCreate(tg_id=FAKE_TELEGRAM_ID))
    await contacts.set_status(contact.id, ContactStatus.PENDING)
    default = await modes.get_by_name(DEFAULT_MODE_NAME)
    assert default is not None
    await contacts.set_status(contact.id, ContactStatus.ACTIVE, mode_id=default.id)
    for number in range(count):
        await messages.append(
            MessageCreate(
                contact_id=contact.id,
                direction=MessageDirection.INCOMING,
                text=f"invented {number}",
                tg_message_id=number + 1,
                created_at=NOW - timedelta(minutes=30 - number),
            )
        )
    return contact.id


async def test_a_stopped_and_paused_autopilot_does_not_stop_the_summary_scan(
    session_factory: SessionFactory,
) -> None:
    contact_id = await _active_contact_with_messages(session_factory, EVERY)
    state = SqlAutopilotStateRepository(session_factory)
    await state.stop(AutopilotStopReason.PROVIDER_AUTH)
    await state.pause_until(
        NOW + timedelta(hours=1), AutopilotPauseReason.PROVIDER_UNAVAILABLE, now=NOW
    )
    queue = RecordingSummaryQueue()

    assert await _sweeper(SqlSummaryRepository(session_factory), queue).sweep() == 1
    assert [job[0] for job in queue.jobs] == [contact_id]


async def test_the_failure_cooldown_is_respected(session_factory: SessionFactory) -> None:
    contact_id = await _active_contact_with_messages(session_factory, EVERY)
    summaries = SqlSummaryRepository(session_factory)
    await summaries.mark_summary_failed(contact_id, NOW)
    clock = FakeClock(NOW)
    queue = RecordingSummaryQueue()
    sweeper = _sweeper(summaries, queue, clock=clock)

    clock.advance(SUMMARY_FAILURE_COOLDOWN_SECONDS - 1)
    assert await sweeper.sweep() == 0
    clock.advance(1)
    assert await sweeper.sweep() == 1


async def test_below_the_threshold_nothing_is_enqueued(session_factory: SessionFactory) -> None:
    await _active_contact_with_messages(session_factory, EVERY - 1)
    queue = RecordingSummaryQueue()

    assert await _sweeper(SqlSummaryRepository(session_factory), queue).sweep() == 0


async def test_a_queue_failure_is_logged_by_class_and_the_others_are_still_enqueued(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    queue = RecordingSummaryQueue(failing_ids=frozenset({3}))

    assert await _sweeper(InMemorySummaries(DUE), queue).sweep() == 1

    assert queue.jobs == [(5, 51)]
    [warning] = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warning.getMessage() == "summary sweep could not enqueue every job"
    assert warning.__dict__["error"] == "ConnectionError"
    assert (warning.__dict__["found"], warning.__dict__["enqueued"]) == (2, 1)


async def test_the_loop_keeps_sweeping_after_a_queue_failure_and_a_storage_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    summaries = InMemorySummaries(DUE, error=RuntimeError("invented storage failure"))
    queue = RecordingSummaryQueue(failing_ids=frozenset({3}))
    sleeps: list[float] = []

    async def on_sleep(seconds: float) -> bool:
        sleeps.append(seconds)
        summaries.error = None
        return len(queue.jobs) >= 2

    capped = CappedLoop(on_sleep, cap=10)
    sweeper = SummarySweeper(
        summaries,
        queue,
        enabled=True,
        every_messages=EVERY,
        clock=FakeClock(NOW),
        sleep=capped.sleep,
    )

    await capped.run(sweeper.run())

    assert sleeps == [SWEEP_INTERVAL_SECONDS] * 3
    assert queue.jobs == [(5, 51), (5, 51)]
    errors = [r for r in caplog.records if r.getMessage() == "summary sweep failed"]
    assert [r.__dict__["error"] for r in errors] == ["RuntimeError"]
