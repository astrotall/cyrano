from __future__ import annotations

import math
import random
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import pairwise

from pydantic import BaseModel

from cyrano.bot.notifier import OUTBOX_NOTICES
from cyrano.config import (
    DEFAULT_AUTOPILOT_DEBOUNCE_ONGOING_SECONDS,
    DEFAULT_AUTOPILOT_DEBOUNCE_SECONDS,
    DEFAULT_AUTOPILOT_MAX_BURST_WAIT_SECONDS,
    DEFAULT_REPLY_HISTORY_MESSAGES,
    DEFAULT_SEND_LIMIT_CONTACT_DAY,
    DEFAULT_SEND_LIMIT_CONTACT_HOUR,
    DEFAULT_SEND_LIMIT_CONTACT_MINUTE,
    DEFAULT_SEND_LIMIT_DAY,
    DEFAULT_SEND_LIMIT_HOUR,
    DEFAULT_SEND_LIMIT_MINUTE,
)
from cyrano.db.repositories import SqlSenderStateRepository
from cyrano.domain.autopilot import SWEEP_INTERVAL_SECONDS, reply_job_id
from cyrano.domain.enums import MessageDirection, OutboxState, ReplyJobOutcome
from cyrano.domain.llm import LLMMessage, LLMResult
from cyrano.domain.models import Contact, IncomingMessage, Message
from cyrano.domain.outbox import ScheduledMessage
from cyrano.domain.schedule import ScheduleProfile
from cyrano.services.autopilot import AutopilotScheduler, AutopilotSweeper
from cyrano.services.contact_events import InMemoryContactEvents
from cyrano.services.humanization import HumanizationPlanner, ScheduleService
from cyrano.services.intake import IntakeService
from cyrano.services.outbox_sender import SENDER_POLL_INTERVAL, OutboxSender
from cyrano.services.send_guard import SendGuard
from cyrano.services.send_limiter import SendLimiter
from tests.services.autopilot_fakes import RecordingQueue
from tests.services.autopilot_rig import AutopilotRig, llm_result
from tests.services.outbox_fakes import (
    FREE_TIMEZONE,
    InMemoryScheduleProfiles,
    RecordingNotifier,
    ScriptedTransport,
)

LIVELY_GAP_SECONDS = 10
LIVELY_MESSAGES = 18
QUIET_OFFSETS_SECONDS = (0, 20, 40)
TAIL_SECONDS = 180
TICK_SECONDS = 1.0
PLANNER_SEED = 43
SENDER_SEED = 44
FIRST_TG_MESSAGE_ID = 81_000
REPLY_TEXT = "an invented answer that keeps the chat going"
LIVELY_PROFILE = ScheduleProfile.model_validate(
    {
        "version": 1,
        "timezone": FREE_TIMEZONE,
        "night": {
            "start": ["00:00", "00:00"],
            "end_weekday": ["00:01", "00:01"],
            "end_weekend": ["00:01", "00:01"],
        },
    }
)


@dataclass(frozen=True)
class DefaultLimits:
    send_limit_minute: int = DEFAULT_SEND_LIMIT_MINUTE
    send_limit_hour: int = DEFAULT_SEND_LIMIT_HOUR
    send_limit_day: int = DEFAULT_SEND_LIMIT_DAY
    send_limit_contact_minute: int = DEFAULT_SEND_LIMIT_CONTACT_MINUTE
    send_limit_contact_hour: int = DEFAULT_SEND_LIMIT_CONTACT_HOUR
    send_limit_contact_day: int = DEFAULT_SEND_LIMIT_CONTACT_DAY


