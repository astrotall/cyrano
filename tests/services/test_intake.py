from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import pytest

from cyrano.db.repositories import SqlContactRepository, SqlMessageRepository
from cyrano.db.session import SessionFactory
from cyrano.domain.enums import ContactStatus, MessageDirection
from cyrano.domain.errors import ContactAlreadyExists
from cyrano.domain.models import (
    MAX_FUTURE_SKEW,
    Contact,
    ContactCreate,
    IncomingMessage,
    NewContactEvent,
)
from cyrano.services.contact_events import InMemoryContactEvents
from cyrano.services.intake import IntakeResult, IntakeService
from cyrano.services.message_time import message_time
from tests.privacy import FAKE_TELEGRAM_ID, assert_absent
from tests.services.outbox_fakes import FakeClock

FAKE_TEXT = "fake-intake-text-that-must-not-leak"
FAKE_NAME = "Fakeintake Person"
FIRST_SENT_AT = datetime(2025, 3, 1, 12, 0, tzinfo=UTC)
CLOCK_NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
ONE_SECOND = timedelta(seconds=1)


def _incoming(
    tg_id: int = FAKE_TELEGRAM_ID,
    tg_message_id: int = 1,
    text: str = FAKE_TEXT,
    sent_at: datetime | None = FIRST_SENT_AT,
    username: str | None = "fake_intake_user",
    display_name: str | None = FAKE_NAME,
) -> IncomingMessage:
    return IncomingMessage(
        tg_id=tg_id,
        username=username,
        display_name=display_name,
        tg_message_id=tg_message_id,
        text=text,
        sent_at=sent_at,
    )


@pytest.fixture
def events() -> InMemoryContactEvents[NewContactEvent]:
    return InMemoryContactEvents[NewContactEvent]()


@pytest.fixture
def intake(
    contacts: SqlContactRepository,
    messages: SqlMessageRepository,
    events: InMemoryContactEvents[NewContactEvent],
) -> IntakeService:
    return IntakeService(contacts, messages, events)


async def _drain(
    events: InMemoryContactEvents[NewContactEvent], count: int
) -> list[NewContactEvent]:
    subscription = events.subscribe()
    return [await asyncio.wait_for(anext(subscription), timeout=1) for _ in range(count)]


async def test_new_sender_creates_a_new_contact_stores_the_message_and_publishes_one_event(
    intake: IntakeService,
    contacts: SqlContactRepository,
    messages: SqlMessageRepository,
    events: InMemoryContactEvents[NewContactEvent],
) -> None:
    result = await intake.handle(_incoming())

    assert isinstance(result, IntakeResult)
    assert result.contact_created is True
    contact = await contacts.get_by_tg_id(FAKE_TELEGRAM_ID)
    assert contact is not None
    assert contact.id == result.contact_id
    assert contact.status is ContactStatus.NEW
    assert contact.username == "fake_intake_user"
    assert contact.display_name == FAKE_NAME
    assert contact.last_message_at == FIRST_SENT_AT
    stored = await messages.list_last(contact.id, 10)
    assert [(m.text, m.tg_message_id, m.direction) for m in stored] == [
        (FAKE_TEXT, 1, MessageDirection.INCOMING)
    ]
    assert result.message_id == stored[0].id
    assert await _drain(events, 1) == [
        NewContactEvent(contact_id=contact.id, message_id=stored[0].id)
    ]
    assert events.published == 1


async def test_second_message_from_the_same_sender_creates_no_contact_and_no_event(
    intake: IntakeService,
    contacts: SqlContactRepository,
    messages: SqlMessageRepository,
    events: InMemoryContactEvents[NewContactEvent],
) -> None:
    first = await intake.handle(_incoming(tg_message_id=1))
    second = await intake.handle(
        _incoming(tg_message_id=2, text="second", sent_at=FIRST_SENT_AT + timedelta(minutes=1))
    )

    assert second.contact_created is False
    assert second.contact_id == first.contact_id
    assert len(await contacts.list_by_status(ContactStatus.NEW)) == 1
    assert [m.tg_message_id for m in await messages.list_last(first.contact_id, 10)] == [1, 2]
    assert events.published == 1


async def test_redelivered_message_is_stored_once_and_publishes_nothing(
    intake: IntakeService,
    messages: SqlMessageRepository,
    events: InMemoryContactEvents[NewContactEvent],
) -> None:
    first = await intake.handle(_incoming())
    again = await intake.handle(_incoming(text="redelivered with other text"))

    assert again.contact_created is False
    assert again.message_id == first.message_id
    stored = await messages.list_last(first.contact_id, 10)
    assert [m.text for m in stored] == [FAKE_TEXT]
    assert events.published == 1


