from __future__ import annotations

import logging
import random
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import timedelta

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
    HandOverOutcome,
    MediaKind,
    MessageDirection,
    OutboxSource,
    OutboxState,
    PauseReason,
    TriggerClass,
    TriggerSource,
)
from cyrano.domain.models import (
    Contact,
    IncomingMessage,
    MessageCreate,
    NewContactEvent,
)
from cyrano.domain.outbox import CANCEL_REASON_STOP_TRIGGER, SentMessage
from cyrano.domain.stop_triggers import DialogueLine
from cyrano.logging import JsonFormatter
from cyrano.prompts.texts import MEDIA_LABELS
from cyrano.services.autopilot import AutopilotScheduler
from cyrano.services.contact_events import InMemoryContactEvents
from cyrano.services.early_read import EarlyReadGate, EarlyReadQueue, EarlyReadScheduler
from cyrano.services.hand_over import HandOverService
from cyrano.services.humanization import HumanizationPlanner
from cyrano.services.intake import IntakeResult, IntakeService
from cyrano.services.outbox_sender import OutboxSender
from cyrano.services.send_guard import SendGuard
from cyrano.services.send_limiter import SendLimiter
from cyrano.userbot.handler import IncomingHandler
from tests.privacy import FAKE_TELEGRAM_ID, assert_absent
from tests.services.autopilot_fakes import InMemoryAutopilotState, RecordingQueue
from tests.services.outbox_fakes import (
    FAKE_NOW,
    FakeClock,
    FakeLimits,
    RecordingHandOverNotifier,
    RecordingNotifier,
    free_schedule,
    stored_contact,
)
from tests.userbot.fakes import FakeEvent, FakeMediaMessage, FakeSender, RecordingSleep

CAPTION = "intake-media-caption-marker-zq"
TRIGGER_CAPTION = "intake-media-trigger-marker-zq давай встретимся"
OWNER_LINE = "intake-media-owner-marker-zq"
USERNAME = "intake_media_username_marker_zq"
DISPLAY_NAME = "IntakeMediaNameMarkerZq"
FILE_NAME = "intake-media-file-name-marker-zq.ogg"
EMOJI = "🫠"
HAND_OVER_KINDS = [
    MediaKind.VOICE,
    MediaKind.VIDEO_NOTE,
    MediaKind.PHOTO,
    MediaKind.VIDEO,
    MediaKind.OTHER,
]
NOT_ACTIVE = [ContactStatus.PENDING, ContactStatus.PAUSED, ContactStatus.IGNORED]
DEBOUNCE = 12
ONGOING_DEBOUNCE = 4
PLANNER_SEED = 3


class _Switch:
    autopilot_enabled = True
    proactive_enabled = True
    early_read_enabled = True


class ForbiddenTransport:
    def __init__(self) -> None:
        self.attempts: list[str] = []

    async def send_text(self, tg_id: int, text: str) -> SentMessage:
        self.attempts.append("send_text")
        raise AssertionError("nothing may be sent to a contact on a media message")

    async def show_typing(self, tg_id: int, seconds: float) -> None:
        self.attempts.append("show_typing")
        raise AssertionError("nothing may be sent to a contact on a media message")

    async def mark_read(self, tg_id: int) -> None:
        self.attempts.append("mark_read")
        raise AssertionError("nothing may be sent to a contact on a media message")


class SpyHandOver(HandOverService):
    def __init__(self, inner: HandOverService, error: Exception | None = None) -> None:
        self._inner = inner
        self._error = error
        self.calls: list[tuple[int, frozenset[TriggerClass], TriggerSource, MediaKind | None]] = []
        self.outcomes: list[HandOverOutcome] = []

    async def hand_over(
        self,
        contact_id: int,
        classes: Iterable[TriggerClass],
        source: TriggerSource,
        media_kind: MediaKind | None = None,
    ) -> HandOverOutcome:
        self.calls.append((contact_id, frozenset(classes), source, media_kind))
        if self._error is not None:
            raise self._error
        outcome = await self._inner.hand_over(contact_id, classes, source, media_kind)
        self.outcomes.append(outcome)
        return outcome


