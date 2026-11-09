from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import pytest

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
    HandOverOutcome,
    MediaKind,
    MessageDirection,
    OutboxSource,
    OutboxState,
    PauseReason,
    TriggerClass,
    TriggerSource,
)
from cyrano.domain.errors import InvalidStatusTransition
from cyrano.domain.models import Contact, Message, MessageCreate, StatusChange
from cyrano.domain.outbox import CANCEL_REASON_STOP_TRIGGER, ScheduledMessage, SentMessage
from cyrano.domain.stop_triggers import (
    HAND_OVER_HISTORY_LINES,
    HAND_OVER_LINE_MAX_CHARS,
    TRUNCATION_MARK,
    DialogueLine,
)
from cyrano.logging import JsonFormatter
from cyrano.services.hand_over import HandOverService
from cyrano.services.hand_over_notifier import HandOverNotifier
from cyrano.services.outbox_sender import OWNER_NOTIFY_DEADLINE, OutboxSender
from cyrano.services.repositories import MessageRepository
from cyrano.services.send_guard import SendGuard
from cyrano.services.send_limiter import SendLimiter
from tests.privacy import FAKE_TELEGRAM_ID, assert_absent
from tests.services.outbox_fakes import (
    FAKE_NOW,
    FakeClock,
    FakeLimits,
    HangingHandOverNotifier,
    RecordingHandOverNotifier,
    RecordingNotifier,
    free_schedule,
    stored_contact,
)

TRIGGER_TEXT = "handover-text-marker-zq давай встретимся"
OWNER_TEXT = "handover-owner-marker-zq"
QUEUED_TEXT = "handover-queued-marker-zq"
USERNAME = "handover_username_marker_zq"
DISPLAY_NAME = "HandoverNameMarkerZq"
FAST_DEADLINE = 0.01
CLASSES = frozenset({TriggerClass.PHOTO, TriggerClass.MEETING})
NOT_ACTIVE = [ContactStatus.NEW, ContactStatus.PENDING, ContactStatus.IGNORED]


class _Switch:
    autopilot_enabled = True
    proactive_enabled = True


class ExplodingTransport:
    def __init__(self) -> None:
        self.attempts: list[str] = []

    async def send_text(self, tg_id: int, text: str) -> SentMessage:
        self.attempts.append("send_text")
        raise AssertionError("nothing may be sent to a contact on a hand-over")

    async def show_typing(self, tg_id: int, seconds: float) -> None:
        self.attempts.append("show_typing")
        raise AssertionError("nothing may be sent to a contact on a hand-over")

    async def mark_read(self, tg_id: int) -> None:
        self.attempts.append("mark_read")
        raise AssertionError("nothing may be sent to a contact on a hand-over")


@dataclass
class Rig:
    contacts: SqlContactRepository
    messages: SqlMessageRepository
    outbox: SqlOutboxRepository
    modes: SqlModeRepository
    state: SqlSenderStateRepository
    notifier: RecordingHandOverNotifier = field(default_factory=RecordingHandOverNotifier)

    def service(
        self,
        *,
        notifier: HandOverNotifier | None = None,
        contacts: SqlContactRepository | None = None,
        messages: MessageRepository | None = None,
        outbox: SqlOutboxRepository | None = None,
        notify_deadline: float = OWNER_NOTIFY_DEADLINE,
    ) -> HandOverService:
        return HandOverService(
            contacts if contacts is not None else self.contacts,
            messages if messages is not None else self.messages,
            outbox if outbox is not None else self.outbox,
            notifier if notifier is not None else self.notifier,
            notify_deadline=notify_deadline,
        )

    async def contact(self, status: ContactStatus, tg_id: int = FAKE_TELEGRAM_ID) -> Contact:
        created = await stored_contact(
            self.contacts,
            self.modes,
            status,
            tg_id=tg_id,
            username=USERNAME,
            display_name=DISPLAY_NAME,
        )
        return await self.reload(created)

    async def reload(self, contact: Contact) -> Contact:
        found = await self.contacts.get_by_id(contact.id)
        assert found is not None
        return found

    async def say(
        self, contact: Contact, direction: MessageDirection, text: str, number: int
    ) -> None:
        await self.messages.append(
            MessageCreate(
                contact_id=contact.id,
                direction=direction,
                text=text,
                tg_message_id=number,
                created_at=FAKE_NOW + timedelta(seconds=number),
            )
        )

    async def queued(self, contact: Contact) -> ScheduledMessage:
        return await self.outbox.enqueue(
            contact.id, QUEUED_TEXT, FAKE_NOW, OutboxSource.MANUAL_TEST
        )