async def test_two_concurrent_first_messages_give_one_contact_and_one_event(
    intake: IntakeService,
    contacts: SqlContactRepository,
    messages: SqlMessageRepository,
    events: InMemoryContactEvents[NewContactEvent],
) -> None:
    results = await asyncio.gather(
        intake.handle(_incoming(tg_message_id=1)),
        intake.handle(_incoming(tg_message_id=2, sent_at=FIRST_SENT_AT + timedelta(seconds=1))),
    )

    assert len(await contacts.list_by_status(ContactStatus.NEW)) == 1
    assert len({r.contact_id for r in results}) == 1
    assert sum(r.contact_created for r in results) == 1
    assert len(await messages.list_last(results[0].contact_id, 10)) == 2
    assert events.published == 1


class _RacingContacts(SqlContactRepository):
    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self._barrier = asyncio.Barrier(2)
        self._waits_left = 2
        self.conflicts = 0

    async def create(self, data: ContactCreate) -> Contact:
        try:
            return await super().create(data)
        except ContactAlreadyExists:
            self.conflicts += 1
            raise

    async def get_by_tg_id(self, tg_id: int) -> Contact | None:
        contact = await super().get_by_tg_id(tg_id)
        if contact is None and self._waits_left > 0:
            self._waits_left -= 1
            await self._barrier.wait()
        return contact


class _VanishingContacts(SqlContactRepository):
    async def get_by_tg_id(self, tg_id: int) -> Contact | None:
        return None

    async def create(self, data: ContactCreate) -> Contact:
        raise ContactAlreadyExists


async def test_losing_the_creation_race_continues_as_for_a_known_sender(
    session_factory: SessionFactory,
    contacts: SqlContactRepository,
    messages: SqlMessageRepository,
    events: InMemoryContactEvents[NewContactEvent],
) -> None:
    racing = _RacingContacts(session_factory)
    service = IntakeService(racing, messages, events)

    results = await asyncio.gather(
        service.handle(_incoming(tg_message_id=1)),
        service.handle(_incoming(tg_message_id=2)),
    )

    assert racing.conflicts == 1
    assert sum(r.contact_created for r in results) == 1
    assert len({r.contact_id for r in results}) == 1
    assert len(await contacts.list_by_status(ContactStatus.NEW)) == 1
    assert len(await messages.list_last(results[0].contact_id, 10)) == 2
    assert events.published == 1


async def test_conflict_without_a_readable_contact_is_not_swallowed(
    session_factory: SessionFactory,
    messages: SqlMessageRepository,
    events: InMemoryContactEvents[NewContactEvent],
) -> None:
    service = IntakeService(_VanishingContacts(session_factory), messages, events)

    with pytest.raises(ContactAlreadyExists):
        await service.handle(_incoming())

    assert events.published == 0


async def test_media_only_message_is_registered_with_empty_text(
    intake: IntakeService,
    contacts: SqlContactRepository,
    messages: SqlMessageRepository,
    events: InMemoryContactEvents[NewContactEvent],
) -> None:
    result = await intake.handle(_incoming(text=""))

    assert result.contact_created is True
    contact = await contacts.get_by_tg_id(FAKE_TELEGRAM_ID)
    assert contact is not None
    assert [m.text for m in await messages.list_last(contact.id, 10)] == [""]
    assert events.published == 1


async def test_sender_without_username_and_name_is_registered(
    intake: IntakeService, contacts: SqlContactRepository
) -> None:
    await intake.handle(_incoming(username=None, display_name=None))

    contact = await contacts.get_by_tg_id(FAKE_TELEGRAM_ID)
    assert contact is not None
    assert contact.username is None
    assert contact.display_name is None


async def test_last_message_at_only_moves_forward(
    intake: IntakeService, contacts: SqlContactRepository
) -> None:
    later = FIRST_SENT_AT + timedelta(hours=2)
    await intake.handle(_incoming(tg_message_id=2, sent_at=later))
    await intake.handle(_incoming(tg_message_id=1, sent_at=FIRST_SENT_AT))

    contact = await contacts.get_by_tg_id(FAKE_TELEGRAM_ID)
    assert contact is not None
    assert contact.last_message_at == later


async def test_nul_bytes_are_removed_before_anything_is_stored(
    intake: IntakeService, contacts: SqlContactRepository, messages: SqlMessageRepository
) -> None:
    result = await intake.handle(
        _incoming(text="te\x00xt", username="us\x00er", display_name="\x00na\x00me")
    )

    contact = await contacts.get_by_tg_id(FAKE_TELEGRAM_ID)
    assert contact is not None
    assert (contact.username, contact.display_name) == ("user", "name")
    stored = await messages.list_last(result.contact_id, 10)
    assert [m.text for m in stored] == ["text"]


async def test_a_full_event_queue_does_not_break_intake(
    contacts: SqlContactRepository, messages: SqlMessageRepository
) -> None:
    full = InMemoryContactEvents[NewContactEvent](max_size=1)
    service = IntakeService(contacts, messages, full)

    first = await service.handle(_incoming(tg_id=FAKE_TELEGRAM_ID))
    second = await service.handle(_incoming(tg_id=7002))

    assert first.contact_created is True
    assert second.contact_created is True
    assert full.published == 1
    assert full.dropped == 1
    assert len(await contacts.list_by_status(ContactStatus.NEW)) == 2


