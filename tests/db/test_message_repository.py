from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError

from cyrano.db.models import MessageRow
from cyrano.db.repositories import SqlContactRepository, SqlMessageRepository
from cyrano.db.session import SessionFactory
from cyrano.domain.enums import MessageDirection
from cyrano.domain.errors import ContactNotFound
from cyrano.domain.models import MAX_BIGINT, Contact, ContactCreate, Message, MessageCreate

FAKE_TEXT = "fake-message-text-that-must-not-leak"
MISSING_ID = 999_999


def _input(contact_id: int, tg_message_id: int, message_text: str = FAKE_TEXT) -> MessageCreate:
    return MessageCreate(
        contact_id=contact_id,
        direction=MessageDirection.INCOMING,
        text=message_text,
        tg_message_id=tg_message_id,
    )


@pytest.fixture
async def contact(contacts: SqlContactRepository) -> Contact:
    return await contacts.create(ContactCreate(tg_id=100))


@pytest.fixture
async def other(contacts: SqlContactRepository) -> Contact:
    return await contacts.create(ContactCreate(tg_id=101))


async def _count(session_factory: SessionFactory) -> int | None:
    async with session_factory() as session:
        return await session.scalar(select(func.count()).select_from(MessageRow))


async def test_append_returns_the_stored_message(
    messages: SqlMessageRepository, contact: Contact
) -> None:
    message = await messages.append(
        MessageCreate(
            contact_id=contact.id,
            direction=MessageDirection.OUTGOING,
            text="fake кириллица 🙂",
            tg_message_id=MAX_BIGINT,
        )
    )

    assert isinstance(message, Message)
    assert message.contact_id == contact.id
    assert message.direction is MessageDirection.OUTGOING
    assert message.text == "fake кириллица 🙂"
    assert message.tg_message_id == MAX_BIGINT
    assert message.created_at.utcoffset() == timedelta(0)


async def test_append_stores_empty_text(messages: SqlMessageRepository, contact: Contact) -> None:
    assert (await messages.append(_input(contact.id, 1, ""))).text == ""


async def test_duplicate_telegram_message_is_stored_once(
    messages: SqlMessageRepository, contact: Contact, session_factory: SessionFactory
) -> None:
    first = await messages.append(_input(contact.id, 1, "original"))

    second = await messages.append(_input(contact.id, 1, "repeated event"))

    assert second == first
    assert second.text == "original"
    assert await _count(session_factory) == 1


async def test_concurrent_duplicates_are_stored_once(
    messages: SqlMessageRepository, contact: Contact, session_factory: SessionFactory
) -> None:
    results = await asyncio.gather(*(messages.append(_input(contact.id, 1)) for _ in range(5)))

    assert {message.id for message in results} == {results[0].id}
    assert await _count(session_factory) == 1


async def test_same_telegram_id_for_another_contact_is_a_new_message(
    messages: SqlMessageRepository, contacts: SqlContactRepository, contact: Contact
) -> None:
    other = await contacts.create(ContactCreate(tg_id=200))

    first = await messages.append(_input(contact.id, 1))
    second = await messages.append(_input(other.id, 1))

    assert first.id != second.id


async def test_append_for_a_missing_contact_raises_without_the_text(
    messages: SqlMessageRepository,
) -> None:
    with pytest.raises(ContactNotFound) as caught:
        await messages.append(_input(MISSING_ID, 1))

    assert FAKE_TEXT not in str(caught.value)
    assert caught.value.__suppress_context__ is True


async def test_list_last_returns_the_newest_in_chronological_order(
    messages: SqlMessageRepository, contacts: SqlContactRepository, contact: Contact
) -> None:
    other = await contacts.create(ContactCreate(tg_id=200))
    await messages.append(_input(other.id, 99))
    for tg_message_id in range(1, 6):
        await messages.append(_input(contact.id, tg_message_id))

    last = await messages.list_last(contact.id, 3)

    assert [message.tg_message_id for message in last] == [3, 4, 5]


async def test_list_last_orders_by_created_at_not_by_insertion(
    messages: SqlMessageRepository, contact: Contact, session_factory: SessionFactory
) -> None:
    first = await messages.append(_input(contact.id, 1))
    second = await messages.append(_input(contact.id, 2))
    async with session_factory.begin() as session:
        await session.execute(
            update(MessageRow)
            .where(MessageRow.id == first.id)
            .values(created_at=second.created_at + timedelta(seconds=1))
        )

    last = await messages.list_last(contact.id, 2)

    assert [message.tg_message_id for message in last] == [2, 1]


async def test_list_last_breaks_timestamp_ties_by_id(
    messages: SqlMessageRepository, contact: Contact, session_factory: SessionFactory
) -> None:
    for tg_message_id in (3, 1, 2):
        await messages.append(_input(contact.id, tg_message_id))
    async with session_factory.begin() as session:
        await session.execute(
            update(MessageRow).values(created_at=datetime(2026, 1, 1, tzinfo=UTC))
        )

    last = await messages.list_last(contact.id, 2)

    assert [message.tg_message_id for message in last] == [1, 2]


