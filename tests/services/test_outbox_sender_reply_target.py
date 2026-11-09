from __future__ import annotations

import logging
import random
from datetime import timedelta

import pytest

from cyrano.bot.notifier import OUTBOX_NOTICES
from cyrano.db.repositories import SqlSenderStateRepository
from cyrano.domain.enums import MessageDirection, OutboxSource, OutboxState
from cyrano.domain.errors import SendFloodWait, SendReplyTargetGone, SendUncertain
from cyrano.domain.models import Contact, Message, MessageCreate
from cyrano.domain.outbox import ERROR_UNCERTAIN, ScheduledMessage
from cyrano.services.outbox_sender import OutboxSender
from cyrano.services.send_guard import SendGuard
from cyrano.services.send_limiter import SendLimiter
from tests.privacy import assert_absent
from tests.services.autopilot_rig import AutopilotRig, SequenceLLMClient, json_logs, llm_result
from tests.services.outbox_fakes import (
    FakeLimits,
    RecordingNotifier,
    ScriptedTransport,
    free_schedule,
)
from tests.services.quote_fakes import FixedPlanner

SEED = 13
READ = "mark_read"
PAUSE = "pause"
TYPING = "show_typing"
SEND = "send_text"
SENDER_LOGGER = "cyrano.services.outbox_sender"
FALLBACK_LOG = "outbox row resent without its reply target"
GONE = "MessageIdInvalidError"
TARGET_TG_MESSAGE_ID = 652000000017
EARLIER_TG_MESSAGE_ID = 652000000021
LATER_TG_MESSAGE_ID = 652000000022
PART_ONE_ZQ = "sender-quote-part-one-zq sure"
PART_TWO_ZQ = "sender-quote-part-two-zq see you"
EARLIER_ZQ = "sender-quote-earlier-zq invented question"
LATER_ZQ = "sender-quote-later-zq invented follow-up"
LATE_AGO = 900


def _sender(
    rig: AutopilotRig, transport: ScriptedTransport, notifier: RecordingNotifier
) -> OutboxSender:
    async def pause(seconds: float) -> None:
        transport.steps.append(PAUSE)
        rig.clock.advance(seconds)

    return OutboxSender(
        rig.outbox,
        rig.contacts,
        rig.messages,
        SqlSenderStateRepository(rig.session_factory),
        SendLimiter(rig.messages, FakeLimits()),
        free_schedule(),
        transport,
        notifier,
        SendGuard(rig.switch),
        OUTBOX_NOTICES,
        clock=rig.clock,
        sleep=pause,
        rng=random.Random(SEED),
    )


async def _row(
    rig: AutopilotRig, *, target: int | None = TARGET_TG_MESSAGE_ID
) -> tuple[Contact, ScheduledMessage]:
    contact = await rig.contact()
    trigger = await rig.say(contact, ago=30)
    [row] = await rig.outbox.enqueue_sequence(
        contact.id, [PART_ONE_ZQ], rig.clock(), OutboxSource.AUTOPILOT, trigger.id, [target]
    )
    return contact, row


async def _stored(rig: AutopilotRig, row: ScheduledMessage) -> ScheduledMessage:
    stored = await rig.outbox.get(row.id)
    assert stored is not None
    return stored


async def _outgoing(rig: AutopilotRig, contact: Contact) -> list[Message]:
    history = await rig.messages.list_last(contact.id, 50)
    return [message for message in history if message.direction is MessageDirection.OUTGOING]


async def _rows(rig: AutopilotRig, contact: Contact) -> list[ScheduledMessage]:
    rows = await rig.outbox.list_rows(None, 50)
    return sorted((row for row in rows if row.contact_id == contact.id), key=lambda row: row.id)


async def test_the_target_reaches_the_transport(rig: AutopilotRig) -> None:
    contact, row = await _row(rig)
    transport = ScriptedTransport(clock=rig.clock)

    assert await _sender(rig, transport, RecordingNotifier()).send_next() is True

    assert transport.reply_tos == [TARGET_TG_MESSAGE_ID]
    assert transport.calls == [(contact.tg_id, PART_ONE_ZQ)]
    assert (await _stored(rig, row)).state is OutboxState.SENT


