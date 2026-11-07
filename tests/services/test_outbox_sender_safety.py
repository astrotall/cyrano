from __future__ import annotations

import asyncio
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import pytest
from sqlalchemy.engine import URL
from telethon.errors import (
    ActiveUserRequiredError,
    AuthKeyDuplicatedError,
    AuthKeyInvalidError,
    AuthKeyPermEmptyError,
    AuthKeyUnregisteredError,
    ChatWriteForbiddenError,
    FloodWaitError,
    ForbiddenError,
    FrozenMethodInvalidError,
    MessageTooLongError,
    PeerFloodError,
    SessionExpiredError,
    SessionPasswordNeededError,
    SessionRevokedError,
    UnauthorizedError,
    UserDeactivatedBanError,
    UserDeactivatedError,
    UserIsBlockedError,
    UserPrivacyRestrictedError,
    UserRestrictedError,
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
    LimitScope,
    LimitWindow,
    OutboxSource,
    OutboxState,
    PauseReason,
    RejectionReason,
)
from cyrano.domain.errors import (
    SendAccountBlocked,
    SendFailure,
    SendFloodWait,
    SendRejected,
    SendUncertain,
)
from cyrano.domain.models import Contact, StatusChange
from cyrano.domain.outbox import (
    CANCEL_REASON_SEND_REJECTED,
    CANCEL_REASON_STALE,
    ERROR_INTERRUPTED,
    ERROR_RATE_LIMITED,
    ERROR_UNCERTAIN,
    LIMIT_NOTICE_INTERVAL_SECONDS,
    ROW_STALE_AFTER_SECONDS,
    STALE_NOTICE_CONTACT_IDS_MAX,
    STALE_NOTICE_INTERVAL_SECONDS,
    STOP_REMINDER_INTERVAL_SECONDS,
    ScheduledMessage,
    SenderState,
)
from cyrano.services.message_transport import MessageTransport
from cyrano.services.outbox_sender import (
    FLOOD_WAIT_MARGIN,
    FLOOD_WAIT_NOTIFY_ABOVE,
    OWNER_NOTIFY_DEADLINE,
    SENDER_POLL_INTERVAL,
    SENDING_STALE_AFTER,
    OutboxSender,
    SenderTimings,
)
from cyrano.services.owner_notifier import OwnerNotifier
from cyrano.services.send_guard import SendGuard
from cyrano.services.send_limiter import SendLimiter
from cyrano.userbot.adapter import PEER_NOT_FOUND, TelethonAdapter
from tests.capped_loop import CappedLoop
from tests.db.conftest import fetch_all
from tests.privacy import FAKE_TELEGRAM_ID, assert_absent
from tests.services.outbox_fakes import (
    FAKE_NOW,
    FakeClock,
    FakeLimits,
    HangingNotifier,
    RecordingNotifier,
    ScriptedTransport,
    eventually,
    free_schedule,
    stored_contact,
)

TEXT = "safety-text-marker-zq that must never leak"
USERNAME = "safety_username_marker_zq"
DISPLAY_NAME = "SafetyNameMarkerZq"
OTHER_TG_ID = FAKE_TELEGRAM_ID + 1
FAST_POLL = SenderTimings(poll_interval=0.001)
NOTICE_INTERVAL = LIMIT_NOTICE_INTERVAL_SECONDS
ACCOUNT_KINDS = [
    "PeerFloodError",
    "UserDeactivatedError",
    "UserDeactivatedBanError",
    "AuthKeyUnregisteredError",
    "AuthKeyInvalidError",
    "SessionRevokedError",
    "SessionExpiredError",
    "UserRestrictedError",
    "FrozenMethodInvalidError",
    "AuthKeyDuplicatedError",
    "AuthKeyPermEmptyError",
    "ActiveUserRequiredError",
    "SessionPasswordNeededError",
    "UnauthorizedError",
]
FIRST_HELD_TG_ID = 700900
FAST_NOTIFY_DEADLINE = 0.01
NOTIFIER_DETAIL = "notifier-detail-marker-zq"
NOTIFY_FAILED_LOG = "owner notification failed"
NOTICE_PATHS = [
    (SendUncertain("FakeNetworkError"), OutboxState.FAILED, ERROR_UNCERTAIN, ContactStatus.ACTIVE),
    (
        SendRejected("FakeTooLongError", RejectionReason.CONTENT),
        OutboxState.FAILED,
        "FakeTooLongError",
        ContactStatus.ACTIVE,
    ),
    (
        SendRejected("FakeBlockedError", RejectionReason.RECIPIENT),
        OutboxState.FAILED,
        "FakeBlockedError",
        ContactStatus.PAUSED,
    ),
    (SendAccountBlocked("FakeKind"), OutboxState.FAILED, "FakeKind", ContactStatus.ACTIVE),
    (
        SendFloodWait("FakeFloodWait", 3600),
        OutboxState.SCHEDULED,
        "FakeFloodWait",
        ContactStatus.ACTIVE,
    ),
]


@dataclass(frozen=True)
class _Switch:
    autopilot_enabled: bool = False


class _Raw:
    def __init__(self, error: Exception | None, peer_error: Exception | None = None) -> None:
        self.send_calls = 0
        self._error = error
        self._peer_error = peer_error

    async def get_input_entity(self, peer: int) -> object:
        if self._peer_error is not None:
            raise self._peer_error
        return object()

    async def send_message(self, peer: object, text: str, **kwargs: object) -> None:
        self.send_calls += 1
        if self._error is not None:
            raise self._error


@dataclass
class Rig:
    outbox: SqlOutboxRepository
    contacts: SqlContactRepository
    messages: SqlMessageRepository
    state: SqlSenderStateRepository
    contact: Contact
    other: Contact
    clock: FakeClock = field(default_factory=FakeClock)
    notifier: RecordingNotifier = field(default_factory=RecordingNotifier)

    def sender(
        self,
        transport: MessageTransport,
        limits: FakeLimits | None = None,
        *,
        timings: SenderTimings | None = None,
        notifier: OwnerNotifier | None = None,
    ) -> OutboxSender:
        return OutboxSender(
            self.outbox,
            self.contacts,
            self.state,
            SendLimiter(self.messages, limits if limits is not None else FakeLimits()),
            free_schedule(),
            transport,
            notifier if notifier is not None else self.notifier,
            SendGuard(_Switch()),
            OUTBOX_NOTICES,
            clock=self.clock,
            timings=timings,
        )

    def transport(self, script: list[Exception] | None = None) -> ScriptedTransport:
        return ScriptedTransport(script or (), clock=self.clock)

    async def row(self, contact: Contact | None = None) -> ScheduledMessage:
        target = contact if contact is not None else self.contact
        return await self.outbox.enqueue(target.id, TEXT, self.clock(), OutboxSource.MANUAL_TEST)

    async def stored(self, row_id: int) -> ScheduledMessage:
        row = await self.outbox.get(row_id)
        assert row is not None
        return row

    async def status(self, contact: Contact | None = None) -> ContactStatus:
        target = contact if contact is not None else self.contact
        found = await self.contacts.get_by_id(target.id)
        assert found is not None
        return found.status


@pytest.fixture
async def rig(
    outbox: SqlOutboxRepository,
    contacts: SqlContactRepository,
    messages: SqlMessageRepository,
    modes: SqlModeRepository,
    sender_state: SqlSenderStateRepository,
) -> Rig:
    contact = await stored_contact(
        contacts,
        modes,
        ContactStatus.ACTIVE,
        tg_id=FAKE_TELEGRAM_ID,
        username=USERNAME,
        display_name=DISPLAY_NAME,
    )
    other = await stored_contact(contacts, modes, ContactStatus.ACTIVE, tg_id=OTHER_TG_ID)
    return Rig(outbox, contacts, messages, sender_state, contact, other)


async def test_a_limited_row_goes_back_to_scheduled_and_is_sent_once_the_slot_frees(
    rig: Rig,
) -> None:
    transport = rig.transport()
    sender = rig.sender(transport, FakeLimits(send_limit_minute=1))
    first = await rig.row()
    limited = await rig.row()

    assert await sender.send_next() is True
    assert (await rig.stored(first.id)).state is OutboxState.SENT

    rig.clock.advance(10)
    assert await sender.send_next() is True

    stored = await rig.stored(limited.id)
    assert stored.state is OutboxState.SCHEDULED
    assert stored.send_at == FAKE_NOW + timedelta(seconds=60)
    assert stored.claimed_at is None
    assert stored.error_class == ERROR_RATE_LIMITED
    assert len(transport.calls) == 1

    rig.clock.advance(49)
    assert await sender.send_next() is False
    assert len(transport.calls) == 1

    rig.clock.advance(1)
    assert await sender.send_next() is True
    stored = await rig.stored(limited.id)
    assert stored.state is OutboxState.SENT
    assert stored.error_class is None
    assert len(transport.calls) == 2


async def test_holding_a_row_for_a_limit_is_not_counted_as_an_attempt(
    rig: Rig, migrated_url: URL
) -> None:
    sender = rig.sender(rig.transport(), FakeLimits(send_limit_minute=1))
    await rig.row()
    await sender.send_next()
    limited = await rig.row()

    for _ in range(3):
        await sender.send_next()
        rig.clock.advance(1)

    assert (await rig.stored(limited.id)).state is OutboxState.SCHEDULED
    assert await fetch_all(migrated_url, "SELECT attempts FROM scheduled_messages") == [(0,), (0,)]


