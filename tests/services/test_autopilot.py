from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta

import pytest
from sqlalchemy import text

from cyrano.domain.autopilot import REPLY_STALE_AFTER_SECONDS, SWEEP_INTERVAL_SECONDS
from cyrano.domain.enums import (
    AutopilotPauseReason,
    AutopilotStopReason,
    ContactStatus,
    MessageDirection,
    OutboxSource,
    OutboxState,
)
from cyrano.domain.errors import StoredScheduleInvalid
from cyrano.domain.models import Contact, ContactCreate, IncomingMessage, Message, MessageCreate
from cyrano.domain.schedule import ScheduleProfile
from cyrano.services.autopilot import AutopilotScheduler, AutopilotSweeper
from cyrano.services.contact_events import InMemoryContactEvents
from cyrano.services.humanization import HumanizationPlanner, ScheduleService, burst_is_ongoing
from cyrano.services.intake import IntakeResult, IntakeService
from tests.capped_loop import CappedLoop
from tests.privacy import FAKE_TELEGRAM_ID
from tests.services.autopilot_fakes import InMemoryAutopilotState, RecordingQueue
from tests.services.autopilot_rig import NOW, AutopilotRig, Switch
from tests.services.outbox_fakes import (
    ALWAYS_FREE_PROFILE,
    FREE_TIMEZONE,
    FakeClock,
    free_schedule,
)
from tests.userbot.fakes import FAKE_SENT_AT, InMemoryContacts, InMemoryMessages

DEBOUNCE = 12
ONGOING_DEBOUNCE = 4
PLANNER_SEED = 3
MEETING_TEXT = "давай встретимся завтра вечером"
ONGOING_WINDOW = timedelta(minutes=ALWAYS_FREE_PROFILE.reaction.ongoing_window_minutes)


class _BrokenProfiles:
    async def get(self) -> ScheduleProfile | None:
        raise StoredScheduleInvalid

    async def replace(self, profile: ScheduleProfile) -> ScheduleProfile:
        return profile


class _Scheduler:
    def __init__(self) -> None:
        self.contacts = InMemoryContacts()
        self.messages = InMemoryMessages()
        self.state = InMemoryAutopilotState()
        self.queue = RecordingQueue()
        self.switch = Switch()
        self.clock = FakeClock(NOW)
        self.schedule = free_schedule()

    def build(self) -> AutopilotScheduler:
        return AutopilotScheduler(
            self.contacts,
            self.state,
            self.queue,
            self.switch,
            HumanizationPlanner(self.messages, self.schedule, random.Random(PLANNER_SEED)),
            debounce_seconds=DEBOUNCE,
            ongoing_debounce_seconds=ONGOING_DEBOUNCE,
            clock=self.clock,
        )

    async def contact(self, status: ContactStatus = ContactStatus.ACTIVE) -> Contact:
        contact = await self.contacts.create(ContactCreate(tg_id=FAKE_TELEGRAM_ID))
        if status is ContactStatus.NEW:
            return contact
        await self.contacts.set_status(contact.id, ContactStatus.PENDING)
        if status is ContactStatus.PENDING:
            return contact
        if status is ContactStatus.IGNORED:
            return await self.contacts.set_status(contact.id, ContactStatus.IGNORED)
        active = await self.contacts.set_status(contact.id, ContactStatus.ACTIVE, mode_id=1)
        if status is ContactStatus.PAUSED:
            return await self.contacts.set_status(contact.id, ContactStatus.PAUSED)
        return active


@pytest.fixture
def scheduling() -> _Scheduler:
    return _Scheduler()


async def test_the_scheduler_enqueues_an_active_contact_with_the_debounce(
    scheduling: _Scheduler,
) -> None:
    contact = await scheduling.contact()

    assert await scheduling.build().on_incoming(contact.id, 41) is True

    assert scheduling.queue.jobs == [(contact.id, 41, DEBOUNCE)]


async def _said(
    scheduling: _Scheduler,
    contact: Contact,
    ago: timedelta,
    direction: MessageDirection = MessageDirection.INCOMING,
) -> Message:
    return await scheduling.messages.append(
        MessageCreate(
            contact_id=contact.id,
            direction=direction,
            text="invented pace line",
            tg_message_id=len(scheduling.messages.stored) + 1,
            created_at=scheduling.clock() - ago,
        )
    )


