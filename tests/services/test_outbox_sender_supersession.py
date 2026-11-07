from __future__ import annotations

import logging
import random
from collections.abc import Awaitable, Callable

import pytest

from cyrano.bot.notifier import OUTBOX_NOTICES
from cyrano.db.repositories import SqlSenderStateRepository
from cyrano.domain.enums import MessageDirection, OutboxSource, OutboxState
from cyrano.domain.models import Contact, Message
from cyrano.domain.outbox import (
    CANCEL_REASON_MANUAL_MESSAGE,
    CANCEL_REASON_OWNER,
    CANCEL_REASON_SEND_REJECTED,
    CANCEL_REASON_STALE,
    CANCEL_REASON_STOP_TRIGGER,
    CANCEL_REASON_SUPERSEDED,
    ScheduledMessage,
)
from cyrano.services.outbox_sender import STEP_LOG_MESSAGE, OutboxSender
from cyrano.services.send_guard import SendGuard
from cyrano.services.send_limiter import SendLimiter
from tests.services.autopilot_rig import REPLY_TEXT_ZQ, AutopilotRig
from tests.services.outbox_fakes import (
    FakeLimits,
    RecordingNotifier,
    ScriptedTransport,
    free_schedule,
)

READ_STEP = "mark_read"
TYPING_STEP = "show_typing"
SEND_STEP = "send_text"
PAUSE_STEP = "pause"
SENDER_LOGGER = "cyrano.services.outbox_sender"
ARRIVAL_TEXT = "invented late arrival"
OLDER_AGO = 300
NEWER_AGO = 60
STALE_ARRIVAL_AGO = 3600


class _ArrivalDuringTyping(ScriptedTransport):
    def __init__(self, rig: AutopilotRig, arrive: Callable[[], Awaitable[object]]) -> None:
        super().__init__(clock=rig.clock)
        self._arrive = arrive

    async def show_typing(self, tg_id: int, seconds: float) -> None:
        await super().show_typing(tg_id, seconds)
        await self._arrive()


def _sender(
    rig: AutopilotRig, transport: ScriptedTransport, notifier: RecordingNotifier | None = None
) -> OutboxSender:
    async def pause(seconds: float) -> None:
        transport.steps.append(PAUSE_STEP)

    return OutboxSender(
        rig.outbox,
        rig.contacts,
        rig.messages,
        SqlSenderStateRepository(rig.session_factory),
        SendLimiter(rig.messages, FakeLimits()),
        free_schedule(),
        transport,
        notifier if notifier is not None else RecordingNotifier(),
        SendGuard(rig.switch),
        OUTBOX_NOTICES,
        clock=rig.clock,
        sleep=pause,
        rng=random.Random(5),
    )


async def _row(
    rig: AutopilotRig,
    contact: Contact,
    trigger: Message | None,
    source: OutboxSource = OutboxSource.AUTOPILOT,
) -> ScheduledMessage:
    return await rig.outbox.enqueue(
        contact.id,
        REPLY_TEXT_ZQ,
        rig.clock(),
        source,
        None if trigger is None else trigger.id,
    )


async def _stored(rig: AutopilotRig, row: ScheduledMessage) -> ScheduledMessage:
    stored = await rig.outbox.get(row.id)
    assert stored is not None
    return stored


def _step_outcomes(caplog: pytest.LogCaptureFixture) -> list[tuple[object, object]]:
    return [
        (record.__dict__["step"], record.__dict__["outcome"])
        for record in caplog.records
        if record.name == SENDER_LOGGER and record.getMessage() == STEP_LOG_MESSAGE
    ]


def test_the_superseded_reason_is_its_own_stored_word() -> None:
    others = {
        CANCEL_REASON_MANUAL_MESSAGE,
        CANCEL_REASON_OWNER,
        CANCEL_REASON_SEND_REJECTED,
        CANCEL_REASON_STALE,
        CANCEL_REASON_STOP_TRIGGER,
    }

    assert CANCEL_REASON_SUPERSEDED == "superseded"
    assert CANCEL_REASON_SUPERSEDED not in others


