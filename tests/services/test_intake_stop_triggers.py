from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from cyrano.db.repositories import (
    SqlContactRepository,
    SqlMessageRepository,
    SqlModeRepository,
    SqlOutboxRepository,
)
from cyrano.domain.enums import (
    ContactStatus,
    HandOverOutcome,
    MessageDirection,
    OutboxSource,
    OutboxState,
    PauseReason,
    TriggerClass,
    TriggerSource,
)
from cyrano.domain.models import Contact, IncomingMessage, NewContactEvent
from cyrano.domain.outbox import CANCEL_REASON_STOP_TRIGGER
from cyrano.domain.stop_triggers import DialogueLine
from cyrano.logging import JsonFormatter
from cyrano.services import intake as intake_module
from cyrano.services.contact_events import InMemoryContactEvents
from cyrano.services.hand_over import HandOverService
from cyrano.services.intake import IntakeResult, IntakeService
from cyrano.userbot.handler import IncomingHandler
from tests.privacy import FAKE_TELEGRAM_ID, assert_absent
from tests.services.outbox_fakes import FAKE_NOW, RecordingHandOverNotifier, stored_contact
from tests.userbot.fakes import FakeEvent, FakeSender, RecordingSleep

TRIGGER_TEXT = "intake-trigger-marker-zq давай встретимся"
HARMLESS_TEXT = "intake-harmless-marker-zq как прошёл день"
USERNAME = "intake_username_marker_zq"
DISPLAY_NAME = "IntakeNameMarkerZq"
SENT_AT = datetime(2025, 3, 4, 5, 6, 7, tzinfo=UTC)
NOT_ACTIVE = [ContactStatus.PENDING, ContactStatus.PAUSED, ContactStatus.IGNORED]


class SpyHandOver(HandOverService):
    def __init__(self, inner: HandOverService, error: Exception | None = None) -> None:
        self._inner = inner
        self._error = error
        self.calls: list[tuple[int, frozenset[TriggerClass], TriggerSource]] = []

    async def hand_over(
        self, contact_id: int, classes: Iterable[TriggerClass], source: TriggerSource
    ) -> HandOverOutcome:
        self.calls.append((contact_id, frozenset(classes), source))
        if self._error is not None:
            raise self._error
        return await self._inner.hand_over(contact_id, classes, source)


@dataclass
class Rig:
    contacts: SqlContactRepository
    messages: SqlMessageRepository
    outbox: SqlOutboxRepository
    modes: SqlModeRepository
    notifier: RecordingHandOverNotifier = field(default_factory=RecordingHandOverNotifier)
    events: InMemoryContactEvents[NewContactEvent] = field(
        default_factory=InMemoryContactEvents[NewContactEvent]
    )

    def spy(self, error: Exception | None = None) -> SpyHandOver:
        inner = HandOverService(self.contacts, self.messages, self.outbox, self.notifier)
        return SpyHandOver(inner, error)

    def intake(self, hand_over: HandOverService | None) -> IntakeService:
        return IntakeService(self.contacts, self.messages, self.events, hand_over=hand_over)

    async def contact(self, status: ContactStatus) -> Contact:
        created = await stored_contact(
            self.contacts,
            self.modes,
            status,
            tg_id=FAKE_TELEGRAM_ID,
            username=USERNAME,
            display_name=DISPLAY_NAME,
        )
        found = await self.contacts.get_by_id(created.id)
        assert found is not None
        return found

    async def status_of(self, contact: Contact) -> tuple[ContactStatus, PauseReason | None]:
        found = await self.contacts.get_by_id(contact.id)
        assert found is not None
        return found.status, found.pause_reason


@pytest.fixture
def rig(
    contacts: SqlContactRepository,
    messages: SqlMessageRepository,
    outbox: SqlOutboxRepository,
    modes: SqlModeRepository,
) -> Rig:
    return Rig(contacts, messages, outbox, modes)


def _incoming(text: str, tg_message_id: int = 1, sent_at: datetime = SENT_AT) -> IncomingMessage:
    return IncomingMessage(
        tg_id=FAKE_TELEGRAM_ID,
        username=USERNAME,
        display_name=DISPLAY_NAME,
        tg_message_id=tg_message_id,
        text=text,
        sent_at=sent_at,
    )


async def test_an_active_contact_writing_a_trigger_is_handed_over_right_after_storing(
    rig: Rig,
) -> None:
    contact = await rig.contact(ContactStatus.ACTIVE)
    row = await rig.outbox.enqueue(
        contact.id, "intake-queued-zq", FAKE_NOW + timedelta(hours=1), OutboxSource.MANUAL_TEST
    )
    spy = rig.spy()

    result = await rig.intake(spy).handle(_incoming(TRIGGER_TEXT))

    assert result == IntakeResult(
        contact_id=contact.id, message_id=result.message_id, contact_created=False
    )
    [stored] = await rig.messages.list_last(contact.id, 10)
    assert (stored.direction, stored.text) == (MessageDirection.INCOMING, TRIGGER_TEXT)
    assert spy.calls == [(contact.id, frozenset({TriggerClass.MEETING}), TriggerSource.TEXT)]
    assert await rig.status_of(contact) == (ContactStatus.PAUSED, PauseReason.STOP_TRIGGER)
    cancelled = await rig.outbox.get(row.id)
    assert cancelled is not None
    assert (cancelled.state, cancelled.cancel_reason) == (
        OutboxState.CANCELLED,
        CANCEL_REASON_STOP_TRIGGER,
    )
    [notice] = rig.notifier.notices
    assert notice.lines[-1] == DialogueLine(direction=MessageDirection.INCOMING, text=TRIGGER_TEXT)


