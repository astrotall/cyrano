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