async def test_a_newer_incoming_message_cancels_the_row_before_any_pre_send_step(
    rig: AutopilotRig,
) -> None:
    contact = await rig.contact()
    trigger = await rig.say(contact, ago=OLDER_AGO)
    row = await _row(rig, contact, trigger)
    await rig.say(contact, ago=NEWER_AGO, text=ARRIVAL_TEXT)
    transport = ScriptedTransport(clock=rig.clock)
    notifier = RecordingNotifier()

    await _sender(rig, transport, notifier).send_next()

    stored = await _stored(rig, row)
    assert (stored.state, stored.cancel_reason) == (OutboxState.CANCELLED, CANCEL_REASON_SUPERSEDED)
    assert stored.error_class is None
    assert transport.steps == []
    assert transport.calls == []
    assert notifier.texts == []


async def test_a_message_that_arrives_during_typing_cancels_the_send(
    rig: AutopilotRig, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=SENDER_LOGGER)
    contact = await rig.contact()
    trigger = await rig.say(contact, ago=OLDER_AGO)
    row = await _row(rig, contact, trigger)

    async def arrive() -> None:
        await rig.say(contact, ago=0, text=ARRIVAL_TEXT)

    transport = _ArrivalDuringTyping(rig, arrive)
    notifier = RecordingNotifier()

    await _sender(rig, transport, notifier).send_next()

    stored = await _stored(rig, row)
    assert (stored.state, stored.cancel_reason) == (OutboxState.CANCELLED, CANCEL_REASON_SUPERSEDED)
    assert transport.steps == [READ_STEP, PAUSE_STEP, TYPING_STEP]
    assert transport.calls == []
    assert notifier.texts == []
    assert _step_outcomes(caplog)[-1] == ("send", "skipped")


async def test_an_outgoing_message_newer_than_the_trigger_cancels_an_autopilot_row(
    rig: AutopilotRig,
) -> None:
    contact = await rig.contact()
    trigger = await rig.say(contact, ago=OLDER_AGO)
    row = await _row(rig, contact, trigger)
    await rig.say(contact, ago=NEWER_AGO, direction=MessageDirection.OUTGOING)
    transport = ScriptedTransport(clock=rig.clock)

    await _sender(rig, transport).send_next()

    stored = await _stored(rig, row)
    assert (stored.state, stored.cancel_reason) == (OutboxState.CANCELLED, CANCEL_REASON_SUPERSEDED)
    assert transport.steps == []


async def test_a_manual_test_row_is_never_superseded(rig: AutopilotRig) -> None:
    contact = await rig.contact()
    trigger = await rig.say(contact, ago=OLDER_AGO)
    row = await _row(rig, contact, trigger, OutboxSource.MANUAL_TEST)
    await rig.say(contact, ago=NEWER_AGO, text=ARRIVAL_TEXT)
    transport = ScriptedTransport(clock=rig.clock)

    await _sender(rig, transport).send_next()

    assert (await _stored(rig, row)).state is OutboxState.SENT
    assert transport.calls == [(contact.tg_id, REPLY_TEXT_ZQ)]


async def test_a_row_without_a_trigger_message_is_never_superseded(rig: AutopilotRig) -> None:
    contact = await rig.contact()
    await rig.say(contact, ago=OLDER_AGO)
    row = await _row(rig, contact, None)
    await rig.say(contact, ago=NEWER_AGO, text=ARRIVAL_TEXT)
    transport = ScriptedTransport(clock=rig.clock)

    await _sender(rig, transport).send_next()

    assert (await _stored(rig, row)).state is OutboxState.SENT
    assert transport.calls == [(contact.tg_id, REPLY_TEXT_ZQ)]


async def test_a_sent_row_is_not_touched_by_a_later_message(rig: AutopilotRig) -> None:
    contact = await rig.contact()
    trigger = await rig.say(contact, ago=OLDER_AGO)
    row = await _row(rig, contact, trigger)
    transport = ScriptedTransport(clock=rig.clock)
    sender = _sender(rig, transport)
    await sender.send_next()

    await rig.say(contact, ago=0, text=ARRIVAL_TEXT)
    claimed = await sender.send_next()

    stored = await _stored(rig, row)
    assert claimed is False
    assert (stored.state, stored.cancel_reason) == (OutboxState.SENT, None)
    assert len(transport.calls) == 1