async def test_a_harmless_text_from_an_active_contact_is_not_handed_over(rig: Rig) -> None:
    contact = await rig.contact(ContactStatus.ACTIVE)
    spy = rig.spy()

    await rig.intake(spy).handle(_incoming(HARMLESS_TEXT))

    assert spy.calls == []
    assert await rig.status_of(contact) == (ContactStatus.ACTIVE, None)
    assert rig.notifier.notices == []


@pytest.mark.parametrize("status", NOT_ACTIVE)
async def test_a_contact_that_is_not_active_is_not_checked(rig: Rig, status: ContactStatus) -> None:
    contact = await rig.contact(status)
    spy = rig.spy()

    await rig.intake(spy).handle(_incoming(TRIGGER_TEXT))

    assert spy.calls == []
    assert (await rig.status_of(contact))[0] is status
    assert len(await rig.messages.list_last(contact.id, 10)) == 1


async def test_a_new_sender_with_a_trigger_text_is_registered_and_not_checked(rig: Rig) -> None:
    spy = rig.spy()

    result = await rig.intake(spy).handle(_incoming(TRIGGER_TEXT))

    assert result.contact_created is True
    assert spy.calls == []
    contact = await rig.contacts.get_by_id(result.contact_id)
    assert contact is not None
    assert contact.status is ContactStatus.NEW


async def test_only_the_new_message_is_checked_so_a_resume_does_not_retrigger_on_old_text(
    rig: Rig,
) -> None:
    contact = await rig.contact(ContactStatus.ACTIVE)
    spy = rig.spy()
    intake = rig.intake(spy)
    await intake.handle(_incoming(TRIGGER_TEXT, tg_message_id=1))
    await rig.contacts.set_status(contact.id, ContactStatus.ACTIVE)

    await intake.handle(_incoming(HARMLESS_TEXT, tg_message_id=2, sent_at=SENT_AT + timedelta(1)))

    assert len(spy.calls) == 1
    assert await rig.status_of(contact) == (ContactStatus.ACTIVE, None)
    assert len(rig.notifier.notices) == 1


async def test_replaying_the_same_incoming_message_hands_over_once(rig: Rig) -> None:
    contact = await rig.contact(ContactStatus.ACTIVE)
    intake = rig.intake(rig.spy())

    await intake.handle(_incoming(TRIGGER_TEXT))
    await intake.handle(_incoming(TRIGGER_TEXT))

    assert len(rig.notifier.notices) == 1
    assert len(await rig.messages.list_last(contact.id, 10)) == 1
    assert await rig.status_of(contact) == (ContactStatus.PAUSED, PauseReason.STOP_TRIGGER)


async def test_a_failing_hand_over_never_breaks_the_intake(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    contact = await rig.contact(ContactStatus.ACTIVE)
    spy = rig.spy(error=RuntimeError(TRIGGER_TEXT))

    with caplog.at_level(logging.DEBUG):
        result = await rig.intake(spy).handle(_incoming(TRIGGER_TEXT))

    assert result.contact_id == contact.id
    assert len(await rig.messages.list_last(contact.id, 10)) == 1
    [warning] = [r for r in caplog.records if r.getMessage() == "stop-trigger check failed"]
    assert warning.__dict__["error"] == "RuntimeError"
    assert_absent(TRIGGER_TEXT, "\n".join(JsonFormatter().format(r) for r in caplog.records))


async def test_a_failing_detector_never_breaks_the_intake(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    contact = await rig.contact(ContactStatus.ACTIVE)

    def explode(texts: object) -> object:
        raise ValueError("fake detector failure")

    monkeypatch.setattr(intake_module, "detect", explode)

    result = await rig.intake(rig.spy()).handle(_incoming(TRIGGER_TEXT))

    assert result.contact_id == contact.id
    assert await rig.status_of(contact) == (ContactStatus.ACTIVE, None)


async def test_a_failing_gate_does_not_make_the_userbot_retry_the_intake(rig: Rig) -> None:
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
    event = FakeEvent(id=5, raw_text=TRIGGER_TEXT, sender=sender, chat=sender)

    await handler.handle(event)

    assert _Counting.calls == 1
    assert sleep.delays == []
    assert (handler.handled, handler.failed) == (1, 0)
    assert event.write_attempts == []


async def test_without_a_hand_over_service_the_intake_behaves_as_before(rig: Rig) -> None:
    contact = await rig.contact(ContactStatus.ACTIVE)

    await rig.intake(None).handle(_incoming(TRIGGER_TEXT))

    assert await rig.status_of(contact) == (ContactStatus.ACTIVE, None)


async def test_the_intake_path_logs_no_text_name_or_telegram_id(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    await rig.contact(ContactStatus.ACTIVE)

    with caplog.at_level(logging.DEBUG):
        await rig.intake(rig.spy()).handle(_incoming(TRIGGER_TEXT))

    output = "\n".join(JsonFormatter().format(record) for record in caplog.records)
    assert output
    for marker in (TRIGGER_TEXT, USERNAME, DISPLAY_NAME, FAKE_TELEGRAM_ID):
        assert_absent(marker, output)
