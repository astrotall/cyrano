from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from cyrano.bot.notifier import OUTBOX_NOTICES
from cyrano.db.repositories import (
    SqlContactRepository,
    SqlMessageRepository,
    SqlModeRepository,
    SqlOutboxRepository,
    SqlSenderStateRepository,
)
from cyrano.domain.enums import (
    ContactStatus,
    ManualMessageOutcome,
    MessageDirection,
    OutboxSource,
    OutboxState,
    PauseReason,
)
from cyrano.domain.errors import InvalidStatusTransition
from cyrano.domain.models import (
    MAX_FUTURE_SKEW,
    Contact,
    Message,
    MessageCreate,
    OutgoingMessage,
    StatusChange,
)
from cyrano.domain.outbox import (
    CANCEL_REASON_MANUAL_MESSAGE,
    REFUSED_CONTACT_NOT_ACTIVE,
    ScheduledMessage,
)
from cyrano.logging import JsonFormatter
from cyrano.services.manual_intervention import SETTLE_DELAYS_SECONDS, ManualMessageDetector
from cyrano.services.manual_pause_notifier import ManualPauseNotifier
from cyrano.services.outbox_sender import OWNER_NOTIFY_DEADLINE, OutboxSender
from cyrano.services.repositories import MessageRepository
from cyrano.services.send_guard import SendGuard
from cyrano.services.send_limiter import SendLimiter
from tests.bot.fakes import FAKE_TIME
from tests.privacy import FAKE_STRANGER_TELEGRAM_ID, FAKE_TELEGRAM_ID, assert_absent
from tests.services.outbox_fakes import (
    FAKE_NOW,
    FakeClock,
    FakeLimits,
    HangingManualPauseNotifier,
    RecordingManualPauseNotifier,
    RecordingNotifier,
    ScriptedTransport,
    free_schedule,
    stored_contact,
)
from tests.userbot.fakes import RecordingSleep

MANUAL_TEXT = "manual-text-marker-zq that must never leak"
USERNAME = "manual_username_marker_zq"
DISPLAY_NAME = "ManualNameMarkerZq"
QUEUED_TEXT = "queued-text-marker-zq that must never leak"
MESSAGE_ID = 800100
SENT_AT = datetime(2025, 3, 4, 5, 6, 7, tzinfo=UTC)
CLOCK_NOW = datetime(2025, 6, 1, 12, 0, tzinfo=UTC)
ONE_SECOND = timedelta(seconds=1)
FAST_DEADLINE = 0.01
EVERY_DELAY = list(SETTLE_DELAYS_SECONDS)
NOT_ACTIVE = [
    ContactStatus.NEW,
    ContactStatus.PENDING,
    ContactStatus.PAUSED,
    ContactStatus.IGNORED,
]


class _Switch:
    autopilot_enabled = False


class AppearingSleep:
    def __init__(
        self,
        messages: MessageRepository,
        contact_id: int,
        tg_message_id: int,
        appear_on_call: int | None,
    ) -> None:
        self.delays: list[float] = []
        self._messages = messages
        self._contact_id = contact_id
        self._tg_message_id = tg_message_id
        self._appear_on_call = appear_on_call

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        if self._appear_on_call == len(self.delays):
            await self._messages.append(
                MessageCreate(
                    contact_id=self._contact_id,
                    direction=MessageDirection.OUTGOING,
                    text=QUEUED_TEXT,
                    tg_message_id=self._tg_message_id,
                )
            )


class RunOnSleep:
    def __init__(self, action: Callable[[], Awaitable[None]]) -> None:
        self.delays: list[float] = []
        self._action = action

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        await self._action()