async def test_a_row_for_the_latest_message_is_sent_with_every_step(rig: AutopilotRig) -> None:
    contact = await rig.contact()
    await rig.say(contact, ago=OLDER_AGO)
    trigger = await rig.say(contact, ago=NEWER_AGO, text=ARRIVAL_TEXT)
    row = await _row(rig, contact, trigger)
    transport = ScriptedTransport(clock=rig.clock)

    await _sender(rig, transport).send_next()

    assert (await _stored(rig, row)).state is OutboxState.SENT
    assert transport.steps == [READ_STEP, PAUSE_STEP, TYPING_STEP, SEND_STEP]


async def test_an_older_message_stored_later_does_not_supersede_a_newer_trigger(
    rig: AutopilotRig,
) -> None:
    contact = await rig.contact()
    trigger = await rig.say(contact, ago=NEWER_AGO)
    caught_up = await rig.say(contact, ago=STALE_ARRIVAL_AGO, text=ARRIVAL_TEXT)
    row = await _row(rig, contact, trigger)
    transport = ScriptedTransport(clock=rig.clock)

    await _sender(rig, transport).send_next()

    assert caught_up.id > trigger.id
    assert caught_up.created_at < trigger.created_at
    assert (await _stored(rig, row)).state is OutboxState.SENT


async def test_a_message_stored_earlier_but_written_later_supersedes_a_trigger_stored_later(
    rig: AutopilotRig,
) -> None:
    contact = await rig.contact()
    written_later = await rig.say(contact, ago=NEWER_AGO, text=ARRIVAL_TEXT)
    trigger = await rig.say(contact, ago=STALE_ARRIVAL_AGO)
    row = await _row(rig, contact, trigger)
    transport = ScriptedTransport(clock=rig.clock)

    await _sender(rig, transport).send_next()

    assert trigger.id > written_later.id
    stored = await _stored(rig, row)
    assert (stored.state, stored.cancel_reason) == (OutboxState.CANCELLED, CANCEL_REASON_SUPERSEDED)
    assert transport.steps == []


async def test_a_message_with_the_same_time_and_a_larger_id_supersedes_the_trigger(
    rig: AutopilotRig,
) -> None:
    contact = await rig.contact()
    trigger = await rig.say(contact, ago=NEWER_AGO)
    same_time = await rig.say(contact, ago=NEWER_AGO, text=ARRIVAL_TEXT)
    row = await _row(rig, contact, trigger)
    transport = ScriptedTransport(clock=rig.clock)

    await _sender(rig, transport).send_next()

    assert same_time.created_at == trigger.created_at
    assert same_time.id > trigger.id
    stored = await _stored(rig, row)
    assert (stored.state, stored.cancel_reason) == (OutboxState.CANCELLED, CANCEL_REASON_SUPERSEDED)


async def test_a_message_with_the_same_time_and_a_smaller_id_does_not_supersede_the_trigger(
    rig: AutopilotRig,
) -> None:
    contact = await rig.contact()
    same_time = await rig.say(contact, ago=NEWER_AGO, text=ARRIVAL_TEXT)
    trigger = await rig.say(contact, ago=NEWER_AGO)
    row = await _row(rig, contact, trigger)
    transport = ScriptedTransport(clock=rig.clock)

    await _sender(rig, transport).send_next()

    assert same_time.created_at == trigger.created_at
    assert same_time.id < trigger.id
    assert (await _stored(rig, row)).state is OutboxState.SENT


async def test_a_message_of_another_contact_does_not_supersede_a_row(rig: AutopilotRig) -> None:
    contact = await rig.contact()
    other = await rig.contact()
    trigger = await rig.say(contact, ago=OLDER_AGO)
    row = await _row(rig, contact, trigger)
    await rig.say(other, ago=NEWER_AGO, text=ARRIVAL_TEXT)
    transport = ScriptedTransport(clock=rig.clock)

    await _sender(rig, transport).send_next()

    assert (await _stored(rig, row)).state is OutboxState.SENT