async def test_a_limited_row_is_held_as_often_as_needed_and_never_dropped_or_failed(
    rig: Rig,
) -> None:
    transport = rig.transport()
    sender = rig.sender(transport, FakeLimits(send_limit_minute=1))
    rows = [await rig.row() for _ in range(4)]

    for _ in range(40):
        await sender.send_next()
        rig.clock.advance(15)

    states = [(await rig.stored(row.id)).state for row in rows]
    assert states == [OutboxState.SENT] * 4
    assert len(transport.calls) == 4


async def test_the_limits_never_let_more_through_than_allowed_in_any_minute(rig: Rig) -> None:
    transport = rig.transport()
    sender = rig.sender(transport, FakeLimits(send_limit_minute=2))
    for _ in range(5):
        await rig.row()
    sent_times = []

    for _ in range(200):
        before = len(transport.calls)
        await sender.send_next()
        if len(transport.calls) > before:
            sent_times.append(rig.clock())
        rig.clock.advance(1)

    assert len(sent_times) == 5
    for start in sent_times:
        window = [t for t in sent_times if start <= t < start + timedelta(seconds=60)]
        assert len(window) <= 2


async def test_a_limit_counts_from_the_database_so_it_survives_a_new_sender(rig: Rig) -> None:
    limits = FakeLimits(send_limit_day=1)
    await rig.row()
    await rig.sender(rig.transport(), limits).send_next()
    rig.clock.advance(3600)
    limited = await rig.row()
    transport = rig.transport()

    await rig.sender(transport, limits).send_next()

    assert transport.calls == []
    assert (await rig.stored(limited.id)).send_at == FAKE_NOW + timedelta(days=1)


async def test_a_per_contact_limit_holds_only_that_contact(rig: Rig) -> None:
    transport = rig.transport()
    sender = rig.sender(transport, FakeLimits(send_limit_contact_minute=1))
    await rig.row()
    await sender.send_next()
    held = await rig.row()
    free = await rig.row(rig.other)
    rig.clock.advance(1)

    await sender.send_next()
    await sender.send_next()

    assert (await rig.stored(held.id)).state is OutboxState.SCHEDULED
    assert (await rig.stored(free.id)).state is OutboxState.SENT


async def test_the_guard_runs_before_the_limiter_and_cancels_a_limited_row_of_a_paused_contact(
    rig: Rig,
) -> None:
    sender = rig.sender(rig.transport(), FakeLimits(send_limit_minute=1))
    await rig.row()
    await sender.send_next()
    row = await rig.row()
    await rig.contacts.set_status(rig.contact.id, ContactStatus.PAUSED)

    await sender.send_next()

    assert (await rig.stored(row.id)).state is OutboxState.CANCELLED


async def test_a_limited_row_of_a_contact_paused_meanwhile_is_cancelled_when_it_comes_back(
    rig: Rig,
) -> None:
    transport = rig.transport()
    sender = rig.sender(transport, FakeLimits(send_limit_minute=1))
    await rig.row()
    await sender.send_next()
    row = await rig.row()
    await sender.send_next()
    await rig.contacts.set_status(rig.contact.id, ContactStatus.PAUSED)
    rig.clock.advance(60)

    await sender.send_next()

    assert (await rig.stored(row.id)).state is OutboxState.CANCELLED
    assert len(transport.calls) == 1


@pytest.mark.parametrize(
    ("limits", "scope", "window"),
    [
        (FakeLimits(send_limit_hour=1), LimitScope.GLOBAL, LimitWindow.HOUR),
        (FakeLimits(send_limit_day=1), LimitScope.GLOBAL, LimitWindow.DAY),
        (FakeLimits(send_limit_contact_hour=1), LimitScope.CONTACT, LimitWindow.HOUR),
        (FakeLimits(send_limit_contact_day=1), LimitScope.CONTACT, LimitWindow.DAY),
    ],
)
async def test_an_hourly_or_daily_limit_notifies_once_an_hour_for_its_kind(
    rig: Rig, limits: FakeLimits, scope: LimitScope, window: LimitWindow
) -> None:
    sender = rig.sender(rig.transport(), limits)
    await rig.row()
    await sender.send_next()
    expected = OUTBOX_NOTICES.limit_reached[(scope, window)].format(contact_id=rig.contact.id)

    await rig.row()
    await sender.send_next()
    assert rig.notifier.texts == [expected]

    rig.clock.advance(NOTICE_INTERVAL - 1)
    await rig.row()
    await sender.send_next()
    assert rig.notifier.texts == [expected]

    rig.clock.advance(1)
    await rig.row()
    await sender.send_next()
    await sender.send_next()
    assert rig.notifier.texts == [expected, expected]


async def test_a_held_row_coming_back_does_not_repeat_the_notice_within_the_hour(rig: Rig) -> None:
    sender = rig.sender(rig.transport(), FakeLimits(send_limit_hour=1))
    await rig.row()
    await sender.send_next()
    for _ in range(5):
        await rig.row()
    for _ in range(5):
        await sender.send_next()

    assert len(rig.notifier.texts) == 1


async def test_a_second_contact_at_the_same_limit_within_the_hour_is_the_same_kind(
    rig: Rig,
) -> None:
    sender = rig.sender(rig.transport(), FakeLimits(send_limit_contact_day=1))
    await rig.row()
    await rig.row(rig.other)
    await sender.send_next()
    await sender.send_next()
    await rig.row()
    await rig.row(rig.other)

    await sender.send_next()
    await sender.send_next()

    assert len(rig.notifier.texts) == 1


async def test_two_kinds_at_once_give_one_notice_each(rig: Rig) -> None:
    sender = rig.sender(rig.transport(), FakeLimits(send_limit_hour=1, send_limit_contact_day=1))
    await rig.row()
    await sender.send_next()
    await rig.row()

    await sender.send_next()

    assert sorted(rig.notifier.texts) == sorted(
        [
            OUTBOX_NOTICES.limit_reached[(LimitScope.GLOBAL, LimitWindow.HOUR)].format(
                contact_id=rig.contact.id
            ),
            OUTBOX_NOTICES.limit_reached[(LimitScope.CONTACT, LimitWindow.DAY)].format(
                contact_id=rig.contact.id
            ),
        ]
    )


@pytest.mark.parametrize(
    "limits", [FakeLimits(send_limit_minute=1), FakeLimits(send_limit_contact_minute=1)]
)
async def test_a_minute_limit_never_notifies(rig: Rig, limits: FakeLimits) -> None:
    sender = rig.sender(rig.transport(), limits)
    for _ in range(3):
        await rig.row()

    for _ in range(3):
        await sender.send_next()

    assert rig.notifier.texts == []


async def test_a_restart_may_repeat_a_limit_notice(rig: Rig) -> None:
    limits = FakeLimits(send_limit_hour=1)
    await rig.row()
    await rig.sender(rig.transport(), limits).send_next()
    await rig.row()
    await rig.sender(rig.transport(), limits).send_next()
    await rig.row()
    await rig.sender(rig.transport(), limits).send_next()

    assert len(rig.notifier.texts) == 2


async def test_a_failing_notice_about_a_limit_changes_nothing(rig: Rig) -> None:
    rig.notifier = RecordingNotifier(error=RuntimeError("fake notifier failure"))
    sender = rig.sender(rig.transport(), FakeLimits(send_limit_hour=1))
    await rig.row()
    await sender.send_next()
    row = await rig.row()

    await sender.send_next()

    assert (await rig.stored(row.id)).state is OutboxState.SCHEDULED


def test_the_notifier_deadline_is_fifteen_seconds() -> None:
    assert OWNER_NOTIFY_DEADLINE == 15.0
    assert SenderTimings().notify_deadline == OWNER_NOTIFY_DEADLINE


def _notice_failures(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.getMessage() == NOTIFY_FAILED_LOG]


async def _assert_the_decision_of_the_notice_path(
    rig: Rig,
    row_id: int,
    failure: SendFailure,
    state: OutboxState,
    error: str,
    status: ContactStatus,
) -> None:
    stored = await rig.stored(row_id)
    assert (stored.state, stored.error_class) == (state, error)
    assert await rig.status() is status
    sender_state = await rig.state.get()
    assert sender_state.stopped is isinstance(failure, SendAccountBlocked)
    assert sender_state.is_paused(rig.clock()) is isinstance(failure, SendFloodWait)


@pytest.mark.parametrize(("failure", "state", "error", "status"), NOTICE_PATHS)
async def test_a_hanging_notifier_is_cut_at_the_deadline_and_changes_no_decision(
    rig: Rig,
    caplog: pytest.LogCaptureFixture,
    failure: SendFailure,
    state: OutboxState,
    error: str,
    status: ContactStatus,
) -> None:
    caplog.set_level(logging.DEBUG)
    row = await rig.row()
    notifier = HangingNotifier()
    sender = rig.sender(
        rig.transport([failure]),
        timings=SenderTimings(notify_deadline=FAST_NOTIFY_DEADLINE),
        notifier=notifier,
    )

    assert await sender.send_next() is True

    await _assert_the_decision_of_the_notice_path(rig, row.id, failure, state, error, status)
    assert (notifier.calls, notifier.cancelled) == (1, 1)
    logged = _notice_failures(caplog)
    assert [getattr(record, "error", None) for record in logged] == ["TimeoutError"]