@dataclass
class Rig:
    contacts: SqlContactRepository
    messages: SqlMessageRepository
    outbox: SqlOutboxRepository
    modes: SqlModeRepository
    state: SqlSenderStateRepository
    notifier: RecordingManualPauseNotifier = field(default_factory=RecordingManualPauseNotifier)
    sleep: RecordingSleep = field(default_factory=RecordingSleep)
    clock: FakeClock = field(default_factory=lambda: FakeClock(CLOCK_NOW))

    def detector(
        self,
        *,
        notifier: ManualPauseNotifier | None = None,
        sleep: RecordingSleep | AppearingSleep | RunOnSleep | None = None,
        messages: MessageRepository | None = None,
        contacts: SqlContactRepository | None = None,
        notify_deadline: float = OWNER_NOTIFY_DEADLINE,
    ) -> ManualMessageDetector:
        return ManualMessageDetector(
            contacts if contacts is not None else self.contacts,
            messages if messages is not None else self.messages,
            self.outbox,
            notifier if notifier is not None else self.notifier,
            sleep=sleep if sleep is not None else self.sleep,
            notify_deadline=notify_deadline,
            clock=self.clock,
        )

    async def contact(self, status: ContactStatus, tg_id: int = FAKE_TELEGRAM_ID) -> Contact:
        return await self.reload(
            await stored_contact(
                self.contacts,
                self.modes,
                status,
                tg_id=tg_id,
                username=USERNAME,
                display_name=DISPLAY_NAME,
            )
        )

    async def reload(self, contact: Contact) -> Contact:
        found = await self.contacts.get_by_id(contact.id)
        assert found is not None
        return found

    async def queued(self, contact: Contact, send_at: datetime) -> ScheduledMessage:
        return await self.outbox.enqueue(contact.id, QUEUED_TEXT, send_at, OutboxSource.MANUAL_TEST)

    async def stored_messages(self, contact: Contact) -> list[tuple[MessageDirection, str, int]]:
        found = await self.messages.list_last(contact.id, 100)
        return [(m.direction, m.text, m.tg_message_id) for m in found]


@pytest.fixture
def rig(
    contacts: SqlContactRepository,
    messages: SqlMessageRepository,
    outbox: SqlOutboxRepository,
    modes: SqlModeRepository,
    sender_state: SqlSenderStateRepository,
) -> Rig:
    return Rig(contacts, messages, outbox, modes, sender_state)


def _message(
    contact: Contact | None = None,
    *,
    tg_id: int = FAKE_TELEGRAM_ID,
    tg_message_id: int = MESSAGE_ID,
    text: str = MANUAL_TEXT,
    sent_at: datetime | None = SENT_AT,
) -> OutgoingMessage:
    return OutgoingMessage(
        tg_id=contact.tg_id if contact is not None else tg_id,
        tg_message_id=tg_message_id,
        text=text,
        sent_at=sent_at,
    )


async def test_the_settle_delays_are_the_documented_series_of_about_sixteen_seconds() -> None:
    assert SETTLE_DELAYS_SECONDS == (0.25, 0.5, 1.0, 2.0, 4.0, 8.0)
    assert sum(SETTLE_DELAYS_SECONDS) == 15.75


async def test_a_manual_message_to_an_active_contact_pauses_it_and_tells_the_owner_once(
    rig: Rig,
) -> None:
    contact = await rig.contact(ContactStatus.ACTIVE)
    other = await rig.contact(ContactStatus.ACTIVE, FAKE_TELEGRAM_ID + 1)
    own_row = await rig.queued(contact, FAKE_NOW + timedelta(hours=1))
    other_row = await rig.queued(other, FAKE_NOW + timedelta(hours=1))

    outcome = await rig.detector().handle(_message(contact))

    assert outcome is ManualMessageOutcome.PAUSED
    paused = await rig.reload(contact)
    assert (paused.status, paused.pause_reason) == (
        ContactStatus.PAUSED,
        PauseReason.MANUAL_MESSAGE,
    )
    assert paused.mode_id == contact.mode_id
    assert paused.last_message_at == contact.last_message_at
    cancelled = await rig.outbox.get(own_row.id)
    assert cancelled is not None
    assert (cancelled.state, cancelled.cancel_reason) == (
        OutboxState.CANCELLED,
        CANCEL_REASON_MANUAL_MESSAGE,
    )
    untouched = await rig.outbox.get(other_row.id)
    assert untouched is not None
    assert untouched.state is OutboxState.SCHEDULED
    assert (await rig.reload(other)).status is ContactStatus.ACTIVE
    assert rig.notifier.contact_ids == [contact.id]
    assert await rig.stored_messages(contact) == [
        (MessageDirection.OUTGOING, MANUAL_TEXT, MESSAGE_ID)
    ]
    assert rig.sleep.delays == []


