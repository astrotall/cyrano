from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

import pytest
from telethon.errors import (
    FloodWaitError,
    MessageTooLongError,
    RPCError,
    ServerError,
)

from cyrano.bot.notifier import OUTBOX_NOTICES
from cyrano.db.repositories import (
    SqlContactRepository,
    SqlMessageRepository,
    SqlModeRepository,
    SqlOutboxRepository,
    SqlSenderStateRepository,
)
from cyrano.db.session import SessionFactory
from cyrano.domain.enums import (
    ContactStatus,
    MessageDirection,
    OutboxSource,
    OutboxState,
    RejectionReason,
)
from cyrano.domain.errors import SendFailure, SendFloodWait, SendRejected, SendUncertain
from cyrano.domain.models import Contact
from cyrano.domain.outbox import (
    CAUSE_SEND_DEADLINE,
    ERROR_INTERRUPTED,
    ERROR_UNCERTAIN,
    REFUSED_AUTOPILOT_DISABLED,
    REFUSED_CONTACT_NOT_ACTIVE,
    REFUSED_UNKNOWN_CONTACT,
    ScheduledMessage,
)
from cyrano.services.message_transport import MessageTransport
from cyrano.services.outbox_sender import (
    FLOOD_WAIT_MARGIN,
    FLOOD_WAIT_NOTIFY_ABOVE,
    SENDING_STALE_AFTER,
    OutboxSender,
    SenderTimings,
)
from cyrano.services.owner_notifier import OwnerNotifier
from cyrano.services.send_guard import SendGuard
from cyrano.services.send_limiter import SendLimiter
from cyrano.userbot.adapter import MISSING_RESPONSE, TelethonAdapter
from tests.privacy import (
    FAKE_OTHER_INTERNAL_ID,
    FAKE_TELEGRAM_ID,
    assert_absent,
)
from tests.services.outbox_fakes import (
    FAKE_NOW,
    FIRST_FAKE_TG_MESSAGE_ID,
    FakeClock,
    FakeLimits,
    HangingTransport,
    RecordingNotifier,
    ScriptedTransport,
    eventually,
    stored_contact,
)

TEXT = "outbox-text-marker-zq that must never leak"
USERNAME = "outbox_username_marker_zq"
DISPLAY_NAME = "OutboxNameMarkerZq"
FAST_POLL = SenderTimings(poll_interval=0.001)
FAR_AHEAD_SECONDS = 30 * 24 * 3600


class _RawTelethon:
    def __init__(self, error: Exception | None) -> None:
        self.send_calls = 0
        self._error = error

    async def get_input_entity(self, peer: int) -> object:
        return object()

    async def send_message(self, peer: object, text: str, **kwargs: object) -> None:
        self.send_calls += 1
        if self._error is not None:
            raise self._error


@dataclass(frozen=True)
class _Switch:
    autopilot_enabled: bool


@dataclass
class Harness:
    outbox: SqlOutboxRepository
    contacts: SqlContactRepository
    messages: SqlMessageRepository
    modes: SqlModeRepository
    clock: FakeClock
    contact: Contact
    notifier: RecordingNotifier
    state: SqlSenderStateRepository

    @property
    def limiter(self) -> SendLimiter:
        return SendLimiter(self.messages, FakeLimits())

    def sender(
        self,
        transport: MessageTransport,
        *,
        switch: bool = False,
        notifier: OwnerNotifier | None = None,
        timings: SenderTimings | None = None,
    ) -> OutboxSender:
        return OutboxSender(
            self.outbox,
            self.contacts,
            self.state,
            self.limiter,
            transport,
            notifier if notifier is not None else self.notifier,
            SendGuard(_Switch(switch)),
            OUTBOX_NOTICES,
            clock=self.clock,
            timings=timings,
        )

    async def row(
        self, source: OutboxSource = OutboxSource.MANUAL_TEST, *, contact_id: int | None = None
    ) -> ScheduledMessage:
        return await self.outbox.enqueue(
            contact_id if contact_id is not None else self.contact.id, TEXT, FAKE_NOW, source
        )

    async def stored(self, row_id: int) -> ScheduledMessage:
        row = await self.outbox.get(row_id)
        assert row is not None
        return row