async def test_a_message_in_an_ongoing_conversation_gets_the_short_debounce(
    scheduling: _Scheduler,
) -> None:
    contact = await scheduling.contact()
    await _said(scheduling, contact, timedelta(minutes=1), MessageDirection.OUTGOING)
    message = await _said(scheduling, contact, timedelta())

    assert await scheduling.build().on_incoming(contact.id, message.id) is True

    assert scheduling.queue.jobs == [(contact.id, message.id, ONGOING_DEBOUNCE)]


async def test_a_first_message_after_a_pause_gets_the_normal_debounce(
    scheduling: _Scheduler,
) -> None:
    contact = await scheduling.contact()
    await _said(scheduling, contact, ONGOING_WINDOW + timedelta(minutes=1))
    await _said(scheduling, contact, ONGOING_WINDOW, MessageDirection.OUTGOING)
    message = await _said(scheduling, contact, timedelta())

    await scheduling.build().on_incoming(contact.id, message.id)

    assert scheduling.queue.jobs == [(contact.id, message.id, DEBOUNCE)]


async def test_the_very_first_message_of_a_contact_gets_the_normal_debounce(
    scheduling: _Scheduler,
) -> None:
    contact = await scheduling.contact()
    message = await _said(scheduling, contact, timedelta())

    await scheduling.build().on_incoming(contact.id, message.id)

    assert scheduling.queue.jobs == [(contact.id, message.id, DEBOUNCE)]


@pytest.mark.parametrize(
    ("gap", "expected"),
    [
        (ONGOING_WINDOW - timedelta(seconds=1), ONGOING_DEBOUNCE),
        (ONGOING_WINDOW, DEBOUNCE),
        (ONGOING_WINDOW + timedelta(seconds=1), DEBOUNCE),
    ],
)
async def test_the_ongoing_window_boundary_is_the_planner_boundary(
    scheduling: _Scheduler, gap: timedelta, expected: int
) -> None:
    contact = await scheduling.contact()
    await _said(scheduling, contact, gap, MessageDirection.OUTGOING)
    message = await _said(scheduling, contact, timedelta())

    await scheduling.build().on_incoming(contact.id, message.id)

    assert scheduling.queue.jobs == [(contact.id, message.id, expected)]
    profile = await free_schedule().get_profile()
    history = await scheduling.messages.list_last(contact.id, 20)
    assert (expected == ONGOING_DEBOUNCE) is burst_is_ongoing(history, len(history) - 1, profile)


async def test_a_later_message_of_a_burst_is_judged_by_the_burst_start(
    scheduling: _Scheduler,
) -> None:
    contact = await scheduling.contact()
    await _said(scheduling, contact, ONGOING_WINDOW * 3, MessageDirection.OUTGOING)
    await _said(scheduling, contact, ONGOING_WINDOW * 1.5)
    await _said(scheduling, contact, timedelta(minutes=1))
    message = await _said(scheduling, contact, timedelta())

    await scheduling.build().on_incoming(contact.id, message.id)

    assert scheduling.queue.jobs == [(contact.id, message.id, DEBOUNCE)]


async def test_a_burst_that_started_inside_the_window_keeps_the_short_debounce(
    scheduling: _Scheduler,
) -> None:
    contact = await scheduling.contact()
    await _said(scheduling, contact, ONGOING_WINDOW, MessageDirection.OUTGOING)
    await _said(scheduling, contact, timedelta(minutes=2))
    message = await _said(scheduling, contact, timedelta())

    await scheduling.build().on_incoming(contact.id, message.id)

    assert scheduling.queue.jobs == [(contact.id, message.id, ONGOING_DEBOUNCE)]