async def test_the_owner_message_keeps_its_telegram_time_and_leaves_the_last_message_time(
    rig: Rig, contacts: SqlContactRepository
) -> None:
    contact = await rig.contact(ContactStatus.ACTIVE)
    await contacts.touch_last_message_at(contact.id, FAKE_TIME)

    await rig.detector().handle(_message(contact, sent_at=SENT_AT))

    [stored] = await rig.messages.list_last(contact.id, 10)
    assert stored.created_at == SENT_AT
    assert (await rig.reload(contact)).last_message_at == FAKE_TIME


@pytest.mark.parametrize(
    "unusable_time", [None, datetime(2025, 3, 4, 5, 6, 7)], ids=["missing", "naive"]
)
async def test_a_manual_message_with_a_missing_or_naive_time_still_pauses_and_is_stored_now(
    unusable_time: datetime | None, rig: Rig, contacts: SqlContactRepository
) -> None:
    contact = await rig.contact(ContactStatus.ACTIVE)
    await contacts.touch_last_message_at(contact.id, FAKE_TIME)

    outcome = await rig.detector().handle(_message(contact, sent_at=unusable_time))

    assert outcome is ManualMessageOutcome.PAUSED
    paused = await rig.reload(contact)
    assert (paused.status, paused.pause_reason) == (
        ContactStatus.PAUSED,
        PauseReason.MANUAL_MESSAGE,
    )
    assert paused.last_message_at == FAKE_TIME
    assert rig.notifier.contact_ids == [contact.id]
    [stored] = await rig.messages.list_last(contact.id, 10)
    assert (stored.text, stored.tg_message_id, stored.created_at) == (
        MANUAL_TEXT,
        MESSAGE_ID,
        CLOCK_NOW,
    )


@pytest.mark.parametrize("status", [ContactStatus.PAUSED, ContactStatus.NEW])
async def test_a_stored_only_manual_message_without_a_time_is_stored_now(
    status: ContactStatus, rig: Rig
) -> None:
    contact = await rig.contact(status)

    outcome = await rig.detector().handle(_message(contact, sent_at=None))

    assert outcome is ManualMessageOutcome.STORED
    [stored] = await rig.messages.list_last(contact.id, 10)
    assert stored.created_at == CLOCK_NOW


@pytest.mark.parametrize(
    ("offset", "expected"),
    [
        (MAX_FUTURE_SKEW, CLOCK_NOW + MAX_FUTURE_SKEW),
        (MAX_FUTURE_SKEW - ONE_SECOND, CLOCK_NOW + MAX_FUTURE_SKEW - ONE_SECOND),
        (MAX_FUTURE_SKEW + ONE_SECOND, CLOCK_NOW),
        (timedelta(days=3), CLOCK_NOW),
        (-timedelta(hours=8), CLOCK_NOW - timedelta(hours=8)),
    ],
    ids=["at-the-skew", "within-the-skew", "beyond-the-skew", "far-future", "past"],
)
async def test_the_stored_time_of_a_manual_message_follows_the_clock_skew_rule(
    offset: timedelta, expected: datetime, rig: Rig
) -> None:
    contact = await rig.contact(ContactStatus.ACTIVE)

    await rig.detector().handle(_message(contact, sent_at=CLOCK_NOW + offset))

    [stored] = await rig.messages.list_last(contact.id, 10)
    assert stored.created_at == expected