@pytest.mark.parametrize(("failure", "state", "error", "status"), NOTICE_PATHS)
async def test_a_raising_notifier_changes_no_decision_and_only_its_class_name_is_logged(
    rig: Rig,
    caplog: pytest.LogCaptureFixture,
    failure: SendFailure,
    state: OutboxState,
    error: str,
    status: ContactStatus,
) -> None:
    caplog.set_level(logging.DEBUG)
    row = await rig.row()
    notifier = RecordingNotifier(error=ConnectionError(NOTIFIER_DETAIL))
    sender = rig.sender(rig.transport([failure]), notifier=notifier)

    assert await sender.send_next() is True

    await _assert_the_decision_of_the_notice_path(rig, row.id, failure, state, error, status)
    assert len(notifier.texts) == 1
    logged = _notice_failures(caplog)
    assert [getattr(record, "error", None) for record in logged] == ["ConnectionError"]
    assert NOTIFIER_DETAIL not in caplog.text
    assert not any(notifier.texts[0] in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize(
    "notifier",
    [HangingNotifier(), RecordingNotifier(error=ConnectionError(NOTIFIER_DETAIL))],
    ids=["hanging", "raising"],
)
async def test_a_bad_notifier_on_a_limit_notice_leaves_the_row_held(
    rig: Rig, caplog: pytest.LogCaptureFixture, notifier: OwnerNotifier
) -> None:
    caplog.set_level(logging.DEBUG)
    sender = rig.sender(
        rig.transport(),
        FakeLimits(send_limit_hour=1),
        timings=SenderTimings(notify_deadline=FAST_NOTIFY_DEADLINE),
        notifier=notifier,
    )
    await rig.row()
    await sender.send_next()
    held = await rig.row()

    await sender.send_next()

    stored = await rig.stored(held.id)
    assert (stored.state, stored.error_class) == (OutboxState.SCHEDULED, ERROR_RATE_LIMITED)
    assert len(_notice_failures(caplog)) == 1


async def test_a_hanging_notifier_does_not_hold_back_the_other_interrupted_rows(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    first = await rig.row()
    second = await rig.row()
    assert await rig.outbox.claim_due(rig.clock()) is not None
    assert await rig.outbox.claim_due(rig.clock()) is not None
    notifier = HangingNotifier()
    sender = rig.sender(
        rig.transport(),
        timings=SenderTimings(notify_deadline=FAST_NOTIFY_DEADLINE),
        notifier=notifier,
    )

    await sender.sweep_interrupted()

    for row in (first, second):
        stored = await rig.stored(row.id)
        assert (stored.state, stored.error_class) == (OutboxState.FAILED, ERROR_INTERRUPTED)
    assert (notifier.calls, notifier.cancelled) == (2, 2)
    assert len(_notice_failures(caplog)) == 2


@pytest.mark.parametrize(
    "notifier",
    [HangingNotifier(), RecordingNotifier(error=ConnectionError(NOTIFIER_DETAIL))],
    ids=["hanging", "raising"],
)
async def test_the_running_loop_goes_on_past_a_bad_notifier(
    rig: Rig, notifier: OwnerNotifier
) -> None:
    failed = await rig.row()
    sent = await rig.row()
    transport = rig.transport([SendUncertain("FakeNetworkError")])
    sender = rig.sender(
        transport,
        timings=SenderTimings(poll_interval=0.001, notify_deadline=FAST_NOTIFY_DEADLINE),
        notifier=notifier,
    )

    async def second_row_is_sent() -> bool:
        return (await rig.stored(sent.id)).state is OutboxState.SENT

    task = asyncio.create_task(sender.poll_forever())
    try:
        await eventually(second_row_is_sent)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert (await rig.stored(failed.id)).state is OutboxState.FAILED
    assert len(transport.calls) == 2


async def test_a_shutdown_during_a_notice_still_cancels_the_sender_at_once(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    row = await rig.row()
    notifier = HangingNotifier()
    sender = rig.sender(rig.transport([SendUncertain("FakeNetworkError")]), notifier=notifier)

    task = asyncio.create_task(sender.send_next())
    await notifier.started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert notifier.cancelled == 1
    assert _notice_failures(caplog) == []
    assert (await rig.stored(row.id)).state is OutboxState.FAILED


async def test_a_stale_row_is_cancelled_without_a_transport_call(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    row = await rig.row()
    rig.clock.advance(ROW_STALE_AFTER_SECONDS + 1)
    transport = rig.transport()

    assert await rig.sender(transport).send_next() is True

    stored = await rig.stored(row.id)
    assert (stored.state, stored.cancel_reason) == (OutboxState.CANCELLED, CANCEL_REASON_STALE)
    assert transport.calls == []
    assert rig.notifier.texts == [_stale_notice(1, rig.contact)]
    assert any(getattr(record, "row_id", None) == row.id for record in caplog.records)


async def test_a_row_exactly_at_the_stale_age_is_still_sent(rig: Rig) -> None:
    row = await rig.row()
    rig.clock.advance(ROW_STALE_AFTER_SECONDS)
    transport = rig.transport()

    await rig.sender(transport).send_next()

    assert (await rig.stored(row.id)).state is OutboxState.SENT
    assert len(transport.calls) == 1


async def test_the_stale_check_comes_before_the_guard_and_the_limiter(rig: Rig) -> None:
    row = await rig.row()
    await rig.contacts.set_status(rig.contact.id, ContactStatus.PAUSED)
    rig.clock.advance(ROW_STALE_AFTER_SECONDS + 1)

    await rig.sender(rig.transport(), FakeLimits(send_limit_minute=1)).send_next()

    assert (await rig.stored(row.id)).cancel_reason == CANCEL_REASON_STALE


async def test_a_row_held_past_the_stale_age_by_a_daily_limit_is_stale_when_it_returns(
    rig: Rig,
) -> None:
    transport = rig.transport()
    sender = rig.sender(transport, FakeLimits(send_limit_day=1))
    await rig.row()
    await sender.send_next()
    held = await rig.row()
    await sender.send_next()
    moved = await rig.stored(held.id)
    assert moved.send_at == FAKE_NOW + timedelta(days=1)
    assert moved.due_at == FAKE_NOW

    rig.clock.advance(86400)
    await sender.send_next()

    stored = await rig.stored(held.id)
    assert (stored.state, stored.cancel_reason) == (OutboxState.CANCELLED, CANCEL_REASON_STALE)
    assert stored.due_at == FAKE_NOW
    assert len(transport.calls) == 1


async def _store_outgoing(rig: Rig, tg_message_id: int, at: datetime) -> None:
    long_ago = FAKE_NOW - timedelta(days=1)
    row = await rig.outbox.enqueue(rig.other.id, TEXT, long_ago, OutboxSource.MANUAL_TEST)
    assert await rig.outbox.claim_due(long_ago) is not None
    await rig.outbox.complete_send(row.id, rig.other.id, TEXT, tg_message_id, at)


async def test_a_row_held_by_a_limit_keeps_its_due_time_through_every_hold(rig: Rig) -> None:
    transport = rig.transport()
    sender = rig.sender(transport, FakeLimits(send_limit_minute=1))
    await rig.row()
    await sender.send_next()
    held = await rig.row()

    for hold in range(3):
        await sender.send_next()
        stored = await rig.stored(held.id)
        assert stored.state is OutboxState.SCHEDULED
        assert stored.send_at == FAKE_NOW + timedelta(seconds=60 * (hold + 1))
        assert stored.due_at == FAKE_NOW
        await _store_outgoing(rig, FIRST_HELD_TG_ID + hold, stored.send_at)
        rig.clock.now = stored.send_at


@pytest.mark.parametrize(
    ("over_the_age", "expected"),
    [(0, OutboxState.SENT), (1, OutboxState.CANCELLED)],
)
async def test_the_stale_age_of_a_held_row_counts_strictly_from_its_due_time(
    rig: Rig, over_the_age: int, expected: OutboxState
) -> None:
    transport = rig.transport()
    sender = rig.sender(transport, FakeLimits(send_limit_minute=1))
    await rig.row()
    await sender.send_next()
    held = await rig.row()
    await sender.send_next()
    assert (await rig.stored(held.id)).send_at == FAKE_NOW + timedelta(seconds=60)

    rig.clock.advance(ROW_STALE_AFTER_SECONDS + over_the_age)
    await sender.send_next()

    assert (await rig.stored(held.id)).state is expected
    assert len(transport.calls) == (2 if expected is OutboxState.SENT else 1)


async def test_a_row_moved_forward_by_a_long_flood_wait_is_stale_when_the_pause_ends(
    rig: Rig,
) -> None:
    row = await rig.row()
    wait = ROW_STALE_AFTER_SECONDS + 3600
    transport = rig.transport([SendFloodWait("FakeFloodWait", wait)])
    sender = rig.sender(transport)
    await sender.send_next()
    moved = await rig.stored(row.id)
    assert moved.send_at == FAKE_NOW + timedelta(seconds=wait + FLOOD_WAIT_MARGIN)
    assert moved.due_at == FAKE_NOW

    rig.clock.now = moved.send_at
    await sender.send_next()

    stored = await rig.stored(row.id)
    assert (stored.state, stored.cancel_reason) == (OutboxState.CANCELLED, CANCEL_REASON_STALE)
    assert stored.due_at == FAKE_NOW
    assert len(transport.calls) == 1


async def test_a_row_moved_by_a_short_flood_wait_with_a_fresh_due_time_is_sent(rig: Rig) -> None:
    row = await rig.row()
    transport = rig.transport([SendFloodWait("FakeFloodWait", 90)])
    sender = rig.sender(transport)
    await sender.send_next()
    moved = await rig.stored(row.id)
    assert moved.due_at == FAKE_NOW

    rig.clock.now = moved.send_at
    await sender.send_next()

    assert (await rig.stored(row.id)).state is OutboxState.SENT
    assert len(transport.calls) == 2


async def test_the_due_time_decides_before_the_guard_for_a_rescheduled_row(rig: Rig) -> None:
    transport = rig.transport()
    sender = rig.sender(transport, FakeLimits(send_limit_day=1))
    await rig.row()
    await sender.send_next()
    held = await rig.row()
    await sender.send_next()
    await rig.contacts.set_status(rig.contact.id, ContactStatus.PAUSED)

    rig.clock.advance(86400)
    await sender.send_next()

    assert (await rig.stored(held.id)).cancel_reason == CANCEL_REASON_STALE


async def test_a_future_due_time_is_never_stale_and_is_not_claimed_early(rig: Rig) -> None:
    planned = FAKE_NOW + timedelta(seconds=ROW_STALE_AFTER_SECONDS * 2)
    row = await rig.outbox.enqueue(rig.contact.id, TEXT, planned, OutboxSource.MANUAL_TEST)
    transport = rig.transport()
    sender = rig.sender(transport)

    assert await sender.send_next() is False
    rig.clock.now = planned
    await sender.send_next()

    assert (await rig.stored(row.id)).state is OutboxState.SENT


async def test_a_retry_gives_an_old_failed_row_a_new_due_time_and_it_is_sent(rig: Rig) -> None:
    row = await rig.row()
    transport = rig.transport([SendUncertain("FakeNetworkError")])
    sender = rig.sender(transport)
    await sender.send_next()
    rig.clock.advance(ROW_STALE_AFTER_SECONDS + 1)

    retried = await rig.outbox.retry_failed(row.id, rig.clock())
    assert retried.due_at == retried.send_at == rig.clock()
    await sender.send_next()

    assert (await rig.stored(row.id)).state is OutboxState.SENT
    assert len(transport.calls) == 2


async def test_a_retried_row_that_is_held_keeps_the_due_time_of_the_retry(rig: Rig) -> None:
    row = await rig.row()
    transport = rig.transport([SendUncertain("FakeNetworkError")])
    await rig.sender(transport).send_next()
    rig.clock.advance(ROW_STALE_AFTER_SECONDS + 1)
    retried_at = rig.clock()
    await rig.outbox.retry_failed(row.id, retried_at)
    await _store_outgoing(rig, FIRST_HELD_TG_ID, retried_at)

    await rig.sender(rig.transport(), FakeLimits(send_limit_minute=1)).send_next()

    stored = await rig.stored(row.id)
    assert stored.state is OutboxState.SCHEDULED
    assert stored.due_at == retried_at
    assert stored.send_at == retried_at + timedelta(seconds=60)


async def test_rows_that_waited_out_a_long_stop_are_stale_after_the_resume(rig: Rig) -> None:
    row = await rig.row()
    await rig.state.stop("FakeKind")
    transport = rig.transport()
    sender = rig.sender(transport)
    rig.clock.advance(ROW_STALE_AFTER_SECONDS + 1)
    assert await sender.send_next() is False

    await rig.state.resume()
    await sender.send_next()

    assert (await rig.stored(row.id)).cancel_reason == CANCEL_REASON_STALE
    assert transport.calls == []


async def test_a_flood_wait_pauses_the_whole_sender_and_moves_the_row_to_the_same_time(
    rig: Rig,
) -> None:
    flooded = await rig.row()
    waiting = await rig.row(rig.other)
    transport = rig.transport([SendFloodWait("FakeFloodWait", 30)])
    sender = rig.sender(transport)

    assert await sender.send_next() is True

    until = FAKE_NOW + timedelta(seconds=30 + FLOOD_WAIT_MARGIN)
    state = await rig.state.get()
    assert (state.paused_until, state.pause_reason) == (until, "FakeFloodWait")
    stored = await rig.stored(flooded.id)
    assert (stored.state, stored.send_at, stored.error_class) == (
        OutboxState.SCHEDULED,
        until,
        "FakeFloodWait",
    )

    assert await sender.send_next() is False
    rig.clock.advance(30 + FLOOD_WAIT_MARGIN - 1)
    assert await sender.send_next() is False
    assert len(transport.calls) == 1
    assert (await rig.stored(waiting.id)).state is OutboxState.SCHEDULED
    assert (await rig.stored(waiting.id)).claimed_at is None

    rig.clock.advance(1)
    assert await sender.send_next() is True
    assert await sender.send_next() is True
    assert (await rig.stored(flooded.id)).state is OutboxState.SENT
    assert (await rig.stored(waiting.id)).state is OutboxState.SENT
    assert len(transport.calls) == 3


async def test_a_flood_wait_from_telethon_pauses_the_sender(rig: Rig) -> None:
    await rig.row()
    raw = _Raw(FloodWaitError(request=None, capture=30))

    await rig.sender(TelethonAdapter(raw)).send_next()

    state = await rig.state.get()
    assert state.paused_until == FAKE_NOW + timedelta(seconds=30 + FLOOD_WAIT_MARGIN)
    assert state.pause_reason == "FloodWaitError"


@pytest.mark.parametrize(
    ("seconds", "notices"),
    [
        (int(FLOOD_WAIT_NOTIFY_ABOVE) - 1, 0),
        (int(FLOOD_WAIT_NOTIFY_ABOVE), 0),
        (int(FLOOD_WAIT_NOTIFY_ABOVE) + 1, 1),
    ],
)
async def test_a_pause_notifies_only_above_the_threshold(
    rig: Rig, seconds: int, notices: int
) -> None:
    await rig.row()

    await rig.sender(rig.transport([SendFloodWait("FakeFloodWait", seconds)])).send_next()

    assert len(rig.notifier.texts) == notices


async def test_every_new_pause_notifies_once_and_the_paused_ticks_do_not(rig: Rig) -> None:
    await rig.row()
    transport = rig.transport(
        [SendFloodWait("FakeFloodWait", 100), SendFloodWait("FakeFloodWait", 200)]
    )
    sender = rig.sender(transport)

    await sender.send_next()
    for _ in range(5):
        rig.clock.advance(1)
        await sender.send_next()
    assert len(rig.notifier.texts) == 1

    rig.clock.advance(100)
    await sender.send_next()
    assert len(rig.notifier.texts) == 2
    assert len(transport.calls) == 2


async def test_a_pause_survives_a_new_sender_instance(rig: Rig) -> None:
    await rig.row()
    await rig.row()
    await rig.sender(rig.transport([SendFloodWait("FakeFloodWait", 100)])).send_next()
    transport = rig.transport()
    restarted = rig.sender(transport)

    assert await restarted.send_next() is False
    assert transport.calls == []

    rig.clock.advance(100 + FLOOD_WAIT_MARGIN)
    assert await restarted.send_next() is True
    assert len(transport.calls) == 1


async def test_a_shorter_flood_wait_never_shortens_the_stored_longer_pause(rig: Rig) -> None:
    long_until = FAKE_NOW + timedelta(seconds=1000)
    await rig.state.pause_until(long_until, "FakeLongWait", now=FAKE_NOW)
    row = await rig.row()
    claimed = await rig.outbox.claim_due(FAKE_NOW)
    assert claimed is not None
    sender = rig.sender(rig.transport())

    await sender._handle_failure(claimed, SendFloodWait("FakeShortWait", 100))

    state = await rig.state.get()
    assert (state.paused_until, state.pause_reason) == (long_until, "FakeLongWait")
    assert rig.notifier.texts == []
    rig.clock.advance(100 + FLOOD_WAIT_MARGIN)
    assert (await rig.stored(row.id)).state is OutboxState.SCHEDULED
    assert await sender.send_next() is False


async def test_a_pause_ends_exactly_at_its_time(rig: Rig) -> None:
    until = FAKE_NOW + timedelta(seconds=10)
    await rig.state.pause_until(until, "FakeFloodWait", now=FAKE_NOW)
    await rig.row()
    sender = rig.sender(rig.transport())

    rig.clock.advance(9)
    assert await sender.send_next() is False
    rig.clock.advance(1)
    assert await sender.send_next() is True


async def test_the_running_loop_claims_nothing_during_a_pause_and_goes_on_after_it(
    rig: Rig,
) -> None:
    first = await rig.row()
    second = await rig.row(rig.other)
    transport = rig.transport([SendFloodWait("FakeFloodWait", 100)])
    watched_until = FAKE_NOW + timedelta(seconds=5)
    past_the_pause = watched_until + timedelta(seconds=200)
    calls_during_the_pause: list[int] = []
    claims_during_the_pause: list[OutboxState] = []

    async def advance(seconds: float) -> bool:
        if seconds != SENDER_POLL_INTERVAL:
            return False
        if rig.clock() < watched_until:
            calls_during_the_pause.append(len(transport.calls))
            claims_during_the_pause.append((await rig.stored(second.id)).state)
            rig.clock.advance(1)
            return False
        if rig.clock() < past_the_pause:
            rig.clock.advance(200)
            return False
        return (await rig.stored(first.id)).state is OutboxState.SENT and (
            await rig.stored(second.id)
        ).state is OutboxState.SENT

    loop = CappedLoop(advance)
    sender = OutboxSender(
        rig.outbox,
        rig.contacts,
        rig.state,
        SendLimiter(rig.messages, FakeLimits()),
        free_schedule(),
        transport,
        rig.notifier,
        SendGuard(_Switch()),
        OUTBOX_NOTICES,
        clock=rig.clock,
        sleep=loop.sleep,
    )

    await loop.run(sender.poll_forever())

    assert calls_during_the_pause == [1] * 5
    assert claims_during_the_pause == [OutboxState.SCHEDULED] * 5
    assert (await rig.stored(first.id)).state is OutboxState.SENT
    assert (await rig.stored(second.id)).state is OutboxState.SENT


@pytest.mark.parametrize("kind", ACCOUNT_KINDS)
async def test_an_account_problem_stops_everything_with_one_notice(rig: Rig, kind: str) -> None:
    hit = await rig.row()
    waiting = await rig.row(rig.other)
    transport = rig.transport([SendAccountBlocked(kind)])
    sender = rig.sender(transport)

    assert await sender.send_next() is True

    state = await rig.state.get()
    assert (state.stopped, state.stop_reason) == (True, kind)
    stored = await rig.stored(hit.id)
    assert (stored.state, stored.error_class) == (OutboxState.FAILED, kind)
    assert rig.notifier.texts == [OUTBOX_NOTICES.account_stopped.format(kind=kind)]
    assert kind in rig.notifier.texts[0]
    assert "outbox resume --yes" in rig.notifier.texts[0]

    rig.clock.advance(3600)
    assert await sender.send_next() is False
    assert len(transport.calls) == 1
    other_row = await rig.stored(waiting.id)
    assert (other_row.state, other_row.error_class) == (OutboxState.SCHEDULED, None)
    assert await rig.status() is ContactStatus.ACTIVE
    assert len(rig.notifier.texts) == 1


@pytest.mark.parametrize(
    "error",
    [
        PeerFloodError(request=None),
        UserDeactivatedError(request=None),
        UserDeactivatedBanError(request=None),
        AuthKeyUnregisteredError(request=None),
        AuthKeyInvalidError(request=None),
        SessionRevokedError(request=None),
        SessionExpiredError(request=None),
        UserRestrictedError(request=None),
        FrozenMethodInvalidError(request=None),
        AuthKeyDuplicatedError(request=None),
        AuthKeyPermEmptyError(request=None),
        ActiveUserRequiredError(request=None),
        SessionPasswordNeededError(request=None),
        UnauthorizedError(request=None, message="FAKE_UNAUTHORIZED"),
    ],
)
async def test_each_telethon_account_error_stops_the_sender_through_the_adapter(
    rig: Rig, error: Exception
) -> None:
    row = await rig.row()
    pending = await rig.row()
    raw = _Raw(error)

    await rig.sender(TelethonAdapter(raw)).send_next()

    name = type(error).__name__
    assert (await rig.state.get()).stop_reason == name
    stored = await rig.stored(row.id)
    assert (stored.state, stored.error_class) == (OutboxState.FAILED, name)
    assert raw.send_calls == 1
    assert rig.notifier.texts == [OUTBOX_NOTICES.account_stopped.format(kind=name)]
    assert await rig.status() is ContactStatus.ACTIVE
    assert (await rig.stored(pending.id)).state is OutboxState.SCHEDULED


@pytest.mark.parametrize(
    "error",
    [
        UserPrivacyRestrictedError(request=None),
        ChatWriteForbiddenError(request=None),
        ForbiddenError(request=None, message="FAKE_NEW_REFUSAL"),
    ],
)
async def test_an_ordinary_forbidden_error_pauses_the_contact_and_does_not_stop_the_sender(
    rig: Rig, error: Exception
) -> None:
    row = await rig.row()

    await rig.sender(TelethonAdapter(_Raw(error))).send_next()

    state = await rig.state.get()
    assert (state.stopped, state.stop_reason) == (False, None)
    stored = await rig.stored(row.id)
    assert (stored.state, stored.error_class) == (OutboxState.FAILED, type(error).__name__)
    assert await rig.status() is ContactStatus.PAUSED


async def test_a_stop_survives_a_restart_and_only_resume_clears_it(rig: Rig) -> None:
    await rig.row()
    waiting = await rig.row()
    await rig.sender(rig.transport([SendAccountBlocked("FakeKind")])).send_next()
    transport = rig.transport()
    restarted = rig.sender(transport)

    await restarted.sweep_interrupted()
    await restarted.fail_stale()
    assert await restarted.send_next() is False
    assert transport.calls == []
    assert len(rig.notifier.texts) == 1

    rig.clock.advance(3600)
    assert await restarted.send_next() is False

    await rig.state.resume(keep_stop=True)
    assert await restarted.send_next() is False
    assert transport.calls == []

    await rig.state.resume()
    assert await restarted.send_next() is True
    assert (await rig.stored(waiting.id)).state is OutboxState.SENT


async def test_a_second_stop_gives_no_second_notice_and_keeps_the_first_reason(rig: Rig) -> None:
    row = await rig.row()
    await rig.state.stop("FakeFirstKind")
    claimed = await rig.outbox.claim_due(FAKE_NOW)
    assert claimed is not None

    await rig.sender(rig.transport())._handle_failure(claimed, SendAccountBlocked("FakeSecondKind"))

    assert rig.notifier.texts == []
    assert (await rig.state.get()).stop_reason == "FakeFirstKind"
    assert (await rig.stored(row.id)).error_class == "FakeSecondKind"


async def test_a_storage_failure_while_failing_the_row_still_tells_the_owner_about_the_stop(
    rig: Rig,
) -> None:
    row = await rig.row()

    class _BrokenFail(SqlOutboxRepository):
        async def mark_failed(self, row_id: int, error_class: str) -> ScheduledMessage:
            raise RuntimeError("fake storage failure")

    repository = rig.outbox
    rig.outbox = _BrokenFail(repository._session_factory)

    with pytest.raises(RuntimeError):
        await rig.sender(rig.transport([SendAccountBlocked("FakeKind")])).send_next()

    assert (await rig.state.get()).stopped is True
    assert len(rig.notifier.texts) == 1
    assert (await rig.stored(row.id)).state is OutboxState.SENDING

    rig.outbox = repository
    rig.clock.advance(SENDING_STALE_AFTER)
    await rig.sender(rig.transport()).fail_stale()
    assert (await rig.stored(row.id)).error_class == ERROR_INTERRUPTED


async def test_a_pause_and_a_stop_together_block_until_both_are_gone(rig: Rig) -> None:
    await rig.row()
    await rig.state.stop("FakeKind")
    await rig.state.pause_until(FAKE_NOW + timedelta(seconds=10), "FakeFloodWait", now=FAKE_NOW)
    sender = rig.sender(rig.transport())

    rig.clock.advance(11)
    assert await sender.send_next() is False


@pytest.mark.parametrize(
    "failure",
    [
        SendRejected("FakeBlockedError", RejectionReason.RECIPIENT),
        SendRejected(PEER_NOT_FOUND, RejectionReason.RECIPIENT),
    ],
)
async def test_a_recipient_rejection_fails_the_row_pauses_the_contact_and_cancels_the_rest(
    rig: Rig, failure: SendRejected
) -> None:
    hit = await rig.row()
    pending = await rig.outbox.enqueue(
        rig.contact.id, TEXT, FAKE_NOW + timedelta(days=1), OutboxSource.MANUAL_TEST
    )
    foreign = await rig.row(rig.other)
    transport = rig.transport([failure])
    sender = rig.sender(transport)

    await sender.send_next()

    stored = await rig.stored(hit.id)
    assert (stored.state, stored.error_class) == (OutboxState.FAILED, failure.error_class)
    assert await rig.status() is ContactStatus.PAUSED
    paused = await rig.contacts.get_by_id(rig.contact.id)
    assert paused is not None
    assert paused.pause_reason is PauseReason.SEND_REJECTED
    cancelled = await rig.stored(pending.id)
    assert (cancelled.state, cancelled.cancel_reason) == (
        OutboxState.CANCELLED,
        CANCEL_REASON_SEND_REJECTED,
    )
    assert (await rig.stored(foreign.id)).state is OutboxState.SCHEDULED
    assert await rig.status(rig.other) is ContactStatus.ACTIVE
    assert rig.notifier.texts == [
        OUTBOX_NOTICES.contact_paused.format(contact_id=rig.contact.id, error=failure.error_class)
    ]
    assert set(re.findall(r"\d+", rig.notifier.texts[0])) == {str(rig.contact.id)}

    await sender.send_next()
    assert (await rig.stored(foreign.id)).state is OutboxState.SENT
    assert len(transport.calls) == 2
    assert len(rig.notifier.texts) == 1
    assert (await rig.state.get()).stopped is False


@pytest.mark.parametrize(
    "error", [UserIsBlockedError(request=None), UserPrivacyRestrictedError(request=None)]
)
async def test_a_recipient_refusal_from_telethon_pauses_the_contact(
    rig: Rig, error: Exception
) -> None:
    row = await rig.row()

    await rig.sender(TelethonAdapter(_Raw(error))).send_next()

    assert (await rig.stored(row.id)).error_class == type(error).__name__
    assert await rig.status() is ContactStatus.PAUSED


async def test_a_peer_that_cannot_be_resolved_pauses_the_contact(rig: Rig) -> None:
    row = await rig.row()
    raw = _Raw(None, peer_error=ValueError("no such peer"))

    await rig.sender(TelethonAdapter(raw)).send_next()

    assert (await rig.stored(row.id)).error_class == PEER_NOT_FOUND
    assert await rig.status() is ContactStatus.PAUSED
    assert raw.send_calls == 0


async def test_a_content_rejection_fails_only_the_row(rig: Rig) -> None:
    hit = await rig.row()
    pending = await rig.outbox.enqueue(
        rig.contact.id, TEXT, FAKE_NOW + timedelta(days=1), OutboxSource.MANUAL_TEST
    )
    raw = _Raw(MessageTooLongError(request=None))

    await rig.sender(TelethonAdapter(raw)).send_next()

    stored = await rig.stored(hit.id)
    assert (stored.state, stored.error_class) == (OutboxState.FAILED, "MessageTooLongError")
    assert await rig.status() is ContactStatus.ACTIVE
    assert (await rig.stored(pending.id)).state is OutboxState.SCHEDULED
    assert rig.notifier.texts == [
        OUTBOX_NOTICES.failed.format(
            row_id=hit.id, contact_id=rig.contact.id, error="MessageTooLongError"
        )
    ]


@pytest.mark.parametrize("status", [ContactStatus.PAUSED, ContactStatus.IGNORED])
async def test_a_recipient_rejection_is_harmless_when_the_status_moved_during_the_send(
    rig: Rig, status: ContactStatus
) -> None:
    row = await rig.row()
    claimed = await rig.outbox.claim_due(FAKE_NOW)
    assert claimed is not None
    await rig.contacts.set_status(rig.contact.id, status)

    await rig.sender(rig.transport())._handle_failure(
        claimed, SendRejected("FakeBlockedError", RejectionReason.RECIPIENT)
    )

    assert await rig.status() is status
    assert (await rig.stored(row.id)).state is OutboxState.FAILED
    assert len(rig.notifier.texts) == 1


async def test_a_storage_failure_while_pausing_leaves_the_row_for_the_stale_check(rig: Rig) -> None:
    row = await rig.row()

    class _BrokenStatus(SqlContactRepository):
        async def set_status(
            self,
            contact_id: int,
            status: ContactStatus,
            *,
            mode_id: int | None = None,
            pause_reason: PauseReason | None = None,
            reply_after: datetime | None = None,
        ) -> StatusChange:
            raise RuntimeError("fake storage failure")

    contacts = rig.contacts
    rig.contacts = _BrokenStatus(contacts._session_factory)

    with pytest.raises(RuntimeError):
        await rig.sender(
            rig.transport([SendRejected("FakeBlockedError", RejectionReason.RECIPIENT)])
        ).send_next()

    assert (await rig.stored(row.id)).state is OutboxState.SENDING
    assert rig.notifier.texts == []
    rig.contacts = contacts
    rig.clock.advance(SENDING_STALE_AFTER)
    await rig.sender(rig.transport()).fail_stale()
    assert (await rig.stored(row.id)).error_class == ERROR_INTERRUPTED
    assert len(rig.notifier.texts) == 1


async def test_a_retried_row_of_a_rejected_contact_is_cancelled_by_the_guard(rig: Rig) -> None:
    row = await rig.row()
    transport = rig.transport([SendRejected("FakeBlockedError", RejectionReason.RECIPIENT)])
    sender = rig.sender(transport)
    await sender.send_next()

    await rig.outbox.retry_failed(row.id, rig.clock())
    await sender.send_next()

    assert (await rig.stored(row.id)).state is OutboxState.CANCELLED
    assert len(transport.calls) == 1


@pytest.mark.parametrize(
    "failure",
    [
        SendUncertain("FakeNetworkError"),
        SendFailure("FakeFutureFailure"),
        RuntimeError("fake detail"),
    ],
)
async def test_an_uncertain_outcome_neither_pauses_nor_stops_and_is_never_sent_again(
    rig: Rig, failure: Exception
) -> None:
    row = await rig.row()
    transport = rig.transport([failure])
    sender = rig.sender(transport)

    await sender.send_next()
    rig.clock.advance(ROW_STALE_AFTER_SECONDS * 2)
    await sender.send_next()
    await sender.sweep_interrupted()
    await sender.fail_stale()

    stored = await rig.stored(row.id)
    assert (stored.state, stored.error_class) == (OutboxState.FAILED, ERROR_UNCERTAIN)
    state = await rig.state.get()
    assert (state.stopped, state.paused_until) == (False, None)
    assert await rig.status() is ContactStatus.ACTIVE
    assert len(transport.calls) == 1
    assert len(rig.notifier.texts) == 1


async def test_the_sweeps_still_run_while_the_sender_is_stopped_and_send_nothing(rig: Rig) -> None:
    row = await rig.row()
    await rig.outbox.claim_due(FAKE_NOW)
    await rig.state.stop("FakeKind")
    transport = rig.transport()
    sender = rig.sender(transport)

    await sender.sweep_interrupted()

    stored = await rig.stored(row.id)
    assert (stored.state, stored.error_class) == (OutboxState.FAILED, ERROR_INTERRUPTED)
    assert transport.calls == []


async def test_a_state_that_cannot_be_read_claims_nothing(rig: Rig) -> None:
    row = await rig.row()

    class _BrokenState(SqlSenderStateRepository):
        async def get(self) -> SenderState:
            raise RuntimeError("fake storage failure")

    rig.state = _BrokenState(rig.state._session_factory)
    transport = rig.transport()

    with pytest.raises(RuntimeError):
        await rig.sender(transport).send_next()

    assert (await rig.stored(row.id)).state is OutboxState.SCHEDULED
    assert transport.calls == []


async def test_a_limit_that_cannot_be_read_never_sends_and_the_stale_check_reports_the_row(
    rig: Rig,
) -> None:
    row = await rig.row()

    class _BrokenTimes(SqlMessageRepository):
        async def outgoing_times(
            self, since: datetime, limit: int, contact_id: int | None = None
        ) -> list[datetime]:
            raise RuntimeError("fake storage failure")

    messages = rig.messages
    rig.messages = _BrokenTimes(messages._session_factory)
    transport = rig.transport()

    with pytest.raises(RuntimeError):
        await rig.sender(transport).send_next()

    assert transport.calls == []
    assert (await rig.stored(row.id)).state is OutboxState.SENDING
    rig.messages = messages
    rig.clock.advance(SENDING_STALE_AFTER)
    await rig.sender(transport).fail_stale()
    assert (await rig.stored(row.id)).error_class == ERROR_INTERRUPTED


async def _all_sent(rig: Rig, count: int) -> bool:
    return len(await rig.outbox.list_rows(OutboxState.SENT, count)) >= count


async def test_rows_stay_strictly_sequential_under_the_limits(rig: Rig) -> None:
    for _ in range(4):
        await rig.row()
    transport = rig.transport()
    transport.delay = 0.005
    task = asyncio.create_task(rig.sender(transport, timings=FAST_POLL).poll_forever())

    await eventually(lambda: _all_sent(rig, 4))
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert transport.max_active == 1
    assert len(transport.calls) == 4


async def test_two_loops_never_double_send_under_limits_and_a_flood_wait(
    rig: Rig, session_factory: SessionFactory
) -> None:
    total = 8
    rows = [
        await rig.outbox.enqueue(rig.contact.id, f"{TEXT} {n}", FAKE_NOW, OutboxSource.MANUAL_TEST)
        for n in range(total)
    ]
    transport = rig.transport([SendFloodWait("FakeFloodWait", 1)])
    transport.delay = 0.001
    second = SqlOutboxRepository(session_factory)
    other_rig = Rig(
        second,
        rig.contacts,
        rig.messages,
        rig.state,
        rig.contact,
        rig.other,
        rig.clock,
        rig.notifier,
    )

    async def pushes_the_clock() -> None:
        while True:
            rig.clock.advance(5)
            await asyncio.sleep(0.002)

    tasks = [
        asyncio.create_task(rig.sender(transport, timings=FAST_POLL).poll_forever()),
        asyncio.create_task(other_rig.sender(transport, timings=FAST_POLL).poll_forever()),
        asyncio.create_task(pushes_the_clock()),
    ]

    await eventually(lambda: _all_sent(rig, total))
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    sent_rows = [await rig.stored(row.id) for row in rows]
    assert {row.state for row in sent_rows} == {OutboxState.SENT}
    attempts_per_text = Counter(text for _, text in transport.calls)
    assert sorted(attempts_per_text.values()) == [1] * (total - 1) + [2]
    assert attempts_per_text[transport.calls[0][1]] == 2
    assert len(await rig.messages.list_last(rig.contact.id, total + 1)) == total


async def test_every_notice_and_log_of_the_new_paths_carries_no_personal_data(
    rig: Rig, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    caplog.set_level(logging.DEBUG)
    transport = rig.transport(
        [
            SendFloodWait("FakeFloodWait", 3600),
            SendRejected("FakeBlockedError", RejectionReason.CONTENT),
            SendRejected("FakeBlockedError", RejectionReason.RECIPIENT),
            SendAccountBlocked("FakeKind"),
        ]
    )
    limits = FakeLimits(send_limit_hour=2, send_limit_contact_hour=2)
    sender = rig.sender(transport, limits)
    for _ in range(3):
        await rig.row()
    await rig.outbox.enqueue(
        rig.contact.id, TEXT, FAKE_NOW + timedelta(days=5), OutboxSource.MANUAL_TEST
    )

    await sender.send_next()
    rig.clock.advance(4000)
    await sender.send_next()
    await sender.send_next()
    await rig.outbox.retry_failed(
        (await rig.outbox.list_rows(OutboxState.FAILED, 1))[0].id, rig.clock()
    )
    await rig.contacts.set_status(rig.contact.id, ContactStatus.ACTIVE)
    await sender.send_next()

    captured = capsys.readouterr()
    assert len(rig.notifier.texts) >= 3
    haystack = "\n".join(
        [
            caplog.text,
            captured.out,
            captured.err,
            *rig.notifier.texts,
            *(str(record.__dict__) for record in caplog.records),
        ]
    )
    for marker in (TEXT, USERNAME, DISPLAY_NAME, FAKE_TELEGRAM_ID, OTHER_TG_ID):
        assert_absent(marker, haystack)


async def test_a_limit_notice_and_its_logs_carry_no_personal_data(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    sender = rig.sender(rig.transport(), FakeLimits(send_limit_contact_day=1, send_limit_hour=1))
    await rig.row()
    await sender.send_next()
    await rig.row()
    await sender.send_next()
    rig.clock.advance(ROW_STALE_AFTER_SECONDS * 5)
    await sender.send_next()

    assert rig.notifier.texts
    haystack = "\n".join(
        [caplog.text, *rig.notifier.texts, *(str(record.__dict__) for record in caplog.records)]
    )
    for marker in (TEXT, USERNAME, DISPLAY_NAME, FAKE_TELEGRAM_ID):
        assert_absent(marker, haystack)


def _stale_notice(count: int, *contacts: Contact) -> str:
    return OUTBOX_NOTICES.stale_summary.format(
        count=count, contact_ids=", ".join(f"#{contact.id}" for contact in contacts)
    )


async def _stale_row(rig: Rig, contact: Contact | None = None) -> ScheduledMessage:
    target = contact if contact is not None else rig.contact
    due = rig.clock() - timedelta(seconds=ROW_STALE_AFTER_SECONDS + 1)
    return await rig.outbox.enqueue(target.id, TEXT, due, OutboxSource.MANUAL_TEST)


def test_the_stale_and_stop_intervals_are_named_constants_of_the_documented_size() -> None:
    assert STALE_NOTICE_INTERVAL_SECONDS == 3600
    assert STOP_REMINDER_INTERVAL_SECONDS == 6 * 3600
    assert STALE_NOTICE_CONTACT_IDS_MAX == 5
    timings = SenderTimings()
    assert timings.stale_notice_interval == STALE_NOTICE_INTERVAL_SECONDS
    assert timings.stop_reminder_interval == STOP_REMINDER_INTERVAL_SECONDS


async def test_the_first_stale_cancel_sends_one_notice_with_the_count_and_the_contact_id(
    rig: Rig,
) -> None:
    row = await rig.row()
    rig.clock.advance(ROW_STALE_AFTER_SECONDS + 1)

    await rig.sender(rig.transport()).send_next()

    assert (await rig.stored(row.id)).cancel_reason == CANCEL_REASON_STALE
    assert rig.notifier.texts == [_stale_notice(1, rig.contact)]


async def test_a_row_that_is_not_stale_gives_no_stale_notice(rig: Rig) -> None:
    await rig.row()
    rig.clock.advance(ROW_STALE_AFTER_SECONDS)

    await rig.sender(rig.transport()).send_next()

    assert rig.notifier.texts == []


async def test_several_stale_cancels_in_the_hour_become_one_notice_with_their_sum(
    rig: Rig,
) -> None:
    sender = rig.sender(rig.transport())
    await _stale_row(rig)
    await sender.send_next()
    assert rig.notifier.texts == [_stale_notice(1, rig.contact)]

    for contact in (rig.contact, rig.other, rig.other):
        rig.clock.advance(10)
        await _stale_row(rig, contact)
        await sender.send_next()
    assert rig.notifier.texts == [_stale_notice(1, rig.contact)]

    rig.clock.advance(STALE_NOTICE_INTERVAL_SECONDS - 30 - 1)
    await sender.send_next()
    assert len(rig.notifier.texts) == 1

    rig.clock.advance(1)
    await sender.send_next()
    assert rig.notifier.texts == [
        _stale_notice(1, rig.contact),
        _stale_notice(3, rig.contact, rig.other),
    ]

    rig.clock.advance(STALE_NOTICE_INTERVAL_SECONDS * 3)
    await sender.send_next()
    assert len(rig.notifier.texts) == 2


async def test_the_interval_of_the_stale_notice_counts_from_the_previous_notice(rig: Rig) -> None:
    sender = rig.sender(rig.transport())
    await _stale_row(rig)
    await sender.send_next()

    rig.clock.advance(STALE_NOTICE_INTERVAL_SECONDS - 1)
    await _stale_row(rig)
    await sender.send_next()
    assert len(rig.notifier.texts) == 1

    rig.clock.advance(1)
    await sender.send_next()
    assert rig.notifier.texts[-1] == _stale_notice(1, rig.contact)
    assert len(rig.notifier.texts) == 2


async def test_a_stale_cancel_after_a_quiet_hour_is_told_at_once(rig: Rig) -> None:
    sender = rig.sender(rig.transport())
    await _stale_row(rig)
    await sender.send_next()

    rig.clock.advance(STALE_NOTICE_INTERVAL_SECONDS)
    await _stale_row(rig, rig.other)
    await sender.send_next()

    assert rig.notifier.texts == [
        _stale_notice(1, rig.contact),
        _stale_notice(1, rig.other),
    ]


async def test_the_stale_notice_lists_at_most_five_contact_ids_but_counts_every_cancel(
    rig: Rig, modes: SqlModeRepository
) -> None:
    sender = rig.sender(rig.transport())
    contacts = [
        await stored_contact(
            rig.contacts, modes, ContactStatus.ACTIVE, tg_id=FAKE_TELEGRAM_ID + 10 + n
        )
        for n in range(STALE_NOTICE_CONTACT_IDS_MAX + 2)
    ]
    await _stale_row(rig)
    await sender.send_next()
    for contact in contacts:
        await _stale_row(rig, contact)
        await sender.send_next()
    rig.clock.advance(STALE_NOTICE_INTERVAL_SECONDS)

    await sender.send_next()

    assert rig.notifier.texts[-1] == _stale_notice(
        len(contacts), *contacts[:STALE_NOTICE_CONTACT_IDS_MAX]
    )
    assert f"#{contacts[STALE_NOTICE_CONTACT_IDS_MAX].id}" not in rig.notifier.texts[-1]


async def test_waiting_stale_cancels_are_reported_while_the_sender_is_stopped(rig: Rig) -> None:
    sender = rig.sender(rig.transport())
    await _stale_row(rig)
    await sender.send_next()
    await _stale_row(rig, rig.other)
    await sender.send_next()
    await rig.state.stop("FakeKind")
    rig.clock.advance(STALE_NOTICE_INTERVAL_SECONDS)

    assert await sender.send_next() is False

    assert rig.notifier.texts[-1] == _stale_notice(1, rig.other)


@pytest.mark.parametrize(
    "notifier",
    [HangingNotifier(), RecordingNotifier(error=ConnectionError(NOTIFIER_DETAIL))],
    ids=["hanging", "raising"],
)
async def test_a_bad_notifier_does_not_change_a_stale_cancel(
    rig: Rig, notifier: OwnerNotifier, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    row = await _stale_row(rig)
    transport = rig.transport()
    sender = rig.sender(
        transport, timings=SenderTimings(notify_deadline=FAST_NOTIFY_DEADLINE), notifier=notifier
    )

    assert await sender.send_next() is True

    stored = await rig.stored(row.id)
    assert (stored.state, stored.cancel_reason) == (OutboxState.CANCELLED, CANCEL_REASON_STALE)
    assert transport.calls == []
    assert len(_notice_failures(caplog)) == 1
    assert NOTIFIER_DETAIL not in caplog.text


async def test_a_failed_stale_notice_is_not_repeated_and_the_next_one_counts_new_cancels_only(
    rig: Rig,
) -> None:
    notifier = RecordingNotifier(error=ConnectionError(NOTIFIER_DETAIL))
    sender = rig.sender(rig.transport(), notifier=notifier)
    await _stale_row(rig)
    await sender.send_next()
    rig.clock.advance(STALE_NOTICE_INTERVAL_SECONDS)
    await sender.send_next()
    assert len(notifier.texts) == 1

    await _stale_row(rig, rig.other)
    await sender.send_next()
    assert notifier.texts[-1] == _stale_notice(1, rig.other)


async def test_the_stale_notice_carries_no_personal_data(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    sender = rig.sender(rig.transport())
    await _stale_row(rig)
    await sender.send_next()
    await _stale_row(rig, rig.other)
    await sender.send_next()
    rig.clock.advance(STALE_NOTICE_INTERVAL_SECONDS)
    await sender.send_next()

    assert len(rig.notifier.texts) == 2
    haystack = "\n".join(
        [caplog.text, *rig.notifier.texts, *(str(record.__dict__) for record in caplog.records)]
    )
    for marker in (TEXT, USERNAME, DISPLAY_NAME, FAKE_TELEGRAM_ID, OTHER_TG_ID):
        assert_absent(marker, haystack)
    assert set(re.findall(r"\d+", rig.notifier.texts[0])) <= {"1", "6", "5", str(rig.contact.id)}


async def _stopped_sender(rig: Rig, notifier: OwnerNotifier | None = None) -> OutboxSender:
    await rig.row()
    sender = rig.sender(rig.transport([SendAccountBlocked("FakeKind")]), notifier=notifier)
    await sender.send_next()
    return sender


async def test_a_stopped_sender_reminds_the_owner_every_six_hours_with_the_kind(rig: Rig) -> None:
    sender = await _stopped_sender(rig)
    stop_notice = OUTBOX_NOTICES.account_stopped.format(kind="FakeKind")
    reminder = OUTBOX_NOTICES.stop_reminder.format(kind="FakeKind")
    assert rig.notifier.texts == [stop_notice]

    rig.clock.advance(STOP_REMINDER_INTERVAL_SECONDS - 1)
    await sender.send_next()
    assert rig.notifier.texts == [stop_notice]

    rig.clock.advance(1)
    await sender.send_next()
    await sender.send_next()
    assert rig.notifier.texts == [stop_notice, reminder]

    rig.clock.advance(STOP_REMINDER_INTERVAL_SECONDS - 1)
    await sender.send_next()
    assert len(rig.notifier.texts) == 2

    rig.clock.advance(1)
    await sender.send_next()
    assert rig.notifier.texts == [stop_notice, reminder, reminder]


async def test_the_stop_reminder_names_the_kind_and_the_resume_command_and_no_ids(
    rig: Rig,
) -> None:
    sender = await _stopped_sender(rig)
    rig.clock.advance(STOP_REMINDER_INTERVAL_SECONDS)

    await sender.send_next()

    reminder = rig.notifier.texts[-1]
    assert "FakeKind" in reminder
    assert "python -m cyrano outbox resume --yes" in reminder
    assert re.findall(r"\d+", reminder) == []


async def test_no_reminder_comes_after_the_resume(rig: Rig) -> None:
    sender = await _stopped_sender(rig)
    rig.clock.advance(STOP_REMINDER_INTERVAL_SECONDS - 1)
    await sender.send_next()
    await rig.state.resume(keep_stop=False)
    count = len(rig.notifier.texts)

    for _ in range(4):
        rig.clock.advance(STOP_REMINDER_INTERVAL_SECONDS)
        await sender.send_next()

    assert len(rig.notifier.texts) == count


async def test_a_new_stop_after_a_resume_starts_its_own_reminder_clock(rig: Rig) -> None:
    sender = await _stopped_sender(rig)
    rig.clock.advance(STOP_REMINDER_INTERVAL_SECONDS - 10)
    await sender.send_next()
    await rig.state.resume(keep_stop=False)
    await sender.send_next()
    await rig.row()
    rig.clock.advance(10)
    sender_two = rig.sender(rig.transport([SendAccountBlocked("OtherKind")]))
    await sender_two.send_next()
    before = len(rig.notifier.texts)

    rig.clock.advance(STOP_REMINDER_INTERVAL_SECONDS - 1)
    await sender_two.send_next()
    assert len(rig.notifier.texts) == before

    rig.clock.advance(1)
    await sender_two.send_next()
    assert rig.notifier.texts[-1] == OUTBOX_NOTICES.stop_reminder.format(kind="OtherKind")


async def test_a_failing_notifier_does_not_break_the_stop_reminder_and_it_tries_again_later(
    rig: Rig,
) -> None:
    notifier = RecordingNotifier(error=ConnectionError(NOTIFIER_DETAIL))
    sender = await _stopped_sender(rig, notifier)
    assert len(notifier.texts) == 1

    rig.clock.advance(STOP_REMINDER_INTERVAL_SECONDS)
    assert await sender.send_next() is False
    assert len(notifier.texts) == 2
    assert (await rig.state.get()).stopped is True

    await sender.send_next()
    rig.clock.advance(STOP_REMINDER_INTERVAL_SECONDS - 1)
    await sender.send_next()
    assert len(notifier.texts) == 2

    rig.clock.advance(1)
    await sender.send_next()
    assert len(notifier.texts) == 3
    assert notifier.texts[-1] == OUTBOX_NOTICES.stop_reminder.format(kind="FakeKind")


async def test_a_hanging_notifier_does_not_hold_back_the_stop_reminder_loop(rig: Rig) -> None:
    await rig.state.stop("FakeKind")
    notifier = HangingNotifier()
    sender = rig.sender(
        rig.transport(),
        timings=SenderTimings(notify_deadline=FAST_NOTIFY_DEADLINE),
        notifier=notifier,
    )
    await sender.send_next()
    rig.clock.advance(STOP_REMINDER_INTERVAL_SECONDS)

    assert await sender.send_next() is False

    assert (notifier.calls, notifier.cancelled) == (1, 1)


async def test_a_pause_alone_never_gets_a_reminder(rig: Rig) -> None:
    far = FAKE_NOW + timedelta(seconds=STOP_REMINDER_INTERVAL_SECONDS * 10)
    await rig.state.pause_until(far, "FakeFloodWait", now=FAKE_NOW)
    sender = rig.sender(rig.transport())

    for _ in range(4):
        await sender.send_next()
        rig.clock.advance(STOP_REMINDER_INTERVAL_SECONDS)

    assert rig.notifier.texts == []


async def test_a_pause_on_top_of_a_stop_still_reminds_about_the_stop(rig: Rig) -> None:
    await rig.state.stop("FakeKind")
    far = FAKE_NOW + timedelta(seconds=STOP_REMINDER_INTERVAL_SECONDS * 10)
    await rig.state.pause_until(far, "FakeFloodWait", now=FAKE_NOW)
    sender = rig.sender(rig.transport())
    await sender.send_next()

    rig.clock.advance(STOP_REMINDER_INTERVAL_SECONDS)
    await sender.send_next()

    assert rig.notifier.texts == [OUTBOX_NOTICES.stop_reminder.format(kind="FakeKind")]


async def test_a_restart_during_a_stop_starts_the_reminder_clock_without_a_notice(
    rig: Rig,
) -> None:
    await _stopped_sender(rig)
    rig.clock.advance(100)
    restarted = rig.sender(rig.transport())
    before = len(rig.notifier.texts)

    await restarted.send_next()
    assert len(rig.notifier.texts) == before

    rig.clock.advance(STOP_REMINDER_INTERVAL_SECONDS - 1)
    await restarted.send_next()
    assert len(rig.notifier.texts) == before

    rig.clock.advance(1)
    await restarted.send_next()
    assert rig.notifier.texts[-1] == OUTBOX_NOTICES.stop_reminder.format(kind="FakeKind")


async def test_a_stop_set_by_another_path_after_a_resume_does_not_inherit_the_old_clock(
    rig: Rig,
) -> None:
    sender = await _stopped_sender(rig)
    rig.clock.advance(STOP_REMINDER_INTERVAL_SECONDS - 10)
    await sender.send_next()
    await rig.state.resume(keep_stop=False)
    await sender.send_next()
    rig.clock.advance(5)
    await rig.state.stop("OtherKind")
    await sender.send_next()
    before = len(rig.notifier.texts)

    rig.clock.advance(10)
    await sender.send_next()
    assert len(rig.notifier.texts) == before

    rig.clock.advance(STOP_REMINDER_INTERVAL_SECONDS - 10)
    await sender.send_next()
    assert rig.notifier.texts[-1] == OUTBOX_NOTICES.stop_reminder.format(kind="OtherKind")