async def test_an_unreadable_profile_falls_back_to_the_normal_debounce_and_logs_the_class(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    scheduling = _Scheduler()
    scheduling.schedule = ScheduleService(_BrokenProfiles(), FREE_TIMEZONE)
    contact = await scheduling.contact()
    await _said(scheduling, contact, timedelta(minutes=1), MessageDirection.OUTGOING)
    message = await _said(scheduling, contact, timedelta())

    assert await scheduling.build().on_incoming(contact.id, message.id) is True

    assert scheduling.queue.jobs == [(contact.id, message.id, DEBOUNCE)]
    [record] = [
        r for r in caplog.records if r.getMessage() == "the conversation pace was not decided"
    ]
    assert record.__dict__["error"] == "StoredScheduleInvalid"
    assert "invented pace line" not in caplog.text


@pytest.mark.parametrize(
    "status",
    [ContactStatus.NEW, ContactStatus.PENDING, ContactStatus.PAUSED, ContactStatus.IGNORED],
)
async def test_the_scheduler_does_nothing_for_a_contact_that_is_not_active(
    scheduling: _Scheduler, status: ContactStatus
) -> None:
    contact = await scheduling.contact(status)

    assert await scheduling.build().on_incoming(contact.id, 41) is False

    assert scheduling.queue.jobs == []


async def test_the_scheduler_does_nothing_for_a_missing_contact(scheduling: _Scheduler) -> None:
    assert await scheduling.build().on_incoming(404, 41) is False
    assert scheduling.queue.jobs == []


async def test_the_scheduler_does_nothing_with_the_switch_off(scheduling: _Scheduler) -> None:
    contact = await scheduling.contact()
    scheduling.switch.autopilot_enabled = False

    assert await scheduling.build().on_incoming(contact.id, 41) is False
    assert scheduling.queue.jobs == []


async def test_the_scheduler_does_nothing_while_the_autopilot_is_stopped(
    scheduling: _Scheduler,
) -> None:
    contact = await scheduling.contact()
    await scheduling.state.stop(AutopilotStopReason.PROVIDER_AUTH)

    assert await scheduling.build().on_incoming(contact.id, 41) is False
    assert scheduling.queue.jobs == []


async def test_the_scheduler_does_nothing_while_the_autopilot_is_paused(
    scheduling: _Scheduler,
) -> None:
    contact = await scheduling.contact()
    now = scheduling.clock()
    await scheduling.state.pause_until(
        now + timedelta(seconds=5), AutopilotPauseReason.PROVIDER_UNAVAILABLE, now=now
    )

    assert await scheduling.build().on_incoming(contact.id, 41) is False
    scheduling.clock.advance(5)
    assert await scheduling.build().on_incoming(contact.id, 41) is True


async def test_a_queue_failure_is_caught_and_logged_with_the_class_name_only(
    scheduling: _Scheduler, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    contact = await scheduling.contact()
    scheduling.queue.error = ConnectionError("redis-marker-zq refused")

    assert await scheduling.build().on_incoming(contact.id, 41) is False

    [record] = [r for r in caplog.records if r.getMessage() == "reply job was not enqueued"]
    assert record.__dict__["error"] == "ConnectionError"
    assert "redis-marker-zq" not in caplog.text


def _incoming(text: str, number: int = 1) -> IncomingMessage:
    return IncomingMessage(
        tg_id=FAKE_TELEGRAM_ID,
        username="invented_user",
        display_name="Invented Name",
        tg_message_id=number,
        text=text,
        sent_at=FAKE_SENT_AT,
    )


async def test_the_intake_result_is_unchanged_and_the_job_is_enqueued_once_per_message(
    scheduling: _Scheduler,
) -> None:
    messages = scheduling.messages
    contact = await scheduling.contact()
    plain = IntakeService(scheduling.contacts, messages, InMemoryContactEvents())
    with_scheduler = IntakeService(
        scheduling.contacts,
        messages,
        InMemoryContactEvents(),
        scheduler=scheduling.build(),
    )

    first = await plain.handle(_incoming("hello", 1))
    second = await with_scheduler.handle(_incoming("hello again", 2))

    assert first == IntakeResult(contact_id=contact.id, message_id=1, contact_created=False)
    assert second == IntakeResult(contact_id=contact.id, message_id=2, contact_created=False)
    assert scheduling.queue.jobs == [(contact.id, 2, DEBOUNCE)]


async def test_the_intake_enqueues_nothing_for_a_new_contact(scheduling: _Scheduler) -> None:
    intake = IntakeService(
        scheduling.contacts,
        InMemoryMessages(),
        InMemoryContactEvents(),
        scheduler=scheduling.build(),
    )

    result = await intake.handle(_incoming("hello"))

    assert result.contact_created is True
    assert scheduling.queue.jobs == []


async def test_a_failing_queue_never_changes_the_intake_result(scheduling: _Scheduler) -> None:
    contact = await scheduling.contact()
    scheduling.queue.error = ConnectionError("down")
    intake = IntakeService(
        scheduling.contacts,
        InMemoryMessages(),
        InMemoryContactEvents(),
        scheduler=scheduling.build(),
    )

    result = await intake.handle(_incoming("hello"))

    assert result == IntakeResult(contact_id=contact.id, message_id=1, contact_created=False)


async def test_a_gate_hand_over_prevents_the_enqueue(rig: AutopilotRig) -> None:
    contact = await rig.contact()
    queue = RecordingQueue()
    scheduler = AutopilotScheduler(
        rig.contacts,
        rig.state,
        queue,
        rig.switch,
        rig.planner(),
        debounce_seconds=DEBOUNCE,
        ongoing_debounce_seconds=ONGOING_DEBOUNCE,
        clock=rig.clock,
    )
    intake = IntakeService(
        rig.contacts,
        rig.messages,
        InMemoryContactEvents(),
        hand_over=rig.hand_over(),
        scheduler=scheduler,
    )
    message = IncomingMessage(
        tg_id=contact.tg_id, tg_message_id=91, text=MEETING_TEXT, sent_at=FAKE_SENT_AT
    )

    await intake.handle(message)

    assert (await rig.reload(contact)).status is ContactStatus.PAUSED
    assert queue.jobs == []


def _sweeper(rig: AutopilotRig, queue: RecordingQueue) -> AutopilotSweeper:
    return AutopilotSweeper(
        rig.backlog,
        rig.state,
        queue,
        rig.switch,
        debounce_seconds=DEBOUNCE,
        clock=rig.clock,
    )


async def test_the_sweeper_finds_exactly_the_unanswered_recent_incoming_messages(
    rig: AutopilotRig,
) -> None:
    waiting = await rig.contact()
    waiting_message = await rig.say(waiting, ago=60)
    answered = await rig.contact()
    await rig.say(answered, ago=90)
    await rig.say(answered, ago=60, direction=MessageDirection.OUTGOING)
    young = await rig.contact()
    await rig.say(young, ago=2 * DEBOUNCE - 1)
    exact_edge = await rig.contact()
    edge_message = await rig.say(exact_edge, ago=2 * DEBOUNCE)
    stale = await rig.contact()
    await rig.say(stale, ago=REPLY_STALE_AFTER_SECONDS + 1)
    resumed = await rig.contact(ContactStatus.PAUSED)
    before_resume = await rig.say(resumed, ago=60)
    await rig.contacts.set_status(
        resumed.id, ContactStatus.ACTIVE, reply_after=before_resume.created_at
    )
    queue = RecordingQueue()

    enqueued = await _sweeper(rig, queue).sweep()

    assert enqueued == 2
    assert sorted(queue.jobs) == sorted(
        [(waiting.id, waiting_message.id, 0), (exact_edge.id, edge_message.id, 0)]
    )


@pytest.mark.parametrize(
    "state", [OutboxState.SCHEDULED, OutboxState.SENT, OutboxState.CANCELLED, OutboxState.FAILED]
)
async def test_the_sweeper_skips_a_message_with_an_outbox_row_in_any_state(
    rig: AutopilotRig, state: OutboxState
) -> None:
    contact = await rig.contact()
    message = await rig.say(contact, ago=60)
    row = await rig.outbox.enqueue(
        contact.id, "invented reply", rig.clock(), OutboxSource.AUTOPILOT, message.id
    )
    async with rig.session_factory.begin() as session:
        await session.execute(
            text("UPDATE scheduled_messages SET state = :state WHERE id = :id"),
            {"state": state.value, "id": row.id},
        )
    queue = RecordingQueue()

    assert await _sweeper(rig, queue).sweep() == 0
    assert queue.jobs == []


@pytest.mark.parametrize(
    "status",
    [ContactStatus.NEW, ContactStatus.PENDING, ContactStatus.PAUSED, ContactStatus.IGNORED],
)
async def test_the_sweeper_skips_contacts_that_are_not_active(
    rig: AutopilotRig, status: ContactStatus
) -> None:
    contact = await rig.contact(status)
    await rig.say(contact, ago=60)
    queue = RecordingQueue()

    assert await _sweeper(rig, queue).sweep() == 0


async def test_the_sweeper_does_nothing_while_off_stopped_or_paused(rig: AutopilotRig) -> None:
    contact = await rig.contact()
    await rig.say(contact, ago=60)
    queue = RecordingQueue()
    sweeper = _sweeper(rig, queue)

    rig.switch.autopilot_enabled = False
    assert await sweeper.sweep() == 0
    rig.switch.autopilot_enabled = True
    await rig.state.stop(AutopilotStopReason.PERSONA_MISSING)
    assert await sweeper.sweep() == 0
    await rig.state.resume()
    now = rig.clock()
    await rig.state.pause_until(
        now + timedelta(seconds=60), AutopilotPauseReason.PROVIDER_UNAVAILABLE, now=now
    )
    assert await sweeper.sweep() == 0
    assert queue.jobs == []
    rig.clock.advance(60)
    assert await sweeper.sweep() == 1


async def test_the_sweeper_and_the_count_pick_the_latest_message_by_telegram_time(
    rig: AutopilotRig,
) -> None:
    caught_up = await rig.contact()
    newest = await rig.say(caught_up, ago=60)
    await rig.say(caught_up, ago=120)
    answered = await rig.contact()
    await rig.say(answered, ago=60, direction=MessageDirection.OUTGOING)
    await rig.say(answered, ago=120)
    queue = RecordingQueue()

    enqueued = await _sweeper(rig, queue).sweep()

    assert enqueued == 1
    assert queue.jobs == [(caught_up.id, newest.id, 0)]
    assert await rig.backlog.count_unanswered_active() == 1


async def test_the_sweeper_picks_up_a_message_that_arrived_while_the_contact_was_pending(
    rig: AutopilotRig,
) -> None:
    contact = await rig.contact(ContactStatus.PENDING)
    message = await rig.say(contact, ago=3600)
    await rig.activate(contact.id)
    queue = RecordingQueue()

    await _sweeper(rig, queue).sweep()

    assert queue.jobs == [(contact.id, message.id, 0)]


async def test_a_failing_queue_never_stops_the_sweep_loop(
    rig: AutopilotRig, caplog: pytest.LogCaptureFixture
) -> None:
    contact = await rig.contact()
    await rig.say(contact, ago=60)
    queue = RecordingQueue(error=ConnectionError("down"))
    sleeps: list[float] = []

    async def on_sleep(seconds: float) -> bool:
        sleeps.append(seconds)
        if len(sleeps) == 2:
            queue.error = None
        return bool(queue.jobs)

    capped = CappedLoop(on_sleep, cap=10)
    sweeper = AutopilotSweeper(
        rig.backlog,
        rig.state,
        queue,
        rig.switch,
        debounce_seconds=DEBOUNCE,
        clock=rig.clock,
        sleep=capped.sleep,
    )

    await capped.run(sweeper.run())

    assert sleeps == [SWEEP_INTERVAL_SECONDS] * 3
    assert len(queue.jobs) == 1
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert all(r.__dict__.get("error") == "ConnectionError" for r in warnings)


async def test_a_failing_sweep_never_stops_the_loop(rig: AutopilotRig) -> None:
    calls: list[int] = []

    class _FailingBacklog:
        async def awaiting_reply(
            self, *, newer_than: datetime, older_than: datetime
        ) -> list[Message]:
            calls.append(1)
            raise RuntimeError("invented storage failure")

        async def count_unanswered_active(self) -> int:
            return 0

    async def on_sleep(seconds: float) -> bool:
        return len(calls) >= 3

    capped = CappedLoop(on_sleep, cap=10)
    sweeper = AutopilotSweeper(
        _FailingBacklog(),
        rig.state,
        RecordingQueue(),
        rig.switch,
        debounce_seconds=DEBOUNCE,
        clock=rig.clock,
        sleep=capped.sleep,
    )

    await capped.run(sweeper.run())

    assert len(calls) == 3