@pytest.fixture
async def harness(
    outbox: SqlOutboxRepository,
    contacts: SqlContactRepository,
    messages: SqlMessageRepository,
    modes: SqlModeRepository,
    sender_state: SqlSenderStateRepository,
) -> Harness:
    contact = await stored_contact(
        contacts,
        modes,
        ContactStatus.ACTIVE,
        tg_id=FAKE_TELEGRAM_ID,
        username=USERNAME,
        display_name=DISPLAY_NAME,
    )
    return Harness(
        outbox, contacts, messages, modes, FakeClock(), contact, RecordingNotifier(), sender_state
    )


async def test_a_due_row_is_sent_stored_and_marked_sent(harness: Harness) -> None:
    row = await harness.row()
    await harness.contacts.touch_last_message_at(harness.contact.id, FAKE_NOW)
    transport = ScriptedTransport()

    assert await harness.sender(transport).send_next() is True

    assert transport.calls == [(FAKE_TELEGRAM_ID, TEXT)]
    stored = await harness.stored(row.id)
    assert stored.state is OutboxState.SENT
    assert stored.sent_tg_message_id == FIRST_FAKE_TG_MESSAGE_ID + 1
    messages = await harness.messages.list_last(harness.contact.id, 10)
    assert [(m.direction, m.text, m.tg_message_id) for m in messages] == [
        (MessageDirection.OUTGOING, TEXT, FIRST_FAKE_TG_MESSAGE_ID + 1)
    ]
    contact = await harness.contacts.get_by_id(harness.contact.id)
    assert contact is not None
    assert contact.last_message_at == FAKE_NOW
    assert harness.notifier.texts == []


async def test_nothing_due_sends_nothing(harness: Harness) -> None:
    transport = ScriptedTransport()

    assert await harness.sender(transport).send_next() is False

    assert transport.calls == []


async def test_a_row_in_the_future_waits_for_its_time(harness: Harness) -> None:
    row = await harness.outbox.enqueue(
        harness.contact.id, TEXT, FAKE_NOW + timedelta(seconds=30), OutboxSource.MANUAL_TEST
    )
    transport = ScriptedTransport()
    sender = harness.sender(transport)

    assert await sender.send_next() is False
    harness.clock.advance(30)
    assert await sender.send_next() is True

    assert (await harness.stored(row.id)).state is OutboxState.SENT


@pytest.mark.parametrize("status", [ContactStatus.PAUSED, ContactStatus.IGNORED])
async def test_a_contact_that_changed_after_enqueue_gets_nothing(
    harness: Harness, status: ContactStatus
) -> None:
    row = await harness.row()
    await harness.contacts.set_status(harness.contact.id, status)
    transport = ScriptedTransport()

    await harness.sender(transport).send_next()

    assert transport.calls == []
    stored = await harness.stored(row.id)
    assert stored.state is OutboxState.CANCELLED
    assert stored.cancel_reason == REFUSED_CONTACT_NOT_ACTIVE
    assert await harness.messages.list_last(harness.contact.id, 10) == []


@pytest.mark.parametrize("status", [ContactStatus.NEW, ContactStatus.PENDING])
async def test_a_contact_that_was_never_approved_gets_nothing(
    harness: Harness, status: ContactStatus
) -> None:
    other = await stored_contact(
        harness.contacts, harness.modes, status, tg_id=FAKE_TELEGRAM_ID + 1
    )
    row = await harness.row(contact_id=other.id)
    transport = ScriptedTransport()

    await harness.sender(transport, switch=True).send_next()

    assert transport.calls == []
    assert (await harness.stored(row.id)).state is OutboxState.CANCELLED


