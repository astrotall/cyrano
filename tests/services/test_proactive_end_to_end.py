from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import pytest

from cyrano.bot.notifier import OUTBOX_NOTICES
from cyrano.domain.enums import OutboxSource, OutboxState, ProactiveJobOutcome
from cyrano.domain.models import Contact
from cyrano.domain.outbox import CANCEL_REASON_SUPERSEDED, ScheduledMessage
from cyrano.domain.schedule import ScheduleProfile
from cyrano.services.humanization import ScheduleService, blocked, in_proactive_window
from cyrano.services.outbox_sender import OutboxSender
from cyrano.services.proactive_sweeper import ProactiveSweeper
from cyrano.services.send_guard import SendGuard
from cyrano.services.send_limiter import SendLimiter
from tests.services.autopilot_rig import AutopilotRig, SequenceLLMClient, json_logs, llm_result
from tests.services.outbox_fakes import (
    ALWAYS_FREE_PROFILE,
    MARK_READ_STEP,
    SEND_TEXT_STEP,
    SHOW_TYPING_STEP,
    FakeLimits,
    InMemoryScheduleProfiles,
    RecordingNotifier,
    ScriptedTransport,
)
from tests.services.proactive_rig import FOLLOW_UP_TEXT_ZQ, OUR_TEXT_ZQ, ProactiveRig

PAUSE_STEP = "pause"
HUMAN_ORDER = [MARK_READ_STEP, PAUSE_STEP, SHOW_TYPING_STEP, SEND_TEXT_STEP]
WEDNESDAY_AFTERNOON = datetime(2026, 11, 4, 15, 0, tzinfo=UTC)
WEDNESDAY_NIGHT = datetime(2026, 11, 4, 3, 0, tzinfo=UTC)
SCHOOL_PROFILE = ScheduleProfile.model_validate(
    {
        "version": 1,
        "timezone": "UTC",
        "busy": [{"days": ["wed"], "from": "09:30", "to": "12:00"}],
        "read_pause_seconds": [0, 0],
    }
)


class _Queue:
    def __init__(self) -> None:
        self.jobs: list[tuple[int, int]] = []

    async def enqueue(self, contact_id: int, last_message_id: int) -> None:
        self.jobs.append((contact_id, last_message_id))


class _WritingDuringTyping(ScriptedTransport):
    def __init__(self, rig: AutopilotRig, contact: Contact) -> None:
        super().__init__(clock=rig.clock)
        self._rig = rig
        self._contact = contact

    async def show_typing(self, tg_id: int, seconds: float) -> None:
        await super().show_typing(tg_id, seconds)
        await self._rig.say(self._contact, ago=0)


class _World:
    def __init__(self, rig: AutopilotRig, profile: ScheduleProfile = ALWAYS_FREE_PROFILE) -> None:
        self.rig = rig
        self.prig = ProactiveRig(rig)
        self.schedule = ScheduleService(InMemoryScheduleProfiles(profile), "UTC")
        self.queue = _Queue()
        self.client = SequenceLLMClient(llm_result(FOLLOW_UP_TEXT_ZQ))
        self.transport = ScriptedTransport(clock=rig.clock)

    def sweeper(self) -> ProactiveSweeper:
        return ProactiveSweeper(
            self.prig.proactive, self.prig.eligibility(), self.queue, clock=self.rig.clock
        )

    def sender(self, transport: ScriptedTransport | None = None) -> OutboxSender:
        used = transport if transport is not None else self.transport

        async def pause(seconds: float) -> None:
            used.steps.append(PAUSE_STEP)

        return OutboxSender(
            self.rig.outbox,
            self.rig.contacts,
            self.rig.messages,
            self.prig.sender_state,
            SendLimiter(self.rig.messages, FakeLimits()),
            self.schedule,
            used,
            RecordingNotifier(),
            SendGuard(self.rig.guard_switch),
            OUTBOX_NOTICES,
            clock=self.rig.clock,
            sleep=pause,
            rng=random.Random(11),
        )

    async def tick(self) -> list[ProactiveJobOutcome]:
        await self.sweeper().sweep()
        runner = self.prig.runner(self.client, schedule=self.schedule)
        outcomes = [(await runner.run(*job)).outcome for job in self.queue.jobs]
        self.queue.jobs.clear()
        return outcomes

    async def drain(self, transport: ScriptedTransport | None = None) -> None:
        sender = self.sender(transport)
        while await sender.send_next():
            continue

    async def rows(self) -> list[ScheduledMessage]:
        rows = await self.rig.outbox.list_rows(None, 100)
        return sorted(
            (row for row in rows if row.source is OutboxSource.PROACTIVE), key=lambda r: r.id
        )

    async def until_due(self) -> None:
        pending = [row for row in await self.rows() if row.state is OutboxState.SCHEDULED]
        self.rig.clock.now = max(self.rig.clock(), max(row.send_at for row in pending))