@pytest.fixture
def rig(
    contacts: SqlContactRepository,
    messages: SqlMessageRepository,
    outbox: SqlOutboxRepository,
    modes: SqlModeRepository,
    sender_state: SqlSenderStateRepository,
) -> Rig:
    return Rig(contacts, messages, outbox, modes, sender_state)


def _sender(rig: Rig, transport: ExplodingTransport) -> OutboxSender:
    return OutboxSender(
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
        clock=FakeClock(FAKE_NOW + timedelta(minutes=1)),
    )


async def test_an_active_contact_is_paused_its_rows_cancelled_and_the_owner_told_once(
    rig: Rig,
) -> None:
    contact = await rig.contact(ContactStatus.ACTIVE)
    other = await rig.contact(ContactStatus.ACTIVE, FAKE_TELEGRAM_ID + 1)
    await rig.say(contact, MessageDirection.OUTGOING, OWNER_TEXT, 1)
    await rig.say(contact, MessageDirection.INCOMING, TRIGGER_TEXT, 2)
    own = await rig.queued(contact)
    others = await rig.queued(other)

    outcome = await rig.service().hand_over(contact.id, CLASSES, TriggerSource.TEXT)

    assert outcome is HandOverOutcome.HANDED_OVER
    paused = await rig.reload(contact)
    assert (paused.status, paused.pause_reason, paused.mode_id) == (
        ContactStatus.PAUSED,
        PauseReason.STOP_TRIGGER,
        contact.mode_id,
    )
    cancelled = await rig.outbox.get(own.id)
    assert cancelled is not None
    assert (cancelled.state, cancelled.cancel_reason) == (
        OutboxState.CANCELLED,
        CANCEL_REASON_STOP_TRIGGER,
    )
    untouched = await rig.outbox.get(others.id)
    assert untouched is not None
    assert untouched.state is OutboxState.SCHEDULED
    [notice] = rig.notifier.notices
    assert notice.contact_id == contact.id
    assert notice.name == DISPLAY_NAME
    assert notice.classes == (TriggerClass.MEETING, TriggerClass.PHOTO)
    assert notice.source is TriggerSource.TEXT
    assert notice.lines == (
        DialogueLine(direction=MessageDirection.OUTGOING, text=OWNER_TEXT),
        DialogueLine(direction=MessageDirection.INCOMING, text=TRIGGER_TEXT),
    )


async def test_nothing_reaches_the_contact_after_a_hand_over(rig: Rig) -> None:
    contact = await rig.contact(ContactStatus.ACTIVE)
    await rig.queued(contact)
    transport = ExplodingTransport()

    await rig.service().hand_over(contact.id, CLASSES, TriggerSource.TEXT)
    rows = await rig.outbox.list_rows(None, 10)
    await _sender(rig, transport).send_next()

    assert [row.state for row in rows] == [OutboxState.CANCELLED]

    assert transport.attempts == []
    assert await rig.messages.list_last(contact.id, 10) == []