@dataclass
class Rig:
    contacts: SqlContactRepository
    messages: SqlMessageRepository
    outbox: SqlOutboxRepository
    modes: SqlModeRepository
    sender_state: SqlSenderStateRepository
    notifier: RecordingHandOverNotifier = field(default_factory=RecordingHandOverNotifier)
    events: InMemoryContactEvents[NewContactEvent] = field(
        default_factory=InMemoryContactEvents[NewContactEvent]
    )
    jobs: RecordingQueue = field(default_factory=RecordingQueue)
    early_reads: EarlyReadQueue = field(default_factory=EarlyReadQueue)
    clock: FakeClock = field(default_factory=FakeClock)

    def spy(self, error: Exception | None = None) -> SpyHandOver:
        inner = HandOverService(self.contacts, self.messages, self.outbox, self.notifier)
        return SpyHandOver(inner, error)

    def intake(
        self, hand_over: HandOverService | None, *, with_schedulers: bool = False
    ) -> IntakeService:
        if not with_schedulers:
            return IntakeService(
                self.contacts, self.messages, self.events, hand_over=hand_over, clock=self.clock
            )
        state = InMemoryAutopilotState()
        schedule = free_schedule()
        planner = HumanizationPlanner(self.messages, schedule, random.Random(PLANNER_SEED))
        scheduler = AutopilotScheduler(
            self.contacts,
            state,
            self.jobs,
            _Switch(),
            planner,
            debounce_seconds=DEBOUNCE,
            ongoing_debounce_seconds=ONGOING_DEBOUNCE,
            clock=self.clock,
        )
        gate = EarlyReadGate(
            self.contacts, state, self.sender_state, schedule, _Switch(), clock=self.clock
        )
        early = EarlyReadScheduler(
            gate,
            planner,
            schedule,
            self.early_reads,
            rng=random.Random(PLANNER_SEED),
            clock=self.clock,
        )
        return IntakeService(
            self.contacts,
            self.messages,
            self.events,
            hand_over=hand_over,
            scheduler=scheduler,
            early_read=early,
            clock=self.clock,
        )

    async def contact(self, status: ContactStatus) -> Contact:
        created = await stored_contact(
            self.contacts,
            self.modes,
            status,
            tg_id=FAKE_TELEGRAM_ID,
            username=USERNAME,
            display_name=DISPLAY_NAME,
        )
        return await self.reload(created)

    async def reload(self, contact: Contact) -> Contact:
        found = await self.contacts.get_by_id(contact.id)
        assert found is not None
        return found

    async def status_of(self, contact: Contact) -> tuple[ContactStatus, PauseReason | None]:
        found = await self.reload(contact)
        return found.status, found.pause_reason

    async def lively(self, contact: Contact) -> None:
        await self.messages.append(
            MessageCreate(
                contact_id=contact.id,
                direction=MessageDirection.OUTGOING,
                text=OWNER_LINE,
                tg_message_id=1,
                created_at=FAKE_NOW - timedelta(minutes=1),
            )
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


def _incoming(
    media_kind: MediaKind | None,
    text: str = "",
    tg_message_id: int = 2,
) -> IncomingMessage:
    return IncomingMessage(
        tg_id=FAKE_TELEGRAM_ID,
        username=USERNAME,
        display_name=DISPLAY_NAME,
        tg_message_id=tg_message_id,
        text=text,
        sent_at=FAKE_NOW,
        media_kind=media_kind,
    )


@pytest.mark.parametrize("kind", HAND_OVER_KINDS)
async def test_a_media_message_of_an_active_contact_pauses_it_and_tells_the_owner_once(
    rig: Rig, kind: MediaKind
) -> None:
    contact = await rig.contact(ContactStatus.ACTIVE)
    queued = await rig.outbox.enqueue(
        contact.id,
        "intake-media-queued-zq",
        FAKE_NOW + timedelta(hours=1),
        OutboxSource.MANUAL_TEST,
    )
    spy = rig.spy()

    result = await rig.intake(spy).handle(_incoming(kind))

    assert result == IntakeResult(
        contact_id=contact.id, message_id=result.message_id, contact_created=False
    )
    assert spy.calls == [(contact.id, frozenset({TriggerClass.MEDIA}), TriggerSource.MEDIA, kind)]
    assert await rig.status_of(contact) == (ContactStatus.PAUSED, PauseReason.STOP_TRIGGER)
    cancelled = await rig.outbox.get(queued.id)
    assert cancelled is not None
    assert (cancelled.state, cancelled.cancel_reason) == (
        OutboxState.CANCELLED,
        CANCEL_REASON_STOP_TRIGGER,
    )
    [notice] = rig.notifier.notices
    assert notice.contact_id == contact.id
    assert (notice.source, notice.classes, notice.media_kind) == (
        TriggerSource.MEDIA,
        (TriggerClass.MEDIA,),
        kind,
    )
    assert notice.lines[-1] == DialogueLine(
        direction=MessageDirection.INCOMING, text=MEDIA_LABELS[kind]
    )
    [stored] = [
        message for message in await rig.messages.list_last(contact.id, 10) if message.media_kind
    ]
    assert (stored.media_kind, stored.text) == (kind, "")


@pytest.mark.parametrize("kind", HAND_OVER_KINDS)
async def test_nothing_is_sent_to_the_contact_when_it_sends_media(
    rig: Rig, kind: MediaKind
) -> None:
    contact = await rig.contact(ContactStatus.ACTIVE)
    await rig.outbox.enqueue(
        contact.id, "intake-media-queued-zq", FAKE_NOW, OutboxSource.MANUAL_TEST
    )
    transport = ForbiddenTransport()
    sender = OutboxSender(
        rig.outbox,
        rig.contacts,
        rig.messages,
        rig.sender_state,
        SendLimiter(rig.messages, FakeLimits()),
        free_schedule(),
        transport,
        RecordingNotifier(),
        SendGuard(_Switch()),
        OUTBOX_NOTICES,
        clock=FakeClock(FAKE_NOW + timedelta(minutes=1)),
    )

    await rig.intake(rig.spy()).handle(_incoming(kind))

    assert await sender.send_next() is False
    assert transport.attempts == []


@pytest.mark.parametrize("kind", HAND_OVER_KINDS)
async def test_a_caption_stays_the_text_of_a_media_message(rig: Rig, kind: MediaKind) -> None:
    contact = await rig.contact(ContactStatus.ACTIVE)

    await rig.intake(rig.spy()).handle(_incoming(kind, CAPTION))

    [stored] = await rig.messages.list_last(contact.id, 10)
    assert (stored.media_kind, stored.text) == (kind, CAPTION)
    [notice] = rig.notifier.notices
    assert notice.lines[-1].text == f"{MEDIA_LABELS[kind]} {CAPTION}"


async def test_a_sticker_never_hands_over_and_is_stored_as_its_emoji(rig: Rig) -> None:
    contact = await rig.contact(ContactStatus.ACTIVE)
    spy = rig.spy()

    await rig.intake(spy).handle(_incoming(MediaKind.STICKER, EMOJI))

    assert spy.calls == []
    assert rig.notifier.notices == []
    assert await rig.status_of(contact) == (ContactStatus.ACTIVE, None)
    [stored] = await rig.messages.list_last(contact.id, 10)
    assert (stored.media_kind, stored.text) == (MediaKind.STICKER, EMOJI)


async def test_a_sticker_without_an_emoji_is_stored_without_text_and_does_not_hand_over(
    rig: Rig,
) -> None:
    contact = await rig.contact(ContactStatus.ACTIVE)
    spy = rig.spy()

    await rig.intake(spy).handle(_incoming(MediaKind.STICKER, ""))

    assert spy.calls == []
    [stored] = await rig.messages.list_last(contact.id, 10)
    assert (stored.media_kind, stored.text) == (MediaKind.STICKER, "")


async def test_a_plain_text_message_does_not_reach_the_media_gate(rig: Rig) -> None:
    contact = await rig.contact(ContactStatus.ACTIVE)
    spy = rig.spy()

    await rig.intake(spy).handle(_incoming(None, CAPTION))

    assert spy.calls == []
    assert await rig.status_of(contact) == (ContactStatus.ACTIVE, None)


@pytest.mark.parametrize("status", NOT_ACTIVE)
@pytest.mark.parametrize("kind", [MediaKind.VOICE, MediaKind.PHOTO])
async def test_a_contact_that_is_not_active_is_untouched(
    rig: Rig, status: ContactStatus, kind: MediaKind
) -> None:
    contact = await rig.contact(status)
    before = await rig.reload(contact)
    spy = rig.spy()

    await rig.intake(spy).handle(_incoming(kind))

    assert spy.calls == []
    assert rig.notifier.notices == []
    after = await rig.reload(contact)
    assert (after.status, after.pause_reason) == (before.status, before.pause_reason)
    [stored] = await rig.messages.list_last(contact.id, 10)
    assert stored.media_kind is kind


async def test_a_new_sender_with_media_is_registered_and_not_handed_over(rig: Rig) -> None:
    spy = rig.spy()

    result = await rig.intake(spy).handle(_incoming(MediaKind.VOICE))

    assert result.contact_created is True
    assert spy.calls == []
    contact = await rig.contacts.get_by_id(result.contact_id)
    assert contact is not None
    assert contact.status is ContactStatus.NEW
    [stored] = await rig.messages.list_last(contact.id, 10)
    assert stored.media_kind is MediaKind.VOICE


async def test_a_second_media_message_while_paused_does_nothing_more(rig: Rig) -> None:
    contact = await rig.contact(ContactStatus.ACTIVE)
    spy = rig.spy()
    intake = rig.intake(spy)

    await intake.handle(_incoming(MediaKind.VOICE, tg_message_id=2))
    await intake.handle(_incoming(MediaKind.PHOTO, tg_message_id=3))
    await intake.handle(_incoming(None, CAPTION, tg_message_id=4))

    assert len(spy.calls) == 1
    assert len(rig.notifier.notices) == 1
    assert await rig.status_of(contact) == (ContactStatus.PAUSED, PauseReason.STOP_TRIGGER)
    assert len(await rig.messages.list_last(contact.id, 10)) == 3


async def test_replaying_the_same_media_message_notifies_once(rig: Rig) -> None:
    contact = await rig.contact(ContactStatus.ACTIVE)
    intake = rig.intake(rig.spy())

    await intake.handle(_incoming(MediaKind.VIDEO_NOTE))
    await intake.handle(_incoming(MediaKind.VIDEO_NOTE))

    assert len(rig.notifier.notices) == 1
    assert len(await rig.messages.list_last(contact.id, 10)) == 1


async def test_a_resumed_contact_is_handed_over_again_by_the_next_media_message(
    rig: Rig,
) -> None:
    contact = await rig.contact(ContactStatus.ACTIVE)
    intake = rig.intake(rig.spy())
    await intake.handle(_incoming(MediaKind.VOICE, tg_message_id=2))
    await rig.contacts.set_status(contact.id, ContactStatus.ACTIVE)

    await intake.handle(_incoming(None, CAPTION, tg_message_id=3))
    assert await rig.status_of(contact) == (ContactStatus.ACTIVE, None)
    await intake.handle(_incoming(MediaKind.PHOTO, tg_message_id=4))

    assert [notice.media_kind for notice in rig.notifier.notices] == [
        MediaKind.VOICE,
        MediaKind.PHOTO,
    ]


async def test_a_caption_that_trips_a_trigger_hands_over_once_with_its_text_classes(
    rig: Rig,
) -> None:
    contact = await rig.contact(ContactStatus.ACTIVE)
    spy = rig.spy()

    await rig.intake(spy).handle(_incoming(MediaKind.PHOTO, TRIGGER_CAPTION))

    assert spy.calls == [
        (contact.id, frozenset({TriggerClass.MEETING}), TriggerSource.TEXT, None),
        (contact.id, frozenset({TriggerClass.MEDIA}), TriggerSource.MEDIA, MediaKind.PHOTO),
    ]
    assert spy.outcomes == [HandOverOutcome.HANDED_OVER, HandOverOutcome.ALREADY_PAUSED]
    [notice] = rig.notifier.notices
    assert (notice.source, notice.classes, notice.media_kind) == (
        TriggerSource.TEXT,
        (TriggerClass.MEETING,),
        None,
    )


async def test_a_failing_media_gate_never_breaks_the_intake(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    contact = await rig.contact(ContactStatus.ACTIVE)
    spy = rig.spy(error=RuntimeError(CAPTION))

    with caplog.at_level(logging.DEBUG):
        result = await rig.intake(spy).handle(_incoming(MediaKind.VOICE, CAPTION))

    assert result.contact_id == contact.id
    [stored] = await rig.messages.list_last(contact.id, 10)
    assert stored.media_kind is MediaKind.VOICE
    [warning] = [r for r in caplog.records if r.getMessage() == "media check failed"]
    assert warning.__dict__["error"] == "RuntimeError"
    assert_absent(CAPTION, "\n".join(JsonFormatter().format(r) for r in caplog.records))


async def test_a_failing_media_gate_does_not_make_the_userbot_retry_the_intake(rig: Rig) -> None:
    await rig.contact(ContactStatus.ACTIVE)

    class _Counting(IntakeService):
        calls = 0

        async def handle(self, incoming: IncomingMessage) -> IntakeResult:
            type(self).calls += 1
            return await super().handle(incoming)

    intake = _Counting(
        rig.contacts, rig.messages, rig.events, hand_over=rig.spy(error=RuntimeError("fake"))
    )
    sleep = RecordingSleep()
    handler = IncomingHandler(intake, sleep)
    sender = FakeSender(id=FAKE_TELEGRAM_ID, username=USERNAME)
    event = FakeEvent(
        id=5,
        raw_text="",
        message=FakeMediaMessage(voice=object()),
        sender=sender,
        chat=sender,
    )

    await handler.handle(event)

    assert _Counting.calls == 1
    assert sleep.delays == []
    assert (handler.handled, handler.failed) == (1, 0)
    assert event.write_attempts == []


async def test_a_failing_notifier_still_pauses_and_never_breaks_the_intake(rig: Rig) -> None:
    contact = await rig.contact(ContactStatus.ACTIVE)
    failing = RecordingHandOverNotifier(error=RuntimeError("fake"))
    service = HandOverService(rig.contacts, rig.messages, rig.outbox, failing)

    result = await rig.intake(service).handle(_incoming(MediaKind.VIDEO))

    assert result.contact_id == contact.id
    assert await rig.status_of(contact) == (ContactStatus.PAUSED, PauseReason.STOP_TRIGGER)


async def test_without_a_hand_over_service_a_media_message_is_only_stored(rig: Rig) -> None:
    contact = await rig.contact(ContactStatus.ACTIVE)

    await rig.intake(None).handle(_incoming(MediaKind.VOICE))

    assert await rig.status_of(contact) == (ContactStatus.ACTIVE, None)
    [stored] = await rig.messages.list_last(contact.id, 10)
    assert stored.media_kind is MediaKind.VOICE


@pytest.mark.parametrize("kind", HAND_OVER_KINDS)
async def test_the_media_gate_runs_before_the_schedulers_so_nothing_is_enqueued_or_read(
    rig: Rig, kind: MediaKind
) -> None:
    contact = await rig.contact(ContactStatus.ACTIVE)
    await rig.lively(contact)

    await rig.intake(rig.spy(), with_schedulers=True).handle(_incoming(kind))

    assert rig.jobs.jobs == []
    assert rig.early_reads.pending() == []
    assert len(rig.notifier.notices) == 1


async def test_the_same_rig_enqueues_and_registers_for_a_plain_message(rig: Rig) -> None:
    contact = await rig.contact(ContactStatus.ACTIVE)
    await rig.lively(contact)

    await rig.intake(rig.spy(), with_schedulers=True).handle(_incoming(None, CAPTION))

    assert [job[0] for job in rig.jobs.jobs] == [contact.id]
    assert [request.contact_id for request in rig.early_reads.pending()] == [contact.id]


async def test_a_sticker_is_still_scheduled_like_text(rig: Rig) -> None:
    contact = await rig.contact(ContactStatus.ACTIVE)
    await rig.lively(contact)

    await rig.intake(rig.spy(), with_schedulers=True).handle(_incoming(MediaKind.STICKER, EMOJI))

    assert [job[0] for job in rig.jobs.jobs] == [contact.id]


async def test_a_caption_trigger_and_the_media_gate_leave_the_schedulers_idle(rig: Rig) -> None:
    contact = await rig.contact(ContactStatus.ACTIVE)
    await rig.lively(contact)

    await rig.intake(rig.spy(), with_schedulers=True).handle(
        _incoming(MediaKind.PHOTO, TRIGGER_CAPTION)
    )

    assert rig.jobs.jobs == []
    assert rig.early_reads.pending() == []
    assert len(rig.notifier.notices) == 1


async def test_the_media_path_logs_no_text_caption_name_file_name_or_telegram_id(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    await rig.contact(ContactStatus.ACTIVE)

    with caplog.at_level(logging.DEBUG):
        await rig.intake(rig.spy()).handle(_incoming(MediaKind.PHOTO, f"{CAPTION} {FILE_NAME}"))

    output = "\n".join(JsonFormatter().format(record) for record in caplog.records)
    assert output
    for marker in (CAPTION, FILE_NAME, USERNAME, DISPLAY_NAME, FAKE_TELEGRAM_ID):
        assert_absent(marker, output)