async def test_a_replayed_event_without_a_time_pauses_and_notifies_once(rig: Rig) -> None:
    contact = await rig.contact(ContactStatus.ACTIVE)
    detector = rig.detector()

    first = await detector.handle(_message(contact, sent_at=None))
    rig.clock.advance(30)
    second = await detector.handle(_message(contact, sent_at=None))

    assert (first, second) == (ManualMessageOutcome.PAUSED, ManualMessageOutcome.OWN_MESSAGE)
    assert rig.notifier.contact_ids == [contact.id]
    [stored] = await rig.messages.list_last(contact.id, 10)
    assert stored.created_at == CLOCK_NOW


async def test_a_second_manual_message_to_a_paused_contact_is_only_stored(rig: Rig) -> None:
    contact = await rig.contact(ContactStatus.ACTIVE)
    detector = rig.detector()
    await detector.handle(_message(contact))

    outcome = await detector.handle(_message(contact, tg_message_id=MESSAGE_ID + 1, text="second"))

    assert outcome is ManualMessageOutcome.STORED
    paused = await rig.reload(contact)
    assert (paused.status, paused.pause_reason) == (
        ContactStatus.PAUSED,
        PauseReason.MANUAL_MESSAGE,
    )
    assert rig.notifier.contact_ids == [contact.id]
    assert [text for _, text, _ in await rig.stored_messages(contact)] == [MANUAL_TEXT, "second"]


@pytest.mark.parametrize("status", NOT_ACTIVE)
async def test_a_manual_message_to_a_contact_that_is_not_active_is_only_stored(
    status: ContactStatus, rig: Rig
) -> None:
    contact = await rig.contact(status)
    row = await rig.queued(contact, FAKE_NOW + timedelta(hours=1))

    outcome = await rig.detector().handle(_message(contact))

    assert outcome is ManualMessageOutcome.STORED
    assert await rig.reload(contact) == contact
    stored_row = await rig.outbox.get(row.id)
    assert stored_row is not None
    assert stored_row.state is OutboxState.SCHEDULED
    assert rig.notifier.contact_ids == []
    assert await rig.stored_messages(contact) == [
        (MessageDirection.OUTGOING, MANUAL_TEXT, MESSAGE_ID)
    ]


async def test_a_paused_contact_keeps_the_reason_it_already_has(
    rig: Rig, contacts: SqlContactRepository
) -> None:
    contact = await rig.contact(ContactStatus.ACTIVE)
    await contacts.set_status(
        contact.id, ContactStatus.PAUSED, pause_reason=PauseReason.SEND_REJECTED
    )

    await rig.detector().handle(_message(contact))

    assert (await rig.reload(contact)).pause_reason is PauseReason.SEND_REJECTED


async def test_an_unknown_peer_changes_and_stores_nothing(rig: Rig) -> None:
    contact = await rig.contact(ContactStatus.ACTIVE)

    outcome = await rig.detector().handle(_message(tg_id=FAKE_STRANGER_TELEGRAM_ID))

    assert outcome is ManualMessageOutcome.UNKNOWN_PEER
    assert await rig.reload(contact) == contact
    assert await rig.stored_messages(contact) == []
    assert rig.notifier.contact_ids == []
    assert rig.sleep.delays == []


async def test_a_media_only_manual_message_counts_and_is_stored_with_empty_text(
    rig: Rig,
) -> None:
    contact = await rig.contact(ContactStatus.ACTIVE)

    outcome = await rig.detector().handle(_message(contact, text=""))

    assert outcome is ManualMessageOutcome.PAUSED
    assert (await rig.reload(contact)).status is ContactStatus.PAUSED
    assert await rig.stored_messages(contact) == [(MessageDirection.OUTGOING, "", MESSAGE_ID)]