async def test_a_row_without_a_target_is_sent_as_before(rig: AutopilotRig) -> None:
    _, row = await _row(rig, target=None)
    transport = ScriptedTransport(clock=rig.clock)

    await _sender(rig, transport, RecordingNotifier()).send_next()

    assert transport.reply_tos == [None]
    assert transport.steps == [READ, PAUSE, TYPING, SEND]
    assert (await _stored(rig, row)).state is OutboxState.SENT


async def test_a_gone_target_is_resent_once_plain_on_the_same_row(
    rig: AutopilotRig, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    contact, row = await _row(rig)
    transport = ScriptedTransport([SendReplyTargetGone(GONE)], clock=rig.clock)
    notifier = RecordingNotifier()

    assert await _sender(rig, transport, notifier).send_next() is True

    assert transport.reply_tos == [TARGET_TG_MESSAGE_ID, None]
    assert transport.steps == [READ, PAUSE, TYPING, SEND, SEND]
    assert transport.calls == [(contact.tg_id, PART_ONE_ZQ)] * 2
    stored = await _stored(rig, row)
    assert (stored.state, stored.error_class) == (OutboxState.SENT, None)
    assert [item.id for item in await _rows(rig, contact)] == [row.id]
    assert [message.text for message in await _outgoing(rig, contact)] == [PART_ONE_ZQ]
    assert notifier.texts == []
    [record] = [record for record in caplog.records if record.getMessage() == FALLBACK_LOG]
    assert (record.__dict__["row_id"], record.__dict__["fallbacks"]) == (row.id, 1)
    logs = json_logs(caplog)
    assert_absent(TARGET_TG_MESSAGE_ID, logs)
    assert_absent(PART_ONE_ZQ, logs)
    assert_absent(contact.tg_id, logs)


async def test_a_second_failure_after_the_plain_resend_follows_the_uncertain_policy(
    rig: AutopilotRig,
) -> None:
    contact, row = await _row(rig)
    transport = ScriptedTransport(
        [SendReplyTargetGone(GONE), SendUncertain("FakeError")], clock=rig.clock
    )
    notifier = RecordingNotifier()

    await _sender(rig, transport, notifier).send_next()

    assert transport.reply_tos == [TARGET_TG_MESSAGE_ID, None]
    stored = await _stored(rig, row)
    assert (stored.state, stored.error_class) == (OutboxState.FAILED, ERROR_UNCERTAIN)
    assert len(notifier.texts) == 1
    assert await _outgoing(rig, contact) == []


async def test_a_flood_wait_on_the_plain_resend_reschedules_the_row_as_usual(
    rig: AutopilotRig,
) -> None:
    _, row = await _row(rig)
    transport = ScriptedTransport(
        [SendReplyTargetGone(GONE), SendFloodWait("FloodWaitError", 30)], clock=rig.clock
    )

    await _sender(rig, transport, RecordingNotifier()).send_next()

    stored = await _stored(rig, row)
    assert (stored.state, stored.error_class) == (OutboxState.SCHEDULED, "FloodWaitError")
    assert stored.reply_to_tg_message_id == TARGET_TG_MESSAGE_ID


async def test_the_plain_resend_is_never_repeated(rig: AutopilotRig) -> None:
    _, row = await _row(rig)
    transport = ScriptedTransport(
        [SendReplyTargetGone(GONE), SendReplyTargetGone(GONE), SendReplyTargetGone(GONE)],
        clock=rig.clock,
    )

    await _sender(rig, transport, RecordingNotifier()).send_next()

    assert transport.reply_tos == [TARGET_TG_MESSAGE_ID, None]
    assert (await _stored(rig, row)).state is OutboxState.FAILED


async def test_a_gone_target_error_on_a_plain_row_is_not_retried(rig: AutopilotRig) -> None:
    _, row = await _row(rig, target=None)
    transport = ScriptedTransport([SendReplyTargetGone(GONE)], clock=rig.clock)

    await _sender(rig, transport, RecordingNotifier()).send_next()

    assert transport.reply_tos == [None]
    stored = await _stored(rig, row)
    assert (stored.state, stored.error_class) == (OutboxState.FAILED, ERROR_UNCERTAIN)


async def _incoming(
    rig: AutopilotRig, contact: Contact, text: str, tg_message_id: int, ago: float
) -> Message:
    return await rig.messages.append(
        MessageCreate(
            contact_id=contact.id,
            direction=MessageDirection.INCOMING,
            text=text,
            tg_message_id=tg_message_id,
            created_at=rig.clock() - timedelta(seconds=ago),
        )
    )


async def _send_all(rig: AutopilotRig, sender: OutboxSender, contact: Contact) -> None:
    for _ in range(4):
        for row in await _rows(rig, contact):
            if row.state is OutboxState.SCHEDULED and row.send_at > rig.clock():
                rig.clock.now = row.send_at
        await sender.send_next()


async def test_end_to_end_a_model_marker_is_sent_as_a_telegram_reply_on_one_part_only(
    rig: AutopilotRig, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    contact = await rig.contact()
    await rig.say(contact, ago=900, direction=MessageDirection.OUTGOING, text="invented hello")
    earlier = await _incoming(rig, contact, EARLIER_ZQ, EARLIER_TG_MESSAGE_ID, 50)
    later = await _incoming(rig, contact, LATER_ZQ, LATER_TG_MESSAGE_ID, 40)
    client = SequenceLLMClient(llm_result(f"{PART_ONE_ZQ}\n↩2 {PART_TWO_ZQ}"))

    result = await rig.runner(client, quote_enabled=True).run(contact.id, later.id)
    transport = ScriptedTransport(clock=rig.clock)
    await _send_all(rig, _sender(rig, transport, RecordingNotifier()), contact)

    assert result.quoted is True
    assert transport.calls == [(contact.tg_id, PART_ONE_ZQ), (contact.tg_id, PART_TWO_ZQ)]
    assert transport.reply_tos == [None, earlier.tg_message_id]
    assert [row.state for row in await _rows(rig, contact)] == [OutboxState.SENT] * 2
    logs = json_logs(caplog)
    for marker in (EARLIER_TG_MESSAGE_ID, LATER_TG_MESSAGE_ID, EARLIER_ZQ, PART_TWO_ZQ, "↩"):
        assert_absent(marker, logs)


async def test_end_to_end_a_late_answer_quotes_the_trigger(rig: AutopilotRig) -> None:
    contact = await rig.contact()
    trigger = await _incoming(rig, contact, LATER_ZQ, LATER_TG_MESSAGE_ID, LATE_AGO)
    client = SequenceLLMClient(llm_result(f"{PART_ONE_ZQ}\n{PART_TWO_ZQ}"))

    await rig.runner(client, quote_enabled=True).run(contact.id, trigger.id)
    transport = ScriptedTransport(clock=rig.clock)
    await _send_all(rig, _sender(rig, transport, RecordingNotifier()), contact)

    assert transport.reply_tos == [trigger.tg_message_id, None]
    assert [text for _, text in transport.calls] == [PART_ONE_ZQ, PART_TWO_ZQ]


async def test_end_to_end_a_deleted_target_is_resent_plain(rig: AutopilotRig) -> None:
    contact = await rig.contact()
    trigger = await _incoming(rig, contact, LATER_ZQ, LATER_TG_MESSAGE_ID, 30)
    client = SequenceLLMClient(llm_result(PART_ONE_ZQ))

    planner = FixedPlanner(rig, delay_seconds=LATE_AGO)
    await rig.runner(client, quote_enabled=True, planner=planner).run(contact.id, trigger.id)
    transport = ScriptedTransport([SendReplyTargetGone(GONE)], clock=rig.clock)
    await _send_all(rig, _sender(rig, transport, RecordingNotifier()), contact)

    assert transport.reply_tos == [trigger.tg_message_id, None]
    [row] = await _rows(rig, contact)
    assert (row.state, row.reply_to_tg_message_id) == (OutboxState.SENT, trigger.tg_message_id)
    assert [message.text for message in await _outgoing(rig, contact)] == [PART_ONE_ZQ]