async def test_intake_logs_no_message_text_names_or_telegram_ids(
    intake: IntakeService, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        await intake.handle(_incoming())

    logged = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    assert "cyrano.services.intake" in {r.name for r in caplog.records}
    for personal in (FAKE_TEXT, FAKE_NAME, "fake_intake_user", str(FAKE_TELEGRAM_ID)):
        assert_absent(personal, logged)


def _clocked(
    contacts: SqlContactRepository,
    messages: SqlMessageRepository,
    events: InMemoryContactEvents[NewContactEvent],
    clock: FakeClock,
) -> IntakeService:
    return IntakeService(contacts, messages, events, clock=clock)


async def _stored_times(
    messages: SqlMessageRepository, contacts: SqlContactRepository
) -> tuple[list[datetime], datetime | None]:
    contact = await contacts.get_by_tg_id(FAKE_TELEGRAM_ID)
    assert contact is not None
    stored = await messages.list_last(contact.id, 10)
    return [message.created_at for message in stored], contact.last_message_at


async def test_an_old_message_is_stored_with_its_telegram_time(
    contacts: SqlContactRepository,
    messages: SqlMessageRepository,
    events: InMemoryContactEvents[NewContactEvent],
) -> None:
    clock = FakeClock(CLOCK_NOW)
    sent_at = CLOCK_NOW - timedelta(hours=8)

    await _clocked(contacts, messages, events, clock).handle(_incoming(sent_at=sent_at))

    assert await _stored_times(messages, contacts) == ([sent_at], sent_at)


@pytest.mark.parametrize(
    ("offset", "kept"),
    [
        (timedelta(0), True),
        (MAX_FUTURE_SKEW - ONE_SECOND, True),
        (MAX_FUTURE_SKEW, True),
        (MAX_FUTURE_SKEW + ONE_SECOND, False),
        (timedelta(days=2), False),
    ],
)
async def test_a_future_time_beyond_the_skew_is_replaced_by_now(
    contacts: SqlContactRepository,
    messages: SqlMessageRepository,
    events: InMemoryContactEvents[NewContactEvent],
    offset: timedelta,
    kept: bool,
) -> None:
    clock = FakeClock(CLOCK_NOW)
    sent_at = CLOCK_NOW + offset

    await _clocked(contacts, messages, events, clock).handle(_incoming(sent_at=sent_at))

    expected = sent_at if kept else CLOCK_NOW
    assert await _stored_times(messages, contacts) == ([expected], expected)


@pytest.mark.parametrize("missing", [None, datetime(2026, 11, 4, 11, 0)])
async def test_a_missing_or_naive_time_falls_back_to_now(
    contacts: SqlContactRepository,
    messages: SqlMessageRepository,
    events: InMemoryContactEvents[NewContactEvent],
    missing: datetime | None,
) -> None:
    clock = FakeClock(CLOCK_NOW)

    await _clocked(contacts, messages, events, clock).handle(_incoming(sent_at=missing))

    assert await _stored_times(messages, contacts) == ([CLOCK_NOW], CLOCK_NOW)


def test_message_time_keeps_a_past_time_and_replaces_a_missing_or_far_future_one() -> None:
    past = CLOCK_NOW - timedelta(hours=1)

    assert message_time(past, CLOCK_NOW) == past
    assert message_time(None, CLOCK_NOW) == CLOCK_NOW
    assert message_time(CLOCK_NOW + MAX_FUTURE_SKEW, CLOCK_NOW) == CLOCK_NOW + MAX_FUTURE_SKEW
    assert message_time(CLOCK_NOW + MAX_FUTURE_SKEW + ONE_SECOND, CLOCK_NOW) == CLOCK_NOW


async def test_an_older_replayed_message_does_not_move_last_message_at_backwards(
    contacts: SqlContactRepository,
    messages: SqlMessageRepository,
    events: InMemoryContactEvents[NewContactEvent],
) -> None:
    clock = FakeClock(CLOCK_NOW)
    service = _clocked(contacts, messages, events, clock)
    newer = CLOCK_NOW - timedelta(hours=1)
    older = CLOCK_NOW - timedelta(hours=5)

    await service.handle(_incoming(tg_message_id=2, sent_at=newer))
    await service.handle(_incoming(tg_message_id=1, sent_at=older))

    assert await _stored_times(messages, contacts) == ([older, newer], newer)


async def test_a_second_delivery_of_the_same_message_changes_nothing(
    contacts: SqlContactRepository,
    messages: SqlMessageRepository,
    events: InMemoryContactEvents[NewContactEvent],
) -> None:
    clock = FakeClock(CLOCK_NOW)
    service = _clocked(contacts, messages, events, clock)
    future = _incoming(tg_message_id=7, sent_at=CLOCK_NOW + timedelta(days=1))

    first = await service.handle(future)
    clock.advance(3600)
    second = await service.handle(future)

    assert second.message_id == first.message_id
    assert await _stored_times(messages, contacts) == ([CLOCK_NOW], CLOCK_NOW)