async def test_the_same_event_twice_pauses_notifies_and_stores_once(rig: Rig) -> None:
    contact = await rig.contact(ContactStatus.ACTIVE)
    detector = rig.detector()

    first = await detector.handle(_message(contact))
    second = await detector.handle(_message(contact))

    assert (first, second) == (ManualMessageOutcome.PAUSED, ManualMessageOutcome.OWN_MESSAGE)
    assert rig.notifier.contact_ids == [contact.id]
    assert len(await rig.stored_messages(contact)) == 1


async def test_a_replayed_event_after_a_resume_does_not_pause_again(rig: Rig) -> None:
    contact = await rig.contact(ContactStatus.ACTIVE)
    detector = rig.detector()
    await detector.handle(_message(contact))
    await rig.contacts.set_status(contact.id, ContactStatus.ACTIVE)

    outcome = await detector.handle(_message(contact))

    assert outcome is ManualMessageOutcome.OWN_MESSAGE
    assert (await rig.reload(contact)).status is ContactStatus.ACTIVE
    assert rig.notifier.contact_ids == [contact.id]


async def test_our_own_stored_message_is_ignored_without_waiting_even_with_a_row_sending(
    rig: Rig,
) -> None:
    contact = await rig.contact(ContactStatus.ACTIVE)
    row = await rig.queued(contact, FAKE_NOW)
    await rig.outbox.claim_due(FAKE_NOW)
    await rig.outbox.complete_send(row.id, contact.id, QUEUED_TEXT, MESSAGE_ID, SENT_AT)
    other_row = await rig.queued(contact, FAKE_NOW)
    await rig.outbox.claim_due(FAKE_NOW)

    outcome = await rig.detector().handle(_message(contact, text=QUEUED_TEXT))

    assert outcome is ManualMessageOutcome.OWN_MESSAGE
    assert (await rig.reload(contact)).status is ContactStatus.ACTIVE
    assert rig.notifier.contact_ids == []
    assert rig.sleep.delays == []
    assert (await rig.stored_messages(contact)) == [
        (MessageDirection.OUTGOING, QUEUED_TEXT, MESSAGE_ID)
    ]
    still = await rig.outbox.get(other_row.id)
    assert still is not None
    assert still.state is OutboxState.SENDING


@pytest.mark.parametrize("appears_on", [1, 2, 3, 6])
async def test_an_echo_that_arrives_before_the_sender_stored_its_id_is_ignored(
    appears_on: int, rig: Rig
) -> None:
    contact = await rig.contact(ContactStatus.ACTIVE)
    await rig.queued(contact, FAKE_NOW)
    await rig.outbox.claim_due(FAKE_NOW)
    sleep = AppearingSleep(rig.messages, contact.id, MESSAGE_ID, appears_on)

    outcome = await rig.detector(sleep=sleep).handle(_message(contact, text=QUEUED_TEXT))

    assert outcome is ManualMessageOutcome.OWN_MESSAGE
    assert sleep.delays == EVERY_DELAY[:appears_on]
    assert (await rig.reload(contact)).status is ContactStatus.ACTIVE
    assert rig.notifier.contact_ids == []
    assert len(await rig.stored_messages(contact)) == 1


async def test_a_manual_message_while_a_row_is_sending_waits_every_delay_and_then_pauses(
    rig: Rig,
) -> None:
    contact = await rig.contact(ContactStatus.ACTIVE)
    await rig.queued(contact, FAKE_NOW)
    await rig.outbox.claim_due(FAKE_NOW)
    sleep = AppearingSleep(rig.messages, contact.id, MESSAGE_ID, None)

    outcome = await rig.detector(sleep=sleep).handle(_message(contact))

    assert outcome is ManualMessageOutcome.PAUSED
    assert sleep.delays == EVERY_DELAY
    assert (await rig.reload(contact)).pause_reason is PauseReason.MANUAL_MESSAGE
    assert rig.notifier.contact_ids == [contact.id]