async def test_a_silent_contact_gets_one_follow_up_sent_in_the_human_order(
    rig: AutopilotRig, caplog: pytest.LogCaptureFixture
) -> None:
    rig.clock.now = WEDNESDAY_AFTERNOON
    world = _World(rig)
    contact, last = await world.prig.silent_contact(7)

    assert await world.tick() == [ProactiveJobOutcome.QUEUED]
    [row] = await world.rows()
    assert row.trigger_message_id == last.id
    assert in_proactive_window(row.due_at, ALWAYS_FREE_PROFILE)
    await world.until_due()
    await world.drain()

    assert world.transport.steps == HUMAN_ORDER
    assert world.transport.calls == [(contact.tg_id, FOLLOW_UP_TEXT_ZQ)]
    assert [r.state for r in await world.rows()] == [OutboxState.SENT]
    output = json_logs(caplog)
    for marker in (FOLLOW_UP_TEXT_ZQ, OUR_TEXT_ZQ):
        assert marker not in output


async def test_a_second_follow_up_waits_a_day_and_there_are_never_more_than_two(
    rig: AutopilotRig,
) -> None:
    rig.clock.now = WEDNESDAY_AFTERNOON
    world = _World(rig)
    await world.prig.silent_contact(7)
    await world.tick()
    await world.until_due()
    await world.drain()
    first_sent = rig.clock()

    for hours in (7, 12, 23):
        rig.clock.now = first_sent + timedelta(hours=hours)
        assert await world.tick() == [], hours
    rig.clock.now = first_sent + timedelta(hours=24, minutes=5)
    assert await world.tick() == [ProactiveJobOutcome.QUEUED]
    await world.until_due()
    await world.drain()
    for days in (2, 3, 5, 9):
        rig.clock.now = first_sent + timedelta(days=days)
        assert await world.tick() == [], days

    rows = await world.rows()
    assert [row.state for row in rows] == [OutboxState.SENT, OutboxState.SENT]
    assert rows[1].due_at - rows[0].due_at >= timedelta(hours=24)
    assert len(world.transport.calls) == 2


async def test_her_message_before_the_typing_cancels_the_follow_up(rig: AutopilotRig) -> None:
    rig.clock.now = WEDNESDAY_AFTERNOON
    world = _World(rig)
    contact, _ = await world.prig.silent_contact(7)
    await world.tick()
    await rig.say(contact, ago=0)
    await world.until_due()

    await world.drain()

    [row] = await world.rows()
    assert row.state is OutboxState.CANCELLED
    assert row.cancel_reason == CANCEL_REASON_SUPERSEDED
    assert world.transport.steps == []


async def test_her_message_after_the_start_does_not_cancel_the_follow_up(
    rig: AutopilotRig,
) -> None:
    rig.clock.now = WEDNESDAY_AFTERNOON
    world = _World(rig)
    contact, _ = await world.prig.silent_contact(7)
    await world.tick()
    await world.until_due()
    transport = _WritingDuringTyping(rig, contact)

    await world.drain(transport)

    [row] = await world.rows()
    assert row.state is OutboxState.SENT
    assert transport.steps == HUMAN_ORDER


async def test_at_night_and_in_class_hours_nothing_is_sent_until_the_window(
    rig: AutopilotRig,
) -> None:
    rig.clock.now = WEDNESDAY_NIGHT
    world = _World(rig, SCHOOL_PROFILE)
    contact, _ = await world.prig.silent_contact(7)

    assert await world.tick() == [ProactiveJobOutcome.QUEUED]
    [row] = await world.rows()
    assert row.send_at >= datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
    while rig.clock() < row.send_at - timedelta(minutes=1):
        await world.drain()
        rig.clock.advance(15 * 60)
    assert world.transport.steps == []
    await world.until_due()
    await world.drain()

    sent_at = rig.clock()
    assert world.transport.calls == [(contact.tg_id, FOLLOW_UP_TEXT_ZQ)]
    assert in_proactive_window(sent_at, SCHOOL_PROFILE)
    assert blocked(sent_at, SCHOOL_PROFILE) is None
