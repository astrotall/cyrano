from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

from cyrano.bot.notifier import OUTBOX_NOTICES
from cyrano.db.repositories import SqlSenderStateRepository
from cyrano.domain.enums import (
    ContactStatus,
    ManualMessageOutcome,
    MessageDirection,
    OutboxState,
    ReplyJobOutcome,
)
from cyrano.domain.models import Contact, IncomingMessage, OutgoingMessage
from cyrano.domain.outbox import CANCEL_REASON_MANUAL_MESSAGE, CANCEL_REASON_SUPERSEDED
from cyrano.services.autopilot import AutopilotScheduler, AutopilotSweeper
from cyrano.services.contact_events import InMemoryContactEvents
from cyrano.services.humanization import HumanizationPlanner
from cyrano.services.intake import IntakeService
from cyrano.services.manual_intervention import ManualMessageDetector
from cyrano.services.outbox_sender import OutboxSender
from cyrano.services.send_guard import SendGuard
from cyrano.services.send_limiter import SendLimiter
from tests.services.autopilot_fakes import RecordingQueue
from tests.services.autopilot_rig import (
    REPLY_TEXT_ZQ,
    AutopilotRig,
    SequenceLLMClient,
    llm_result,
)
from tests.services.outbox_fakes import (
    FakeLimits,
    RecordingManualPauseNotifier,
    RecordingNotifier,
    ScriptedTransport,
    free_schedule,
)

DEBOUNCE = 12
PAUSE_STEP = "pause"
SEND_DELAY_SECONDS = 600
OWNER_TG_MESSAGE_ID = 880011
EIGHT_HOURS_SECONDS = 8 * 3600
MEETING_TEXT = "давай встретимся завтра вечером"
BURST_OFFSETS_SECONDS = (0, 40, 80)
BURST_PLANNER_SEED = 184
TICK_SECONDS = 2
SIMULATION_SECONDS = 260
BURST_FIRST_TG_MESSAGE_ID = 600


class _Pipeline:
    def __init__(self, rig: AutopilotRig) -> None:
        self.rig = rig
        rig.clock.now = datetime.now(UTC)
        self.queue = RecordingQueue()
        self.transport = ScriptedTransport(clock=rig.clock)
        self.scheduler = AutopilotScheduler(
            rig.contacts,
            rig.state,
            self.queue,
            rig.switch,
            debounce_seconds=DEBOUNCE,
            clock=rig.clock,
        )
        self.intake = IntakeService(
            rig.contacts,
            rig.messages,
            InMemoryContactEvents(),
            hand_over=rig.hand_over(),
            scheduler=self.scheduler,
            clock=rig.clock,
        )
        self.client = SequenceLLMClient(llm_result())

    async def receive(self, contact: Contact, text: str, number: int, ago: float = 0) -> None:
        await self.intake.handle(
            IncomingMessage(
                tg_id=contact.tg_id,
                tg_message_id=number,
                text=text,
                sent_at=self.rig.clock() - timedelta(seconds=ago),
            )
        )

    def sweeper(self) -> AutopilotSweeper:
        return AutopilotSweeper(
            self.rig.backlog,
            self.rig.state,
            self.queue,
            self.rig.switch,
            debounce_seconds=DEBOUNCE,
            clock=self.rig.clock,
        )

    async def run_jobs(self) -> list[ReplyJobOutcome]:
        runner = self.rig.runner(self.client)
        outcomes = []
        for contact_id, message_id, _ in self.queue.jobs:
            outcomes.append((await runner.run(contact_id, message_id)).outcome)
        return outcomes

    async def pause(self, seconds: float) -> None:
        self.transport.steps.append(PAUSE_STEP)

    def sender(self) -> OutboxSender:
        return OutboxSender(
            self.rig.outbox,
            self.rig.contacts,
            self.rig.messages,
            SqlSenderStateRepository(self.rig.session_factory),
            SendLimiter(self.rig.messages, FakeLimits()),
            free_schedule(),
            self.transport,
            RecordingNotifier(),
            SendGuard(self.rig.switch),
            OUTBOX_NOTICES,
            clock=self.rig.clock,
            sleep=self.pause,
            rng=random.Random(5),
        )


async def test_an_incoming_message_becomes_one_reply_sent_in_the_human_order(
    rig: AutopilotRig,
) -> None:
    pipeline = _Pipeline(rig)
    contact = await rig.contact()

    await pipeline.receive(contact, "how was the invented trip", 501)
    outcomes = await pipeline.run_jobs()
    [row] = await rig.outbox.list_rows(None, 10)
    rig.clock.advance(SEND_DELAY_SECONDS)
    await pipeline.sender().send_next()

    assert [job[2] for job in pipeline.queue.jobs] == [DEBOUNCE]
    assert outcomes == [ReplyJobOutcome.QUEUED]
    assert pipeline.transport.steps == ["mark_read", PAUSE_STEP, "show_typing", "send_text"]
    assert pipeline.transport.calls == [(contact.tg_id, REPLY_TEXT_ZQ)]
    sent = await rig.outbox.get(row.id)
    assert sent is not None
    assert sent.state is OutboxState.SENT