@pytest.mark.parametrize("state", [OutboxState.SCHEDULED, OutboxState.SENT, OutboxState.FAILED])
async def test_a_row_that_is_not_sending_means_no_waiting(state: OutboxState, rig: Rig) -> None:
    contact = await rig.contact(ContactStatus.ACTIVE)
    row = await rig.queued(contact, FAKE_NOW)
    if state is not OutboxState.SCHEDULED:
        await rig.outbox.claim_due(FAKE_NOW)
        if state is OutboxState.SENT:
            await rig.outbox.complete_send(row.id, contact.id, QUEUED_TEXT, MESSAGE_ID + 5, SENT_AT)
        else:
            await rig.outbox.mark_failed(row.id, "fake_error")

    outcome = await rig.detector().handle(_message(contact))

    assert outcome is ManualMessageOutcome.PAUSED
    assert rig.sleep.delays == []


async def test_a_sending_row_of_another_contact_means_no_waiting(rig: Rig) -> None:
    contact = await rig.contact(ContactStatus.ACTIVE)
    other = await rig.contact(ContactStatus.ACTIVE, FAKE_TELEGRAM_ID + 1)
    await rig.queued(other, FAKE_NOW)
    await rig.outbox.claim_due(FAKE_NOW)

    await rig.detector().handle(_message(contact))

    assert rig.sleep.delays == []


async def test_a_contact_that_changed_during_the_wait_is_judged_by_its_new_status(
    rig: Rig, contacts: SqlContactRepository
) -> None:
    contact = await rig.contact(ContactStatus.ACTIVE)
    await rig.queued(contact, FAKE_NOW)
    await rig.outbox.claim_due(FAKE_NOW)

    async def ignore_the_contact() -> None:
        await contacts.set_status(contact.id, ContactStatus.IGNORED)

    sleep = RunOnSleep(ignore_the_contact)

    outcome = await rig.detector(sleep=sleep).handle(_message(contact))

    assert outcome is ManualMessageOutcome.STORED
    assert (await rig.reload(contact)).status is ContactStatus.IGNORED
    assert rig.notifier.contact_ids == []


async def test_a_refused_transition_during_the_pause_only_stores_the_message(
    rig: Rig, contacts: SqlContactRepository
) -> None:
    contact = await rig.contact(ContactStatus.ACTIVE)

    class _Raced(SqlContactRepository):
        async def set_status(
            self,
            contact_id: int,
            status: ContactStatus,
            *,
            mode_id: int | None = None,
            pause_reason: PauseReason | None = None,
            reply_after: datetime | None = None,
        ) -> StatusChange:
            raise InvalidStatusTransition(ContactStatus.IGNORED, status)

    raced = _Raced(contacts._session_factory)

    outcome = await rig.detector(contacts=raced).handle(_message(contact))

    assert outcome is ManualMessageOutcome.STORED
    assert rig.notifier.contact_ids == []
    assert len(await rig.stored_messages(contact)) == 1


async def test_a_pause_that_another_writer_made_first_gets_no_second_notice(
    rig: Rig, contacts: SqlContactRepository
) -> None:
    contact = await rig.contact(ContactStatus.ACTIVE)

    class _LosesTheRace(SqlContactRepository):
        async def set_status(
            self,
            contact_id: int,
            status: ContactStatus,
            *,
            mode_id: int | None = None,
            pause_reason: PauseReason | None = None,
            reply_after: datetime | None = None,
        ) -> StatusChange:
            await super().set_status(contact_id, status, pause_reason=PauseReason.SEND_REJECTED)
            return await super().set_status(contact_id, status, pause_reason=pause_reason)

    loser = _LosesTheRace(contacts._session_factory)

    outcome = await rig.detector(contacts=loser).handle(_message(contact))

    assert outcome is ManualMessageOutcome.STORED
    assert rig.notifier.contact_ids == []
    assert (await rig.reload(contact)).pause_reason is PauseReason.SEND_REJECTED


