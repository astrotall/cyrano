from __future__ import annotations

import random
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest

from cyrano.bot.notifier import OUTBOX_NOTICES
from cyrano.db.repositories import SqlSenderStateRepository
from cyrano.domain.autopilot import ReplyJobResult
from cyrano.domain.enums import MessageDirection, OutboxState, ReplyJobOutcome, ReplyJobReason
from cyrano.domain.llm import LLMMessage, LLMResult
from cyrano.domain.models import Contact, Message
from cyrano.domain.outbox import ScheduledMessage
from cyrano.domain.schedule import ScheduleProfile
from cyrano.services.autopilot import AutopilotSweeper
from cyrano.services.humanization import ScheduleService
from cyrano.services.outbox_sender import OutboxSender
from cyrano.services.send_guard import SendGuard
from cyrano.services.send_limiter import SendLimiter
from tests.services.autopilot_fakes import RecordingQueue
from tests.services.autopilot_rig import AutopilotRig, SequenceLLMClient, json_logs, llm_result
from tests.services.outbox_fakes import (
    MARK_READ_STEP,
    SEND_TEXT_STEP,
    SHOW_TYPING_STEP,
    FakeLimits,
    InMemoryScheduleProfiles,
    RecordingNotifier,
    ScriptedTransport,
)

PAUSE_STEP = "pause"
HUMAN_ORDER = [MARK_READ_STEP, PAUSE_STEP, SHOW_TYPING_STEP, SEND_TEXT_STEP]
NIGHT_START = datetime(2026, 11, 4, 1, 0, tzinfo=UTC)
NIGHT_END = datetime(2026, 11, 4, 8, 0, tzinfo=UTC)
CLASS_START = datetime(2026, 11, 4, 14, 0, tzinfo=UTC)
CLASS_END = datetime(2026, 11, 4, 15, 30, tzinfo=UTC)
CLASS_LABEL = "лекция"
PROFILE = ScheduleProfile.model_validate(
    {
        "version": 1,
        "timezone": "UTC",
        "night": {
            "start": ["01:00", "01:00"],
            "end_weekday": ["08:00", "08:00"],
            "end_weekend": ["10:00", "10:00"],
        },
        "busy": [{"days": ["wed"], "from": "14:00", "to": "15:30", "label": CLASS_LABEL}],
        "read_pause_seconds": [0, 0],
    }
)
FAREWELL_ZQ = "farewell-marker-zq спокойной ночи, завтра напишу"
GOODBYE_ZQ = "goodbye-marker-zq мне пора, напишу позже"
MORNING_ZQ = "morning-marker-zq доброе утро"
HER_TEXT_ZQ = "her-text-marker-zq"
DEBOUNCE = 12