@pytest.mark.parametrize("limit", [0, -1])
async def test_list_last_with_a_non_positive_limit_is_empty(
    messages: SqlMessageRepository, contact: Contact, limit: int
) -> None:
    await messages.append(_input(contact.id, 1))

    assert await messages.list_last(contact.id, limit) == []


async def test_list_last_for_a_contact_without_messages_is_empty(
    messages: SqlMessageRepository, contact: Contact
) -> None:
    assert await messages.list_last(contact.id, 10) == []


async def test_message_text_is_not_logged(
    messages: SqlMessageRepository, contact: Contact, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    await messages.append(_input(contact.id, 1))
    await messages.append(_input(contact.id, 1))
    await messages.list_last(contact.id, 1)

    assert FAKE_TEXT not in caplog.text


async def test_driver_errors_do_not_carry_the_statement_parameters(
    session_factory: SessionFactory,
) -> None:
    statement = text(
        "INSERT INTO messages (contact_id, direction, text, tg_message_id) "
        "VALUES (:contact_id, 'incoming', :text, 1)"
    )

    with pytest.raises(IntegrityError) as caught:
        async with session_factory.begin() as session:
            await session.execute(statement, {"contact_id": MISSING_ID, "text": FAKE_TEXT})

    assert FAKE_TEXT not in str(caught.value)


async def _stored(
    messages: SqlMessageRepository,
    session_factory: SessionFactory,
    contact_id: int,
    tg_message_id: int,
    direction: MessageDirection,
    created_at: datetime,
) -> None:
    stored = await messages.append(
        MessageCreate(
            contact_id=contact_id,
            direction=direction,
            text=FAKE_TEXT,
            tg_message_id=tg_message_id,
        )
    )
    async with session_factory.begin() as session:
        await session.execute(
            update(MessageRow).where(MessageRow.id == stored.id).values(created_at=created_at)
        )


WINDOW_NOW = datetime(2025, 3, 4, 5, 6, 7, tzinfo=UTC)


def _before(seconds: int) -> datetime:
    return WINDOW_NOW - timedelta(seconds=seconds)


async def test_outgoing_times_reads_only_outgoing_messages_newest_first_from_the_window_start(
    messages: SqlMessageRepository, session_factory: SessionFactory, contact: Contact
) -> None:
    outgoing = MessageDirection.OUTGOING
    await _stored(messages, session_factory, contact.id, 1, outgoing, _before(30))
    await _stored(messages, session_factory, contact.id, 2, outgoing, _before(10))
    await _stored(messages, session_factory, contact.id, 3, outgoing, _before(60))
    await _stored(messages, session_factory, contact.id, 4, outgoing, _before(61))
    await _stored(messages, session_factory, contact.id, 5, MessageDirection.INCOMING, _before(5))

    times = await messages.outgoing_times(_before(60), 10)

    assert times == [_before(10), _before(30), _before(60)]


async def test_outgoing_times_returns_at_most_the_limit_keeping_the_most_recent(
    messages: SqlMessageRepository, session_factory: SessionFactory, contact: Contact
) -> None:
    for number, seconds in enumerate((40, 10, 30, 20), start=1):
        await _stored(
            messages,
            session_factory,
            contact.id,
            number,
            MessageDirection.OUTGOING,
            _before(seconds),
        )

    assert await messages.outgoing_times(_before(60), 2) == [_before(10), _before(20)]


async def test_outgoing_times_for_a_contact_does_not_mix_contacts_and_global_adds_them(
    messages: SqlMessageRepository,
    session_factory: SessionFactory,
    contact: Contact,
    other: Contact,
) -> None:
    outgoing = MessageDirection.OUTGOING
    await _stored(messages, session_factory, contact.id, 1, outgoing, _before(10))
    await _stored(messages, session_factory, other.id, 2, outgoing, _before(20))

    assert await messages.outgoing_times(_before(60), 10, contact.id) == [_before(10)]
    assert await messages.outgoing_times(_before(60), 10, other.id) == [_before(20)]
    assert await messages.outgoing_times(_before(60), 10) == [_before(10), _before(20)]


async def test_outgoing_times_is_empty_without_history_and_refuses_a_naive_start(
    messages: SqlMessageRepository,
) -> None:
    assert await messages.outgoing_times(_before(60), 10) == []
    with pytest.raises(ValueError, match="timezone-aware"):
        await messages.outgoing_times(datetime(2025, 1, 1), 10)
    with pytest.raises(ValueError, match="timezone-aware"):
        await messages.busiest_outgoing_contact(datetime(2025, 1, 1))


async def test_outgoing_times_returns_utc(
    messages: SqlMessageRepository, session_factory: SessionFactory, contact: Contact
) -> None:
    await _stored(messages, session_factory, contact.id, 1, MessageDirection.OUTGOING, _before(1))

    times = await messages.outgoing_times(_before(60), 1)

    assert times[0].utcoffset() == timedelta(0)


async def test_the_busiest_contact_counts_outgoing_messages_after_the_start_only(
    messages: SqlMessageRepository,
    session_factory: SessionFactory,
    contact: Contact,
    other: Contact,
) -> None:
    outgoing = MessageDirection.OUTGOING
    await _stored(messages, session_factory, contact.id, 1, outgoing, _before(10))
    await _stored(messages, session_factory, other.id, 2, outgoing, _before(20))
    await _stored(messages, session_factory, other.id, 3, outgoing, _before(30))
    await _stored(messages, session_factory, contact.id, 4, MessageDirection.INCOMING, _before(1))
    await _stored(messages, session_factory, contact.id, 5, MessageDirection.INCOMING, _before(2))
    await _stored(messages, session_factory, contact.id, 6, outgoing, _before(60))
    await _stored(messages, session_factory, contact.id, 7, outgoing, _before(70))

    peak = await messages.busiest_outgoing_contact(_before(60))

    assert peak is not None
    assert (peak.contact_id, peak.count) == (other.id, 2)
    assert await messages.busiest_outgoing_contact(WINDOW_NOW) is None


async def test_the_busiest_contact_breaks_a_tie_by_the_lower_id(
    messages: SqlMessageRepository,
    session_factory: SessionFactory,
    contact: Contact,
    other: Contact,
) -> None:
    outgoing = MessageDirection.OUTGOING
    await _stored(messages, session_factory, other.id, 1, outgoing, _before(10))
    await _stored(messages, session_factory, contact.id, 2, outgoing, _before(20))

    peak = await messages.busiest_outgoing_contact(_before(60))

    assert peak is not None
    assert peak.contact_id == min(contact.id, other.id)


async def test_exists_is_false_for_an_unknown_message(
    messages: SqlMessageRepository, contact: Contact
) -> None:
    assert await messages.exists(contact.id, 1) is False


@pytest.mark.parametrize("direction", list(MessageDirection))
async def test_exists_finds_a_message_of_either_direction(
    direction: MessageDirection, messages: SqlMessageRepository, contact: Contact
) -> None:
    await messages.append(
        MessageCreate(contact_id=contact.id, direction=direction, text="fake", tg_message_id=7)
    )

    assert await messages.exists(contact.id, 7) is True
    assert await messages.exists(contact.id, 8) is False


async def test_exists_does_not_mix_contacts(
    messages: SqlMessageRepository, contact: Contact, other: Contact
) -> None:
    await messages.append(_input(contact.id, 7))

    assert await messages.exists(other.id, 7) is False
    assert await messages.exists(contact.id, 7) is True


async def test_append_keeps_a_given_creation_time(
    messages: SqlMessageRepository, contact: Contact
) -> None:
    sent_at = datetime(2025, 3, 4, 5, 6, 7, tzinfo=UTC)

    message = await messages.append(
        MessageCreate(
            contact_id=contact.id,
            direction=MessageDirection.OUTGOING,
            text="",
            tg_message_id=9,
            created_at=sent_at,
        )
    )

    assert message.created_at == sent_at
    assert message.text == ""


async def test_append_without_a_creation_time_uses_the_database_clock(
    messages: SqlMessageRepository, contact: Contact
) -> None:
    before = datetime.now(UTC) - timedelta(minutes=1)

    message = await messages.append(_input(contact.id, 9))

    assert message.created_at > before


async def test_a_message_with_a_given_time_is_not_overwritten_by_a_repeat(
    messages: SqlMessageRepository, contact: Contact
) -> None:
    sent_at = datetime(2025, 3, 4, 5, 6, 7, tzinfo=UTC)
    first = await messages.append(
        MessageCreate(
            contact_id=contact.id,
            direction=MessageDirection.OUTGOING,
            text="first",
            tg_message_id=9,
            created_at=sent_at,
        )
    )

    second = await messages.append(
        MessageCreate(
            contact_id=contact.id,
            direction=MessageDirection.OUTGOING,
            text="repeat",
            tg_message_id=9,
            created_at=sent_at + timedelta(hours=1),
        )
    )

    assert second == first


async def test_storing_a_message_does_not_move_the_last_message_time(
    messages: SqlMessageRepository, contacts: SqlContactRepository, contact: Contact
) -> None:
    await messages.append(
        MessageCreate(
            contact_id=contact.id,
            direction=MessageDirection.OUTGOING,
            text="fake",
            tg_message_id=9,
            created_at=datetime(2025, 3, 4, 5, 6, 7, tzinfo=UTC),
        )
    )

    stored = await contacts.get_by_id(contact.id)
    assert stored is not None
    assert stored.last_message_at is None