async def test_a_failing_notifier_never_undoes_the_pause_and_never_raises(rig: Rig) -> None:
    contact = await rig.contact(ContactStatus.ACTIVE)
    notifier = RecordingManualPauseNotifier(error=RuntimeError("fake notifier failure"))

    outcome = await rig.detector(notifier=notifier).handle(_message(contact))

    assert outcome is ManualMessageOutcome.PAUSED
    assert notifier.contact_ids == [contact.id]
    assert (await rig.reload(contact)).status is ContactStatus.PAUSED
    assert len(await rig.stored_messages(contact)) == 1


async def test_a_hung_notifier_is_cut_off_by_the_deadline_and_keeps_the_pause(rig: Rig) -> None:
    contact = await rig.contact(ContactStatus.ACTIVE)
    notifier = HangingManualPauseNotifier()

    outcome = await rig.detector(notifier=notifier, notify_deadline=FAST_DEADLINE).handle(
        _message(contact)
    )

    assert outcome is ManualMessageOutcome.PAUSED
    assert (notifier.calls, notifier.cancelled) == (1, 1)
    assert (await rig.reload(contact)).status is ContactStatus.PAUSED


async def test_the_owner_notice_deadline_is_the_sender_one() -> None:
    assert OWNER_NOTIFY_DEADLINE == 15.0


async def test_a_failure_while_storing_after_the_pause_still_tells_the_owner(
    rig: Rig, messages: SqlMessageRepository
) -> None:
    contact = await rig.contact(ContactStatus.ACTIVE)
    row = await rig.queued(contact, FAKE_NOW + timedelta(hours=1))

    class _BrokenMessages(SqlMessageRepository):
        async def append(self, data: MessageCreate) -> Message:
            raise RuntimeError("fake storage failure")

    broken = _BrokenMessages(messages._session_factory)

    outcome = await rig.detector(messages=broken).handle(_message(contact))

    assert outcome is ManualMessageOutcome.PAUSED
    assert rig.notifier.contact_ids == [contact.id]
    cancelled = await rig.outbox.get(row.id)
    assert cancelled is not None
    assert cancelled.state is OutboxState.CANCELLED


async def test_a_failure_before_the_pause_is_raised_and_a_repeat_pauses_exactly_once(
    rig: Rig, contacts: SqlContactRepository
) -> None:
    contact = await rig.contact(ContactStatus.ACTIVE)

    class _FailsOnce(SqlContactRepository):
        failures = 1

        async def set_status(
            self,
            contact_id: int,
            status: ContactStatus,
            *,
            mode_id: int | None = None,
            pause_reason: PauseReason | None = None,
            reply_after: datetime | None = None,
        ) -> StatusChange:
            if self.failures:
                self.failures -= 1
                raise ConnectionError("fake database failure")
            return await super().set_status(
                contact_id,
                status,
                mode_id=mode_id,
                pause_reason=pause_reason,
                reply_after=reply_after,
            )

    detector = rig.detector(contacts=_FailsOnce(contacts._session_factory))

    with pytest.raises(ConnectionError):
        await detector.handle(_message(contact))
    assert (await rig.reload(contact)).status is ContactStatus.ACTIVE
    assert await rig.stored_messages(contact) == []
    assert rig.notifier.contact_ids == []

    assert await detector.handle(_message(contact)) is ManualMessageOutcome.PAUSED
    assert await detector.handle(_message(contact)) is ManualMessageOutcome.OWN_MESSAGE
    assert rig.notifier.contact_ids == [contact.id]