async def test_only_the_last_five_lines_are_carried_in_order_and_each_is_truncated(
    rig: Rig,
) -> None:
    contact = await rig.contact(ContactStatus.ACTIVE)
    for number in range(1, 9):
        await rig.say(contact, MessageDirection.INCOMING, f"line {number} " + "я" * 400, number)
    await rig.say(contact, MessageDirection.INCOMING, "", 9)

    await rig.service().hand_over(contact.id, CLASSES, TriggerSource.TEXT)

    [notice] = rig.notifier.notices
    assert len(notice.lines) == HAND_OVER_HISTORY_LINES
    assert [line.text.split(" ")[1] for line in notice.lines[:-1]] == ["5", "6", "7", "8"]
    assert notice.lines[-1].text == ""
    for line in notice.lines[:-1]:
        assert len(line.text) == HAND_OVER_LINE_MAX_CHARS
        assert line.text.endswith(TRUNCATION_MARK)


async def test_the_model_signal_hands_over_with_no_classes(rig: Rig) -> None:
    contact = await rig.contact(ContactStatus.ACTIVE)

    outcome = await rig.service().hand_over(contact.id, frozenset(), TriggerSource.MODEL)

    assert outcome is HandOverOutcome.HANDED_OVER
    [notice] = rig.notifier.notices
    assert (notice.classes, notice.source, notice.lines) == ((), TriggerSource.MODEL, ())


class _StatusRecordingContacts(SqlContactRepository):
    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self.status_calls: list[ContactStatus] = []

    async def set_status(
        self,
        contact_id: int,
        status: ContactStatus,
        *,
        mode_id: int | None = None,
        pause_reason: PauseReason | None = None,
        reply_after: datetime | None = None,
    ) -> StatusChange:
        self.status_calls.append(status)
        return await super().set_status(
            contact_id,
            status,
            mode_id=mode_id,
            pause_reason=pause_reason,
            reply_after=reply_after,
        )


@pytest.mark.parametrize("status", NOT_ACTIVE)
async def test_a_contact_that_is_not_active_is_left_alone(
    rig: Rig, contacts: SqlContactRepository, status: ContactStatus
) -> None:
    contact = await rig.contact(status)
    row = await rig.queued(contact) if status is not ContactStatus.NEW else None
    recording = _StatusRecordingContacts(contacts._session_factory)

    outcome = await rig.service(contacts=recording).hand_over(
        contact.id, CLASSES, TriggerSource.TEXT
    )

    assert outcome is HandOverOutcome.NOT_ACTIVE
    assert recording.status_calls == []
    assert await rig.reload(contact) == contact
    assert rig.notifier.notices == []
    if row is not None:
        stored = await rig.outbox.get(row.id)
        assert stored is not None
        assert stored.state is OutboxState.SCHEDULED


async def test_a_paused_contact_keeps_its_reason_and_gets_no_notice(rig: Rig) -> None:
    contact = await rig.contact(ContactStatus.ACTIVE)
    await rig.contacts.set_status(
        contact.id, ContactStatus.PAUSED, pause_reason=PauseReason.MANUAL_MESSAGE
    )

    outcome = await rig.service().hand_over(contact.id, CLASSES, TriggerSource.TEXT)

    assert outcome is HandOverOutcome.ALREADY_PAUSED
    assert (await rig.reload(contact)).pause_reason is PauseReason.MANUAL_MESSAGE
    assert rig.notifier.notices == []


async def test_a_second_trigger_while_paused_does_nothing(rig: Rig) -> None:
    contact = await rig.contact(ContactStatus.ACTIVE)
    service = rig.service()
    await service.hand_over(contact.id, CLASSES, TriggerSource.TEXT)
    row = await rig.queued(contact)

    second = await service.hand_over(contact.id, {TriggerClass.MONEY}, TriggerSource.MODEL)

    stored = await rig.outbox.get(row.id)
    assert stored is not None
    assert stored.state is OutboxState.SCHEDULED
    assert second is HandOverOutcome.ALREADY_PAUSED
    assert len(rig.notifier.notices) == 1
    assert (await rig.reload(contact)).pause_reason is PauseReason.STOP_TRIGGER