async def test_a_manual_message_of_the_owner_in_between_cancels_the_reply(
    rig: AutopilotRig,
) -> None:
    pipeline = _Pipeline(rig)
    contact = await rig.contact()
    await pipeline.receive(contact, "how was the invented trip", 502)
    await pipeline.run_jobs()
    detector = ManualMessageDetector(
        rig.contacts, rig.messages, rig.outbox, RecordingManualPauseNotifier(), clock=rig.clock
    )

    outcome = await detector.handle(
        OutgoingMessage(
            tg_id=contact.tg_id,
            tg_message_id=OWNER_TG_MESSAGE_ID,
            text="the owner writes by hand",
            sent_at=rig.clock(),
        )
    )
    rig.clock.advance(SEND_DELAY_SECONDS)
    await pipeline.sender().send_next()

    assert outcome is ManualMessageOutcome.PAUSED
    [row] = await rig.outbox.list_rows(None, 10)
    assert (row.state, row.cancel_reason) == (OutboxState.CANCELLED, CANCEL_REASON_MANUAL_MESSAGE)
    assert pipeline.transport.steps == []


async def test_a_stop_trigger_text_produces_no_job_no_row_and_no_send(rig: AutopilotRig) -> None:
    pipeline = _Pipeline(rig)
    contact = await rig.contact()

    await pipeline.receive(contact, MEETING_TEXT, 503)
    await pipeline.run_jobs()
    rig.clock.advance(SEND_DELAY_SECONDS)
    await pipeline.sender().send_next()

    assert pipeline.queue.jobs == []
    assert pipeline.client.calls == 0
    assert await rig.outbox.list_rows(None, 10) == []
    assert pipeline.transport.steps == []
    assert (await rig.reload(contact)).status is ContactStatus.PAUSED
    assert len(rig.hand_over_notifier.notices) == 1


async def test_a_message_that_arrived_eight_hours_ago_is_not_answered_after_a_catch_up(
    rig: AutopilotRig,
) -> None:
    pipeline = _Pipeline(rig)
    contact = await rig.contact()

    await pipeline.receive(contact, "written long ago", 504, ago=EIGHT_HOURS_SECONDS)
    outcomes = await pipeline.run_jobs()
    jobs_before_sweep = list(pipeline.queue.jobs)
    swept = await pipeline.sweeper().sweep()

    [stored] = await rig.messages.list_last(contact.id, 10)
    assert stored.created_at == rig.clock() - timedelta(seconds=EIGHT_HOURS_SECONDS)
    assert outcomes == [ReplyJobOutcome.SKIPPED]
    assert swept == 0
    assert pipeline.queue.jobs == jobs_before_sweep
    assert pipeline.client.calls == 0
    assert await rig.outbox.list_rows(None, 10) == []


async def test_a_fresh_message_after_an_old_one_is_still_answered(rig: AutopilotRig) -> None:
    pipeline = _Pipeline(rig)
    contact = await rig.contact()

    await pipeline.receive(contact, "written long ago", 505, ago=EIGHT_HOURS_SECONDS)
    await pipeline.receive(contact, "written just now", 506, ago=30)
    outcomes = await pipeline.run_jobs()

    assert outcomes == [ReplyJobOutcome.SKIPPED, ReplyJobOutcome.QUEUED]
    assert len(await rig.outbox.list_rows(None, 10)) == 1


async def test_three_messages_within_a_minute_get_one_reply_for_the_last_one(
    rig: AutopilotRig,
) -> None:
    pipeline = _Pipeline(rig)
    contact = await rig.contact()
    planner = HumanizationPlanner(rig.messages, rig.schedule, random.Random(BURST_PLANNER_SEED))
    runner = rig.runner(pipeline.client, planner=planner)
    sender = pipeline.sender()
    start = rig.clock()
    waiting: list[tuple[datetime, int, int]] = []
    queued_before = 0
    outcomes: list[ReplyJobOutcome] = []

    for second in range(0, SIMULATION_SECONDS + 1, TICK_SECONDS):
        rig.clock.now = start + timedelta(seconds=second)
        for number, offset in enumerate(BURST_OFFSETS_SECONDS):
            if offset == second:
                await pipeline.receive(
                    contact, f"invented burst part {number}", BURST_FIRST_TG_MESSAGE_ID + number
                )
        for contact_id, message_id, delay in pipeline.queue.jobs[queued_before:]:
            waiting.append((rig.clock() + timedelta(seconds=delay), contact_id, message_id))
        queued_before = len(pipeline.queue.jobs)
        for job in [job for job in waiting if job[0] <= rig.clock()]:
            waiting.remove(job)
            outcomes.append((await runner.run(job[1], job[2])).outcome)
        await sender.send_next()

    rows = sorted(await rig.outbox.list_rows(None, 10), key=lambda row: row.id)
    history = await rig.messages.list_last(contact.id, 10)
    incoming = [message for message in history if message.direction is MessageDirection.INCOMING]
    assert outcomes == [ReplyJobOutcome.QUEUED] * len(BURST_OFFSETS_SECONDS)
    assert [row.trigger_message_id for row in rows] == [message.id for message in incoming]
    assert [(row.state, row.cancel_reason) for row in rows] == [
        (OutboxState.CANCELLED, CANCEL_REASON_SUPERSEDED),
        (OutboxState.CANCELLED, CANCEL_REASON_SUPERSEDED),
        (OutboxState.SENT, None),
    ]
    assert rows[0].send_at > incoming[1].created_at
    assert rows[1].send_at > incoming[2].created_at
    assert pipeline.transport.steps == ["mark_read", PAUSE_STEP, "show_typing", "send_text"]
    assert pipeline.transport.calls == [(contact.tg_id, REPLY_TEXT_ZQ)]
    assert history[-1].direction is MessageDirection.OUTGOING
    assert [message.direction for message in history[:-1]] == [MessageDirection.INCOMING] * 3