async def test_a_contact_missing_at_send_time_gets_nothing(harness: Harness) -> None:
    row = await harness.row()

    class _NoContacts(SqlContactRepository):
        async def get_by_id(self, contact_id: int) -> Contact | None:
            return None

    harness.contacts = _NoContacts(harness.contacts._session_factory)
    transport = ScriptedTransport()

    await harness.sender(transport).send_next()

    assert transport.calls == []
    stored = await harness.stored(row.id)
    assert (stored.state, stored.cancel_reason) == (
        OutboxState.CANCELLED,
        REFUSED_UNKNOWN_CONTACT,
    )


async def test_an_autopilot_row_with_the_switch_off_is_cancelled(harness: Harness) -> None:
    row = await harness.row(OutboxSource.AUTOPILOT)
    transport = ScriptedTransport()

    await harness.sender(transport, switch=False).send_next()

    assert transport.calls == []
    stored = await harness.stored(row.id)
    assert (stored.state, stored.cancel_reason) == (
        OutboxState.CANCELLED,
        REFUSED_AUTOPILOT_DISABLED,
    )


async def test_an_autopilot_row_with_the_switch_on_is_sent(harness: Harness) -> None:
    row = await harness.row(OutboxSource.AUTOPILOT)
    transport = ScriptedTransport()

    await harness.sender(transport, switch=True).send_next()

    assert (await harness.stored(row.id)).state is OutboxState.SENT


async def test_a_manual_row_ignores_the_switch(harness: Harness) -> None:
    row = await harness.row()
    transport = ScriptedTransport()

    await harness.sender(transport, switch=False).send_next()

    assert (await harness.stored(row.id)).state is OutboxState.SENT


async def _assert_uncertain_once_and_never_again(
    harness: Harness,
    sender: OutboxSender,
    row: ScheduledMessage,
    transport_calls: Callable[[], int],
) -> None:
    stored = await harness.stored(row.id)
    assert (stored.state, stored.error_class) == (OutboxState.FAILED, ERROR_UNCERTAIN)
    assert transport_calls() == 1
    notice = OUTBOX_NOTICES.uncertain.format(row_id=row.id, contact_id=row.contact_id)
    assert harness.notifier.texts == [notice]

    harness.clock.advance(FAR_AHEAD_SECONDS)
    assert await sender.send_next() is False
    await sender.fail_stale()
    await sender.sweep_interrupted()

    assert transport_calls() == 1
    assert harness.notifier.texts == [notice]
    assert (await harness.stored(row.id)).state is OutboxState.FAILED
    assert await harness.messages.list_last(harness.contact.id, 10) == []


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError(),
        ConnectionError("fake detail"),
        OSError("fake detail"),
        RPCError(request=None, message="FAKE_RPC", code=500),
        ServerError(request=None, message="FAKE_SERVER"),
        ValueError("Request was unsuccessful 3 time(s)"),
    ],
)
async def test_every_uncertain_cause_from_telethon_fails_once_and_is_never_sent_again(
    harness: Harness, error: Exception
) -> None:
    row = await harness.row()
    raw = _RawTelethon(error)
    sender = harness.sender(TelethonAdapter(raw))

    assert await sender.send_next() is True

    await _assert_uncertain_once_and_never_again(harness, sender, row, lambda: raw.send_calls)