async def test_an_unknown_contact_is_not_found(rig: Rig) -> None:
    outcome = await rig.service().hand_over(999_999, CLASSES, TriggerSource.TEXT)

    assert outcome is HandOverOutcome.NOT_FOUND
    assert rig.notifier.notices == []


async def test_a_pause_made_first_by_another_writer_gets_no_notice(
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
            await super().set_status(contact_id, status, pause_reason=PauseReason.MANUAL_MESSAGE)
            return await super().set_status(contact_id, status, pause_reason=pause_reason)

    service = rig.service(contacts=_LosesTheRace(contacts._session_factory))

    outcome = await service.hand_over(contact.id, CLASSES, TriggerSource.TEXT)

    assert outcome is HandOverOutcome.ALREADY_PAUSED
    assert rig.notifier.notices == []
    assert (await rig.reload(contact)).pause_reason is PauseReason.MANUAL_MESSAGE


async def test_a_refused_transition_is_not_active(rig: Rig, contacts: SqlContactRepository) -> None:
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

    outcome = await rig.service(contacts=_Raced(contacts._session_factory)).hand_over(
        contact.id, CLASSES, TriggerSource.TEXT
    )

    assert outcome is HandOverOutcome.NOT_ACTIVE
    assert rig.notifier.notices == []


async def test_a_failing_notifier_never_undoes_the_pause_and_never_raises(rig: Rig) -> None:
    contact = await rig.contact(ContactStatus.ACTIVE)
    notifier = RecordingHandOverNotifier(error=RuntimeError("fake notifier failure"))

    outcome = await rig.service(notifier=notifier).hand_over(
        contact.id, CLASSES, TriggerSource.TEXT
    )

    assert outcome is HandOverOutcome.HANDED_OVER
    assert len(notifier.notices) == 1
    assert (await rig.reload(contact)).status is ContactStatus.PAUSED


async def test_a_hung_notifier_is_cut_off_by_the_deadline_and_keeps_the_pause(rig: Rig) -> None:
    contact = await rig.contact(ContactStatus.ACTIVE)
    notifier = HangingHandOverNotifier()

    outcome = await rig.service(notifier=notifier, notify_deadline=FAST_DEADLINE).hand_over(
        contact.id, CLASSES, TriggerSource.TEXT
    )

    assert outcome is HandOverOutcome.HANDED_OVER
    assert (notifier.calls, notifier.cancelled) == (1, 1)
    assert (await rig.reload(contact)).status is ContactStatus.PAUSED


async def test_the_notice_deadline_is_the_sender_one() -> None:
    assert OWNER_NOTIFY_DEADLINE == 15.0


async def test_a_failing_cancel_still_tells_the_owner(
    rig: Rig, outbox: SqlOutboxRepository
) -> None:
    contact = await rig.contact(ContactStatus.ACTIVE)

    class _BrokenOutbox(SqlOutboxRepository):
        async def cancel_pending_for_contact(self, contact_id: int, reason: str) -> int:
            raise RuntimeError("fake storage failure")

    service = rig.service(outbox=_BrokenOutbox(outbox._session_factory))

    outcome = await service.hand_over(contact.id, CLASSES, TriggerSource.TEXT)

    assert outcome is HandOverOutcome.HANDED_OVER
    assert len(rig.notifier.notices) == 1


async def test_a_failing_history_load_still_tells_the_owner_without_lines(
    rig: Rig, messages: SqlMessageRepository
) -> None:
    contact = await rig.contact(ContactStatus.ACTIVE)

    class _BrokenMessages(SqlMessageRepository):
        async def list_last(self, contact_id: int, limit: int) -> list[Message]:
            raise RuntimeError("fake storage failure")

    service = rig.service(messages=_BrokenMessages(messages._session_factory))

    outcome = await service.hand_over(contact.id, CLASSES, TriggerSource.TEXT)

    assert outcome is HandOverOutcome.HANDED_OVER
    [notice] = rig.notifier.notices
    assert notice.lines == ()


async def test_logs_carry_ids_classes_and_the_source_and_never_a_text_or_a_name(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    contact = await rig.contact(ContactStatus.ACTIVE)
    await rig.say(contact, MessageDirection.INCOMING, TRIGGER_TEXT, 1)
    await rig.say(contact, MessageDirection.OUTGOING, OWNER_TEXT, 2)
    await rig.queued(contact)
    failing = RecordingHandOverNotifier(error=RuntimeError(TRIGGER_TEXT))

    with caplog.at_level(logging.DEBUG):
        await rig.service(notifier=failing).hand_over(contact.id, CLASSES, TriggerSource.TEXT)
        await rig.service().hand_over(contact.id, CLASSES, TriggerSource.TEXT)

    output = "\n".join(JsonFormatter().format(record) for record in caplog.records)
    assert output
    for marker in (TRIGGER_TEXT, OWNER_TEXT, QUEUED_TEXT, USERNAME, DISPLAY_NAME, FAKE_TELEGRAM_ID):
        assert_absent(marker, output)
    [handed] = [
        r for r in caplog.records if r.getMessage() == "a stop-trigger handed the contact over"
    ]
    assert handed.__dict__["contact_id"] == contact.id
    assert handed.__dict__["classes"] == ["meeting", "photo"]
    assert handed.__dict__["source"] == "text"


async def test_the_media_kind_reaches_the_notice_and_a_text_hand_over_has_none(rig: Rig) -> None:
    media_contact = await rig.contact(ContactStatus.ACTIVE)
    text_contact = await rig.contact(ContactStatus.ACTIVE, FAKE_TELEGRAM_ID + 1)

    media = await rig.service().hand_over(
        media_contact.id, (TriggerClass.MEDIA,), TriggerSource.MEDIA, MediaKind.PHOTO
    )
    text = await rig.service().hand_over(text_contact.id, CLASSES, TriggerSource.TEXT)

    assert (media, text) == (HandOverOutcome.HANDED_OVER, HandOverOutcome.HANDED_OVER)
    first, second = rig.notifier.notices
    assert (first.source, first.classes, first.media_kind) == (
        TriggerSource.MEDIA,
        (TriggerClass.MEDIA,),
        MediaKind.PHOTO,
    )
    assert second.media_kind is None


async def test_a_media_message_in_the_history_is_shown_with_its_label_and_caption(
    rig: Rig,
) -> None:
    contact = await rig.contact(ContactStatus.ACTIVE)
    await rig.messages.append(
        MessageCreate(
            contact_id=contact.id,
            direction=MessageDirection.INCOMING,
            text="handover-caption-marker-zq",
            tg_message_id=1,
            created_at=FAKE_NOW,
            media_kind=MediaKind.PHOTO,
        )
    )
    await rig.messages.append(
        MessageCreate(
            contact_id=contact.id,
            direction=MessageDirection.INCOMING,
            text="",
            tg_message_id=2,
            created_at=FAKE_NOW + timedelta(seconds=1),
            media_kind=MediaKind.VOICE,
        )
    )
    await rig.messages.append(
        MessageCreate(
            contact_id=contact.id,
            direction=MessageDirection.INCOMING,
            text="",
            tg_message_id=3,
            created_at=FAKE_NOW + timedelta(seconds=2),
        )
    )

    await rig.service().hand_over(
        contact.id, (TriggerClass.MEDIA,), TriggerSource.MEDIA, MediaKind.VOICE
    )

    [notice] = rig.notifier.notices
    assert [line.text for line in notice.lines] == [
        "[фото] handover-caption-marker-zq",
        "[голосовое сообщение]",
        "",
    ]