class _Watching(SequenceLLMClient):
    def __init__(self, *steps: LLMResult | Exception) -> None:
        super().__init__(*steps)
        self.users: list[str] = []

    async def complete(
        self,
        messages: Sequence[LLMMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResult:
        self.users.append(messages[1].content)
        return await super().complete(messages, temperature=temperature, max_tokens=max_tokens)


class _World:
    def __init__(self, rig: AutopilotRig) -> None:
        self.rig = rig
        rig.schedule = ScheduleService(InMemoryScheduleProfiles(PROFILE), "UTC")
        self.transport = ScriptedTransport(clock=rig.clock)

    async def her(self, contact: Contact, ago: float = 0) -> Message:
        return await self.rig.say(contact, ago=ago, text=HER_TEXT_ZQ)

    async def job(
        self, contact: Contact, trigger: Message, client: SequenceLLMClient
    ) -> ReplyJobResult:
        return await self.rig.runner(client, wrapup_enabled=True).run(contact.id, trigger.id)

    def sender(self) -> OutboxSender:
        transport = self.transport

        async def pause(seconds: float) -> None:
            transport.steps.append(PAUSE_STEP)

        return OutboxSender(
            self.rig.outbox,
            self.rig.contacts,
            self.rig.messages,
            SqlSenderStateRepository(self.rig.session_factory),
            SendLimiter(self.rig.messages, FakeLimits()),
            self.rig.schedule,
            transport,
            RecordingNotifier(),
            SendGuard(self.rig.guard_switch),
            OUTBOX_NOTICES,
            clock=self.rig.clock,
            sleep=pause,
            rng=random.Random(3),
        )

    async def scheduled(self) -> list[ScheduledMessage]:
        rows = await self.rig.outbox.list_rows(OutboxState.SCHEDULED, 10)
        return sorted(rows, key=lambda row: row.id)

    async def send_due(self) -> None:
        [row] = await self.scheduled()
        self.rig.clock.now = max(self.rig.clock(), row.send_at)
        sender = self.sender()
        while await sender.send_next():
            continue

    async def unanswered(self) -> list[tuple[int, int, int]]:
        queue = RecordingQueue()
        await AutopilotSweeper(
            self.rig.backlog,
            self.rig.state,
            queue,
            self.rig.switch,
            debounce_seconds=DEBOUNCE,
            clock=self.rig.clock,
        ).sweep()
        return queue.jobs


async def _lively(world: _World, start: datetime) -> tuple[Contact, Message]:
    world.rig.clock.now = start
    contact = await world.rig.contact()
    await world.her(contact, ago=240)
    await world.rig.say(contact, ago=200, direction=MessageDirection.OUTGOING)
    await world.her(contact, ago=120)
    await world.rig.say(contact, ago=90, direction=MessageDirection.OUTGOING)
    trigger = await world.her(contact, ago=5)
    return contact, trigger


@pytest.mark.parametrize(
    ("start", "window_start", "window_end", "reason", "farewell"),
    [
        (NIGHT_START - timedelta(minutes=15), NIGHT_START, NIGHT_END, "sleep", FAREWELL_ZQ),
        (CLASS_START - timedelta(minutes=15), CLASS_START, CLASS_END, CLASS_LABEL, GOODBYE_ZQ),
    ],
)
async def test_a_lively_conversation_is_wrapped_up_before_the_window(
    rig: AutopilotRig,
    caplog: pytest.LogCaptureFixture,
    start: datetime,
    window_start: datetime,
    window_end: datetime,
    reason: str,
    farewell: str,
) -> None:
    world = _World(rig)
    contact, trigger = await _lively(world, start)
    client = _Watching(llm_result(farewell), llm_result("SKIP"), llm_result(MORNING_ZQ))

    first = await world.job(contact, trigger, client)
    await world.send_due()

    sent_at = rig.clock()
    assert first.outcome is ReplyJobOutcome.QUEUED
    assert world.transport.steps == HUMAN_ORDER
    assert world.transport.calls == [(contact.tg_id, farewell)]
    assert sent_at < window_start - timedelta(seconds=30)
    assert f"The owner becomes unavailable in about 15 minutes ({reason})." in client.users[0]

    rig.clock.advance(20)
    goodbye = await world.her(contact)
    rig.clock.advance(DEBOUNCE * 2 + 1)
    skipped = await world.job(contact, goodbye, client)

    assert skipped.reason is ReplyJobReason.MODEL_SKIP
    assert await world.scheduled() == []
    assert (await rig.reload(contact)).reply_after == goodbye.created_at
    assert await world.unanswered() == []

    rig.clock.now = window_start + timedelta(minutes=20)
    inside = await world.her(contact)
    rig.clock.advance(DEBOUNCE)
    later = await world.job(contact, inside, client)
    steps_before = list(world.transport.steps)
    sender = world.sender()
    while rig.clock() < window_end:
        await sender.send_next()
        rig.clock.advance(15 * 60)

    assert later.outcome is ReplyJobOutcome.QUEUED
    assert "becomes unavailable" not in client.users[2]
    assert world.transport.steps == steps_before
    [row] = await world.scheduled()
    assert row.send_at > window_end
    await world.send_due()
    assert world.transport.steps == steps_before + HUMAN_ORDER
    assert world.transport.calls[-1] == (contact.tg_id, MORNING_ZQ)
    output = json_logs(caplog)
    for marker in (farewell, MORNING_ZQ, HER_TEXT_ZQ, CLASS_LABEL):
        assert marker not in output