class _PromptRecorder:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def complete(
        self,
        messages: Sequence[LLMMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResult:
        self.prompts.append(messages[-1].content)
        return llm_result(REPLY_TEXT)

    async def complete_json[T: BaseModel](
        self,
        messages: Sequence[LLMMessage],
        schema: type[T],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> T:
        raise NotImplementedError


@dataclass
class _Generation:
    prompt: str
    row_id: int


@dataclass
class _Chat:
    rig: AutopilotRig
    contact: Contact
    offsets: Sequence[float]
    start: datetime = field(init=False)
    pending_jobs: dict[str, tuple[datetime, int]] = field(default_factory=dict)
    generations: list[_Generation] = field(default_factory=list)
    outcomes: list[ReplyJobOutcome] = field(default_factory=list)
    delivered: int = 0

    def __post_init__(self) -> None:
        rig = self.rig
        self.start = rig.clock()
        schedule = ScheduleService(InMemoryScheduleProfiles(LIVELY_PROFILE), FREE_TIMEZONE)
        planner = HumanizationPlanner(rig.messages, schedule, random.Random(PLANNER_SEED))
        self.queue = RecordingQueue()
        self.llm = _PromptRecorder()
        self.runner = rig.runner(self.llm, planner=planner)
        self.intake = IntakeService(
            rig.contacts,
            rig.messages,
            InMemoryContactEvents(),
            hand_over=rig.hand_over(),
            scheduler=AutopilotScheduler(
                rig.contacts,
                rig.state,
                self.queue,
                rig.switch,
                planner,
                debounce_seconds=DEFAULT_AUTOPILOT_DEBOUNCE_SECONDS,
                ongoing_debounce_seconds=DEFAULT_AUTOPILOT_DEBOUNCE_ONGOING_SECONDS,
                clock=rig.clock,
            ),
            clock=rig.clock,
        )
        self.sweeper = AutopilotSweeper(
            rig.backlog,
            rig.state,
            self.queue,
            rig.switch,
            debounce_seconds=DEFAULT_AUTOPILOT_DEBOUNCE_SECONDS,
            clock=rig.clock,
        )
        self.transport = _TypingTransport(self)
        self.sender = OutboxSender(
            rig.outbox,
            rig.contacts,
            rig.messages,
            SqlSenderStateRepository(rig.session_factory),
            SendLimiter(rig.messages, DefaultLimits()),
            schedule,
            self.transport,
            RecordingNotifier(),
            SendGuard(rig.switch),
            OUTBOX_NOTICES,
            clock=rig.clock,
            sleep=self.wait,
            rng=random.Random(SENDER_SEED),
        )
        self.typing_started: dict[int, datetime] = {}

    def elapsed(self) -> float:
        return (self.rig.clock() - self.start).total_seconds()

    async def wait(self, seconds: float) -> None:
        target = self.rig.clock() + timedelta(seconds=seconds)
        while self.rig.clock() < target:
            self.rig.clock.now = min(target, self.rig.clock() + timedelta(seconds=TICK_SECONDS))
            await self.world()

    async def world(self) -> None:
        while self.delivered < len(self.offsets) and self.offsets[self.delivered] <= self.elapsed():
            at = self.start + timedelta(seconds=self.offsets[self.delivered])
            await self.intake.handle(
                IncomingMessage(
                    tg_id=self.contact.tg_id,
                    tg_message_id=FIRST_TG_MESSAGE_ID + self.delivered,
                    text=f"invented lively line {self.delivered:02d}",
                    sent_at=at,
                )
            )
            self.delivered += 1
        for contact_id, message_id, defer in self.queue.jobs:
            job_id = reply_job_id(contact_id, message_id)
            if job_id not in self.pending_jobs:
                self.pending_jobs[job_id] = (
                    self.rig.clock() + timedelta(seconds=defer),
                    message_id,
                )
        self.queue.jobs.clear()
        for job_id, (due, message_id) in sorted(self.pending_jobs.items(), key=lambda j: j[1]):
            if due > self.rig.clock():
                continue
            del self.pending_jobs[job_id]
            prompts_before = len(self.llm.prompts)
            result = await self.runner.run(self.contact.id, message_id)
            self.outcomes.append(result.outcome)
            if len(self.llm.prompts) > prompts_before and result.row_id is not None:
                self.generations.append(_Generation(self.llm.prompts[-1], result.row_id))

    async def run(self, seconds: float) -> None:
        next_sweep = self.elapsed()
        next_poll = self.elapsed()
        await self.world()
        while self.elapsed() < seconds:
            if self.elapsed() >= next_sweep:
                await self.sweeper.sweep()
                next_sweep += SWEEP_INTERVAL_SECONDS
            if self.elapsed() >= next_poll:
                await self.sender.send_next()
                next_poll = self.elapsed() + SENDER_POLL_INTERVAL
            await self.wait(TICK_SECONDS)


class _TypingTransport(ScriptedTransport):
    def __init__(self, chat: _Chat) -> None:
        super().__init__(clock=chat.rig.clock)
        self._chat = chat
        self.typing_at: list[datetime] = []

    async def show_typing(self, tg_id: int, seconds: float) -> None:
        await super().show_typing(tg_id, seconds)
        self.typing_at.append(self._chat.rig.clock())
        await self._chat.wait(seconds)


async def _play(rig: AutopilotRig, offsets: Sequence[float], seconds: float) -> _Chat:
    chat = _Chat(rig, await rig.contact(), offsets)
    await chat.run(seconds)
    return chat


async def _history(chat: _Chat) -> list[Message]:
    return await chat.rig.messages.list_last(chat.contact.id, 1000)


async def _rows(chat: _Chat) -> list[ScheduledMessage]:
    return sorted(
        (row for row in await chat.rig.outbox.list_rows(None, 1000)),
        key=lambda row: row.id,
    )


def _lively_offsets() -> list[float]:
    return [float(LIVELY_GAP_SECONDS * number) for number in range(LIVELY_MESSAGES)]


REPLY_GAP_BOUND_SECONDS = (
    DEFAULT_AUTOPILOT_MAX_BURST_WAIT_SECONDS + LIVELY_PROFILE.typing.max_seconds
)
LIMIT_WINDOW_SECONDS = 60


def _replies_allowed_by_the_contact_minute_limit(seconds: float) -> int:
    return DEFAULT_SEND_LIMIT_CONTACT_MINUTE * math.ceil(seconds / LIMIT_WINDOW_SECONDS)


def _key(message: Message) -> tuple[datetime, int]:
    return message.created_at, message.id


def _sent_times(history: Sequence[Message]) -> list[datetime]:
    return [m.created_at for m in history if m.direction is MessageDirection.OUTGOING]


async def _assert_every_reply_saw_what_came_before_its_trigger(chat: _Chat) -> None:
    history = await _history(chat)
    rows = {row.id: row for row in await _rows(chat)}
    by_id = {message.id: message for message in history}
    for generation in chat.generations:
        trigger_id = rows[generation.row_id].trigger_message_id
        assert trigger_id is not None
        trigger = by_id[trigger_id]
        before = [m for m in history if _key(m) <= _key(trigger)]
        window = before[-DEFAULT_REPLY_HISTORY_MESSAGES:]
        last_reply = max(
            (i for i, m in enumerate(before) if m.direction is MessageDirection.OUTGOING),
            default=-1,
        )
        unanswered = before[last_reply + 1 :]
        assert all(m in window for m in unanswered)
        for message in window:
            if message.direction is MessageDirection.INCOMING:
                assert message.text in generation.prompt
        for message in history:
            if message.direction is MessageDirection.INCOMING and _key(message) > _key(trigger):
                assert message.text not in generation.prompt


async def _assert_one_row_per_trigger_and_no_started_row_cancelled(chat: _Chat) -> None:
    rows = await _rows(chat)
    triggers = [row.trigger_message_id for row in rows]
    sent = [row for row in rows if row.state is OutboxState.SENT]
    assert len(triggers) == len(set(triggers))
    assert {row.state for row in rows} <= {OutboxState.SENT, OutboxState.CANCELLED}
    assert len(chat.transport.typing_at) == len(sent) == len(chat.transport.calls)
    assert chat.transport.steps.count("mark_read") == len(sent)


async def test_a_contact_writing_every_ten_seconds_gets_replies_all_the_way_through(
    rig: AutopilotRig,
) -> None:
    offsets = _lively_offsets()
    duration = offsets[-1] + TAIL_SECONDS

    chat = await _play(rig, offsets, duration)

    history = await _history(chat)
    sent = _sent_times(history)
    incoming = [m for m in history if m.direction is MessageDirection.INCOMING]
    assert len(incoming) == LIVELY_MESSAGES
    marks = [chat.start, *sent]
    gaps = [(later - earlier).total_seconds() for earlier, later in pairwise(marks)]
    assert gaps
    assert max(gaps) <= REPLY_GAP_BOUND_SECONDS, (
        "a reply must follow within the maximum wait plus the longest typing"
    )
    for message in incoming:
        answer = next(at for at in sent if at > message.created_at)
        assert (answer - message.created_at).total_seconds() <= REPLY_GAP_BOUND_SECONDS
    upper_bound = min(len(incoming), _replies_allowed_by_the_contact_minute_limit(duration))
    assert len(sent) <= upper_bound, (
        "one row per trigger and at most SEND_LIMIT_CONTACT_MINUTE sends in every minute"
    )
    assert len([at for at in sent if at > incoming[-1].created_at]) == 1
    await _assert_one_row_per_trigger_and_no_started_row_cancelled(chat)
    await _assert_every_reply_saw_what_came_before_its_trigger(chat)


async def test_three_messages_in_a_minute_then_silence_end_with_one_reply_covering_them(
    rig: AutopilotRig,
) -> None:
    offsets = list(QUIET_OFFSETS_SECONDS)

    chat = await _play(rig, offsets, offsets[-1] + TAIL_SECONDS)

    history = await _history(chat)
    sent = _sent_times(history)
    incoming = [m for m in history if m.direction is MessageDirection.INCOMING]
    rows = await _rows(chat)
    [sent_row] = [row for row in rows if row.state is OutboxState.SENT]
    assert sent_row.trigger_message_id == incoming[-1].id
    assert len(sent) == 1
    assert sent[0] > incoming[-1].created_at
    [covering] = [g for g in chat.generations if g.row_id == sent_row.id]
    for message in incoming:
        assert message.text in covering.prompt
    await _assert_one_row_per_trigger_and_no_started_row_cancelled(chat)
    await _assert_every_reply_saw_what_came_before_its_trigger(chat)