async def test_a_send_without_a_response_message_is_uncertain_and_never_sent_again(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    row = await harness.row()
    raw = _RawTelethon(None)
    sender = harness.sender(TelethonAdapter(raw))

    await sender.send_next()

    await _assert_uncertain_once_and_never_again(harness, sender, row, lambda: raw.send_calls)
    assert any(getattr(r, "error", None) == MISSING_RESPONSE for r in caplog.records)


@pytest.mark.parametrize(
    "failure",
    [SendUncertain("FakeNetworkError"), SendFailure("FakeFutureFailure")],
)
async def test_an_uncertain_failure_from_the_transport_is_never_sent_again(
    harness: Harness, failure: SendFailure
) -> None:
    row = await harness.row()
    transport = ScriptedTransport([failure, failure])
    sender = harness.sender(transport)

    await sender.send_next()

    await _assert_uncertain_once_and_never_again(harness, sender, row, lambda: len(transport.calls))


async def test_a_send_that_hits_the_deadline_is_uncertain_and_never_sent_again(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    row = await harness.row()
    transport = HangingTransport()
    sender = harness.sender(transport, timings=SenderTimings(send_deadline=0.01))

    assert await sender.send_next() is True

    assert transport.cancelled is True
    await _assert_uncertain_once_and_never_again(harness, sender, row, lambda: transport.calls)
    assert any(getattr(r, "error", None) == CAUSE_SEND_DEADLINE for r in caplog.records)


async def test_the_running_loop_never_sends_an_uncertain_row_again(harness: Harness) -> None:
    row = await harness.row()
    transport = ScriptedTransport([SendUncertain("FakeNetworkError")])
    ticks = 0

    async def advance(_: float) -> None:
        nonlocal ticks
        ticks += 1
        if ticks == 60:
            raise _StopLoop
        harness.clock.advance(3600)

    sender = OutboxSender(
        harness.outbox,
        harness.contacts,
        harness.state,
        harness.limiter,
        transport,
        harness.notifier,
        SendGuard(_Switch(False)),
        OUTBOX_NOTICES,
        clock=harness.clock,
        sleep=advance,
    )

    with pytest.raises(_StopLoop):
        await sender.poll_forever()

    assert len(transport.calls) == 1
    assert len(harness.notifier.texts) == 1
    assert (await harness.stored(row.id)).state is OutboxState.FAILED


async def test_a_storage_failure_while_failing_an_uncertain_row_never_sends_it_again(
    harness: Harness,
) -> None:
    row = await harness.row()

    class _BrokenFail(SqlOutboxRepository):
        async def mark_failed(self, row_id: int, error_class: str) -> ScheduledMessage:
            raise RuntimeError("fake storage failure")

    repository = harness.outbox
    harness.outbox = _BrokenFail(repository._session_factory)
    transport = ScriptedTransport([SendUncertain("FakeNetworkError")])

    with pytest.raises(RuntimeError):
        await harness.sender(transport).send_next()

    assert (await harness.stored(row.id)).state is OutboxState.SENDING
    assert harness.notifier.texts == []
    harness.outbox = repository
    sender = harness.sender(transport)
    harness.clock.advance(SENDING_STALE_AFTER)
    assert await sender.send_next() is False
    await sender.fail_stale()

    stored = await harness.stored(row.id)
    assert (stored.state, stored.error_class) == (OutboxState.FAILED, ERROR_INTERRUPTED)
    assert len(transport.calls) == 1
    assert len(harness.notifier.texts) == 1


async def test_a_content_refusal_from_telethon_fails_with_its_class_and_the_plain_notice(
    harness: Harness,
) -> None:
    row = await harness.row()
    error = MessageTooLongError(request=None)
    raw = _RawTelethon(error)

    await harness.sender(TelethonAdapter(raw)).send_next()

    stored = await harness.stored(row.id)
    name = type(error).__name__
    assert (stored.state, stored.error_class) == (OutboxState.FAILED, name)
    assert raw.send_calls == 1
    assert harness.notifier.texts == [
        OUTBOX_NOTICES.failed.format(row_id=row.id, contact_id=row.contact_id, error=name)
    ]


async def test_a_flood_wait_from_telethon_is_rescheduled_and_sent_later(harness: Harness) -> None:
    row = await harness.row()
    raw = _RawTelethon(FloodWaitError(request=None, capture=30))

    await harness.sender(TelethonAdapter(raw)).send_next()

    stored = await harness.stored(row.id)
    assert stored.state is OutboxState.SCHEDULED
    assert stored.send_at == FAKE_NOW + timedelta(seconds=30 + FLOOD_WAIT_MARGIN)
    assert harness.notifier.texts == []


async def test_a_flood_wait_reschedules_at_the_wait_plus_the_margin(harness: Harness) -> None:
    row = await harness.row()
    wait = int(FLOOD_WAIT_NOTIFY_ABOVE)
    transport = ScriptedTransport([SendFloodWait("FakeFloodWait", wait)])
    sender = harness.sender(transport)

    await sender.send_next()

    stored = await harness.stored(row.id)
    assert stored.state is OutboxState.SCHEDULED
    assert stored.error_class == "FakeFloodWait"
    assert stored.send_at == FAKE_NOW + timedelta(seconds=wait + FLOOD_WAIT_MARGIN)
    assert harness.notifier.texts == []

    harness.clock.advance(wait + FLOOD_WAIT_MARGIN - 1)
    assert await sender.send_next() is False
    assert len(transport.calls) == 1
    harness.clock.advance(1)
    assert await sender.send_next() is True
    assert (await harness.stored(row.id)).state is OutboxState.SENT


async def test_a_long_flood_wait_notifies_the_owner(harness: Harness) -> None:
    row = await harness.row()
    wait = int(FLOOD_WAIT_NOTIFY_ABOVE) + 1
    transport = ScriptedTransport([SendFloodWait("FakeFloodWait", wait)])

    await harness.sender(transport).send_next()

    assert (await harness.stored(row.id)).state is OutboxState.SCHEDULED
    assert len(harness.notifier.texts) == 1
    assert str(wait) in harness.notifier.texts[0]
    assert f"#{row.id}" in harness.notifier.texts[0]


async def test_a_rejected_send_fails_with_one_notification_and_is_never_retried(
    harness: Harness,
) -> None:
    row = await harness.row()
    transport = ScriptedTransport([SendRejected("FakeBlockedError", RejectionReason.CONTENT)])
    sender = harness.sender(transport)

    await sender.send_next()

    stored = await harness.stored(row.id)
    assert (stored.state, stored.error_class) == (OutboxState.FAILED, "FakeBlockedError")
    assert harness.notifier.texts == [
        OUTBOX_NOTICES.failed.format(
            row_id=row.id, contact_id=row.contact_id, error="FakeBlockedError"
        )
    ]
    harness.clock.advance(3600)
    assert await sender.send_next() is False
    assert len(transport.calls) == 1


async def test_an_unexpected_exception_is_uncertain_and_logged_with_the_class_only(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    row = await harness.row()
    transport = ScriptedTransport([RuntimeError("fake detail that stays out")])
    sender = harness.sender(transport)
    caplog.set_level(logging.DEBUG)

    await sender.send_next()

    await _assert_uncertain_once_and_never_again(harness, sender, row, lambda: len(transport.calls))
    assert "fake detail" not in caplog.text
    assert any(getattr(r, "error", None) == "RuntimeError" for r in caplog.records)


async def test_a_cancelled_send_leaves_the_row_in_sending(harness: Harness) -> None:
    row = await harness.row()
    transport = HangingTransport()
    task = asyncio.create_task(harness.sender(transport).run())

    await asyncio.wait_for(transport.started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert transport.cancelled is True
    assert (await harness.stored(row.id)).state is OutboxState.SENDING
    assert await harness.messages.list_last(harness.contact.id, 10) == []
    assert harness.notifier.texts == []


async def test_the_next_start_sweeps_a_row_cancelled_mid_send(harness: Harness) -> None:
    row = await harness.row()
    transport = HangingTransport()
    task = asyncio.create_task(harness.sender(transport).run())
    await asyncio.wait_for(transport.started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    second = ScriptedTransport()

    await harness.sender(second).sweep_interrupted()

    assert second.calls == []
    assert (await harness.stored(row.id)).state is OutboxState.FAILED


async def test_the_startup_sweep_fails_leftovers_with_one_notice_each_and_never_sends(
    harness: Harness,
) -> None:
    first = await harness.row()
    second = await harness.row()
    untouched = await harness.outbox.enqueue(
        harness.contact.id, TEXT, FAKE_NOW + timedelta(days=1), OutboxSource.MANUAL_TEST
    )
    await harness.outbox.claim_due(FAKE_NOW)
    await harness.outbox.claim_due(FAKE_NOW)
    transport = ScriptedTransport()
    sender = harness.sender(transport)

    await sender.sweep_interrupted()

    for row in (first, second):
        stored = await harness.stored(row.id)
        assert (stored.state, stored.error_class) == (OutboxState.FAILED, ERROR_INTERRUPTED)
    assert (await harness.stored(untouched.id)).state is OutboxState.SCHEDULED
    assert transport.calls == []
    assert len(harness.notifier.texts) == 2
    assert f"#{first.id}" in harness.notifier.texts[0]
    assert f"#{second.id}" in harness.notifier.texts[1]

    await sender.sweep_interrupted()
    assert len(harness.notifier.texts) == 2


async def test_the_stale_check_fails_only_rows_older_than_the_limit(harness: Harness) -> None:
    old = await harness.row()
    await harness.outbox.claim_due(FAKE_NOW)
    harness.clock.advance(SENDING_STALE_AFTER)
    young = await harness.row()
    await harness.outbox.claim_due(harness.clock.now)
    transport = ScriptedTransport()
    harness.clock.advance(1)

    await harness.sender(transport).fail_stale()

    stored_old = await harness.stored(old.id)
    assert (stored_old.state, stored_old.error_class) == (OutboxState.FAILED, ERROR_INTERRUPTED)
    assert (await harness.stored(young.id)).state is OutboxState.SENDING
    assert transport.calls == []
    assert len(harness.notifier.texts) == 1


async def test_two_stale_checks_notify_once_about_one_row(harness: Harness) -> None:
    await harness.row()
    await harness.outbox.claim_due(FAKE_NOW)
    harness.clock.advance(SENDING_STALE_AFTER)
    transport = ScriptedTransport()

    await asyncio.gather(
        harness.sender(transport).fail_stale(), harness.sender(transport).fail_stale()
    )

    assert len(harness.notifier.texts) == 1


class _StopLoop(Exception):
    pass


async def _has_calls(transport: ScriptedTransport) -> bool:
    return bool(transport.calls)


async def _is_sent(harness: Harness, row_id: int) -> bool:
    return (await harness.stored(row_id)).state is OutboxState.SENT


async def _sent_at_least(harness: Harness, count: int) -> bool:
    return len(await harness.outbox.list_rows(OutboxState.SENT, count)) >= count


async def test_the_loop_runs_the_stale_check_on_its_interval(harness: Harness) -> None:
    row = await harness.row()
    await harness.outbox.claim_due(FAKE_NOW)
    timings = SenderTimings(poll_interval=10, stale_check_interval=60, stale_after=120)
    seen: list[OutboxState] = []
    ticks = 0

    async def advance(seconds: float) -> None:
        nonlocal ticks
        ticks += 1
        seen.append((await harness.stored(row.id)).state)
        if ticks == 13:
            raise _StopLoop
        harness.clock.advance(seconds)

    sender = OutboxSender(
        harness.outbox,
        harness.contacts,
        harness.state,
        harness.limiter,
        ScriptedTransport(),
        harness.notifier,
        SendGuard(_Switch(False)),
        OUTBOX_NOTICES,
        clock=harness.clock,
        timings=timings,
        sleep=advance,
    )

    with pytest.raises(_StopLoop):
        await sender.poll_forever()

    assert seen[:12] == [OutboxState.SENDING] * 12
    assert seen[12] is OutboxState.FAILED
    assert len(harness.notifier.texts) == 1


async def test_rows_go_out_one_at_a_time_in_order(harness: Harness) -> None:
    texts = [f"{TEXT} {number}" for number in range(4)]
    for offset, text in reversed(list(enumerate(texts))):
        await harness.outbox.enqueue(
            harness.contact.id,
            text,
            FAKE_NOW + timedelta(seconds=offset),
            OutboxSource.MANUAL_TEST,
        )
    harness.clock.advance(10)
    transport = ScriptedTransport()
    transport.delay = 0.01
    task = asyncio.create_task(harness.sender(transport, timings=FAST_POLL).run())

    await eventually(lambda: _sent_at_least(harness, len(texts)))
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert [text for _, text in transport.calls] == texts
    assert transport.max_active == 1


async def test_one_poll_sends_at_most_one_row(harness: Harness) -> None:
    await harness.row()
    await harness.row()
    transport = ScriptedTransport()

    await harness.sender(transport).send_next()

    assert len(transport.calls) == 1


async def test_two_sender_loops_never_send_a_row_twice(
    harness: Harness, session_factory: SessionFactory
) -> None:
    total = 12
    for number in range(total):
        await harness.outbox.enqueue(
            harness.contact.id, f"{TEXT} {number}", FAKE_NOW, OutboxSource.MANUAL_TEST
        )
    transport = ScriptedTransport()
    transport.delay = 0.001
    other = Harness(
        SqlOutboxRepository(session_factory),
        SqlContactRepository(session_factory),
        harness.messages,
        harness.modes,
        harness.clock,
        harness.contact,
        harness.notifier,
        SqlSenderStateRepository(session_factory),
    )
    tasks = [
        asyncio.create_task(harness.sender(transport, timings=FAST_POLL).poll_forever()),
        asyncio.create_task(other.sender(transport, timings=FAST_POLL).poll_forever()),
    ]

    await eventually(lambda: _sent_at_least(harness, total))
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    sent_texts = [text for _, text in transport.calls]
    assert len(sent_texts) == total
    assert len(set(sent_texts)) == total
    assert len(await harness.messages.list_last(harness.contact.id, total + 1)) == total


async def test_a_bad_row_never_stops_the_loop(harness: Harness) -> None:
    bad = await harness.row()
    good = await harness.row()

    class _BrokenForOneRow(SqlOutboxRepository):
        async def complete_send(
            self, row_id: int, contact_id: int, text: str, tg_message_id: int, sent_at: datetime
        ) -> ScheduledMessage:
            if row_id == bad.id:
                raise RuntimeError("fake storage failure")
            return await super().complete_send(row_id, contact_id, text, tg_message_id, sent_at)

    harness.outbox = _BrokenForOneRow(harness.outbox._session_factory)
    transport = ScriptedTransport()
    task = asyncio.create_task(harness.sender(transport, timings=FAST_POLL).poll_forever())

    await eventually(lambda: _is_sent(harness, good.id))
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert task.cancelled()


async def test_a_storage_failure_after_the_send_is_never_followed_by_a_second_send(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    row = await harness.row()

    class _BrokenComplete(SqlOutboxRepository):
        async def complete_send(
            self, row_id: int, contact_id: int, text: str, tg_message_id: int, sent_at: datetime
        ) -> ScheduledMessage:
            raise RuntimeError("fake storage failure")

    repository = harness.outbox
    harness.outbox = _BrokenComplete(repository._session_factory)
    transport = ScriptedTransport()
    timings = SenderTimings(poll_interval=0.001)
    task = asyncio.create_task(harness.sender(transport, timings=timings).poll_forever())

    await eventually(lambda: _has_calls(transport))
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert len(transport.calls) == 1
    assert (await repository.get(row.id)) is not None
    assert (await harness.stored(row.id)).state is OutboxState.SENDING
    harness.outbox = repository
    harness.clock.advance(SENDING_STALE_AFTER)
    await harness.sender(transport).fail_stale()
    assert (await harness.stored(row.id)).state is OutboxState.FAILED
    assert len(transport.calls) == 1
    assert len(harness.notifier.texts) == 1


async def test_a_send_past_its_deadline_does_not_hold_up_the_next_row(harness: Harness) -> None:
    stuck = await harness.row()
    good = await harness.row()
    transport = HangingTransport()
    timings = SenderTimings(send_deadline=0.01)

    await harness.sender(transport, timings=timings).send_next()
    await harness.sender(ScriptedTransport()).send_next()

    assert (await harness.stored(stuck.id)).state is OutboxState.FAILED
    assert (await harness.stored(good.id)).state is OutboxState.SENT


@pytest.mark.parametrize(
    "failure",
    [SendRejected("FakeBlockedError", RejectionReason.CONTENT), SendUncertain("FakeNetworkError")],
)
async def test_a_failing_notifier_changes_nothing(harness: Harness, failure: SendFailure) -> None:
    row = await harness.row()
    transport = ScriptedTransport([failure])
    notifier = RecordingNotifier(error=RuntimeError("fake notifier failure"))

    await harness.sender(transport, notifier=notifier).send_next()

    assert (await harness.stored(row.id)).state is OutboxState.FAILED
    assert len(notifier.texts) == 1


async def test_a_storage_failure_in_the_startup_sweep_does_not_stop_the_loop(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.ERROR)
    good = await harness.row()

    class _BrokenSweep(SqlOutboxRepository):
        async def stale_sending(self, claimed_before: datetime) -> list[ScheduledMessage]:
            raise RuntimeError("fake storage failure")

    harness.outbox = _BrokenSweep(harness.outbox._session_factory)
    transport = ScriptedTransport()
    task = asyncio.create_task(harness.sender(transport, timings=FAST_POLL).run())

    await eventually(lambda: _is_sent(harness, good.id))
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert any(getattr(r, "error", None) == "RuntimeError" for r in caplog.records)
    assert "fake storage failure" not in caplog.text


async def test_notifications_and_logs_carry_no_personal_data(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    transport = ScriptedTransport(
        [
            SendUncertain("FakeNetworkError"),
            SendFloodWait("FakeFloodWait", 3600),
            SendRejected("FakeBlockedError", RejectionReason.CONTENT),
            RuntimeError(TEXT),
        ]
    )
    sender = harness.sender(transport, switch=True)
    for _ in range(3):
        await harness.row(OutboxSource.AUTOPILOT)
    await harness.outbox.claim_due(FAKE_NOW)
    await sender.sweep_interrupted()
    for _ in range(6):
        await sender.send_next()
        harness.clock.advance(10_000)
    await harness.row()
    await harness.contacts.set_status(harness.contact.id, ContactStatus.PAUSED)
    await sender.send_next()

    assert len(harness.notifier.texts) >= 3
    haystack = "\n".join(
        [
            caplog.text,
            *harness.notifier.texts,
            *(str(record.__dict__) for record in caplog.records),
        ]
    )
    for marker in (TEXT, USERNAME, DISPLAY_NAME, FAKE_TELEGRAM_ID, FAKE_OTHER_INTERNAL_ID):
        assert_absent(marker, haystack)


@pytest.mark.parametrize(
    ("build_transport", "cause"),
    [
        (
            lambda: TelethonAdapter(
                _RawTelethon(ConnectionError(f"{TEXT} {USERNAME} {FAKE_TELEGRAM_ID}"))
            ),
            "ConnectionError",
        ),
        (
            lambda: TelethonAdapter(_RawTelethon(ValueError(f"{DISPLAY_NAME} {FAKE_TELEGRAM_ID}"))),
            "ValueError",
        ),
        (
            lambda: TelethonAdapter(
                _RawTelethon(RPCError(request=None, message=TEXT, code=FAKE_TELEGRAM_ID))
            ),
            "RPCError",
        ),
        (lambda: ScriptedTransport([RuntimeError(TEXT)]), "RuntimeError"),
        (HangingTransport, CAUSE_SEND_DEADLINE),
    ],
)
async def test_an_uncertain_send_tells_ids_and_the_class_and_nothing_personal(
    harness: Harness,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    build_transport: Callable[[], MessageTransport],
    cause: str,
) -> None:
    caplog.set_level(logging.DEBUG)
    row = await harness.row()
    timings = SenderTimings(send_deadline=0.01)

    await harness.sender(build_transport(), timings=timings).send_next()

    captured = capsys.readouterr()
    haystack = "\n".join(
        [
            caplog.text,
            captured.out,
            captured.err,
            *harness.notifier.texts,
            *(str(record.__dict__) for record in caplog.records),
        ]
    )
    for marker in (TEXT, USERNAME, DISPLAY_NAME, FAKE_TELEGRAM_ID):
        assert_absent(marker, haystack)
    assert any(getattr(record, "error", None) == cause for record in caplog.records)
    assert len(harness.notifier.texts) == 1
    assert cause not in harness.notifier.texts[0]
    numbers = set(re.findall(r"\d+", harness.notifier.texts[0]))
    assert numbers == {str(row.id), str(harness.contact.id)}