class _TypingHook(ScriptedTransport):
    def __init__(self, hook: ManualMessageDetector, message: OutgoingMessage) -> None:
        super().__init__()
        self.hook = hook
        self.message = message
        self.outcome: ManualMessageOutcome | None = None

    async def show_typing(self, tg_id: int, seconds: float) -> None:
        await super().show_typing(tg_id, seconds)
        self.outcome = await self.hook.handle(self.message)


async def test_a_manual_message_during_the_pre_send_part_cancels_the_row_at_the_second_guard(
    rig: Rig,
) -> None:
    contact = await rig.contact(ContactStatus.ACTIVE)
    clock = FakeClock()
    row = await rig.queued(contact, clock())
    sleep = AppearingSleep(rig.messages, contact.id, MESSAGE_ID, None)
    transport = _TypingHook(rig.detector(sleep=sleep), _message(contact))
    sender = OutboxSender(
        rig.outbox,
        rig.contacts,
        rig.messages,
        rig.state,
        SendLimiter(rig.messages, FakeLimits()),
        free_schedule(),
        transport,
        RecordingNotifier(),
        SendGuard(_Switch()),
        OUTBOX_NOTICES,
        clock=clock,
    )

    await sender.send_next()

    assert transport.outcome is ManualMessageOutcome.PAUSED
    assert sleep.delays == EVERY_DELAY
    assert transport.steps == ["mark_read", "show_typing"]
    assert transport.calls == []
    refused = await rig.outbox.get(row.id)
    assert refused is not None
    assert (refused.state, refused.cancel_reason) == (
        OutboxState.CANCELLED,
        REFUSED_CONTACT_NOT_ACTIVE,
    )
    assert (await rig.reload(contact)).pause_reason is PauseReason.MANUAL_MESSAGE
    assert rig.notifier.contact_ids == [contact.id]


async def test_the_echo_of_a_real_send_is_not_taken_for_a_manual_message(rig: Rig) -> None:
    contact = await rig.contact(ContactStatus.ACTIVE)
    clock = FakeClock()
    await rig.queued(contact, clock())
    transport = ScriptedTransport(clock=clock)
    sender = OutboxSender(
        rig.outbox,
        rig.contacts,
        rig.messages,
        rig.state,
        SendLimiter(rig.messages, FakeLimits()),
        free_schedule(),
        transport,
        RecordingNotifier(),
        SendGuard(_Switch()),
        OUTBOX_NOTICES,
        clock=clock,
    )

    await sender.send_next()
    [sent] = await rig.messages.list_last(contact.id, 10)
    outcome = await rig.detector().handle(
        _message(
            contact, tg_message_id=sent.tg_message_id, text=QUEUED_TEXT, sent_at=sent.created_at
        )
    )

    assert outcome is ManualMessageOutcome.OWN_MESSAGE
    assert (await rig.reload(contact)).status is ContactStatus.ACTIVE
    assert rig.notifier.contact_ids == []


async def test_logs_and_the_notice_carry_no_text_name_username_or_telegram_id(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    contact = await rig.contact(ContactStatus.ACTIVE)
    await rig.queued(contact, FAKE_NOW + timedelta(hours=1))
    failing = RecordingManualPauseNotifier(error=RuntimeError(MANUAL_TEXT))

    with caplog.at_level(logging.DEBUG):
        await rig.detector(notifier=failing).handle(_message(contact))
        await rig.detector().handle(_message(contact, tg_message_id=MESSAGE_ID + 1))
        await rig.detector().handle(_message(tg_id=FAKE_STRANGER_TELEGRAM_ID))

    formatter = JsonFormatter()
    output = "\n".join(formatter.format(record) for record in caplog.records)
    assert output
    for marker in (MANUAL_TEXT, QUEUED_TEXT, USERNAME, DISPLAY_NAME, FAKE_TELEGRAM_ID):
        assert_absent(marker, output)
    assert_absent(FAKE_STRANGER_TELEGRAM_ID, output)
    assert failing.contact_ids == [contact.id]
