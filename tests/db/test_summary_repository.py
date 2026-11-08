from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from cyrano.db.repositories import (
    SqlContactRepository,
    SqlMessageRepository,
    SqlModeRepository,
    SqlSummaryRepository,
)
from cyrano.db.session import SessionFactory
from cyrano.domain.enums import ContactStatus, MessageDirection, PauseReason
from cyrano.domain.errors import ContactNotFound
from cyrano.domain.models import DEFAULT_MODE_NAME, ContactCreate, ContactUpdate, MessageCreate
from cyrano.domain.summary import (
    SUMMARY_VERSION,
    ContactSummary,
    StoredSummary,
    SummaryCursor,
    read_stored_summary,
)
from cyrano.services.repositories import SummaryRepository

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
COOLDOWN = timedelta(minutes=10)
BASE_TG_ID = 700000000001


@pytest.fixture
def summaries(session_factory: SessionFactory) -> SqlSummaryRepository:
    return SqlSummaryRepository(session_factory)


def test_the_sql_repository_satisfies_the_protocol(summaries: SqlSummaryRepository) -> None:
    protocol: SummaryRepository = summaries
    assert protocol is summaries


async def _contact(
    contacts: SqlContactRepository,
    modes: SqlModeRepository,
    number: int,
    status: ContactStatus = ContactStatus.ACTIVE,
) -> int:
    created = await contacts.create(ContactCreate(tg_id=BASE_TG_ID + number))
    if status is ContactStatus.NEW:
        return created.id
    await contacts.set_status(created.id, ContactStatus.PENDING)
    if status is ContactStatus.PENDING:
        return created.id
    if status is ContactStatus.IGNORED:
        await contacts.set_status(created.id, ContactStatus.IGNORED)
        return created.id
    default = await modes.get_by_name(DEFAULT_MODE_NAME)
    assert default is not None
    await contacts.set_status(created.id, ContactStatus.ACTIVE, mode_id=default.id)
    if status is ContactStatus.PAUSED:
        await contacts.set_status(created.id, ContactStatus.PAUSED, pause_reason=PauseReason.OWNER)
    return created.id


async def _say(
    messages: SqlMessageRepository,
    contact_id: int,
    number: int,
    at: datetime,
    direction: MessageDirection = MessageDirection.INCOMING,
) -> int:
    stored = await messages.append(
        MessageCreate(
            contact_id=contact_id,
            direction=direction,
            text=f"invented text {number}",
            tg_message_id=number,
            created_at=at,
        )
    )
    return stored.id


def _stored(cursor: SummaryCursor, name: str = "Invented") -> StoredSummary:
    return StoredSummary(
        version=SUMMARY_VERSION,
        updated_at=NOW,
        cursor=cursor,
        data=ContactSummary(name=name),
    )


async def test_messages_after_no_cursor_start_at_the_beginning_in_time_and_id_order(
    contacts: SqlContactRepository,
    modes: SqlModeRepository,
    messages: SqlMessageRepository,
    summaries: SqlSummaryRepository,
) -> None:
    contact_id = await _contact(contacts, modes, 1)
    late = await _say(messages, contact_id, 1, NOW)
    early = await _say(messages, contact_id, 2, NOW - timedelta(minutes=5))
    tie_a = await _say(messages, contact_id, 3, NOW - timedelta(minutes=1))
    tie_b = await _say(messages, contact_id, 4, NOW - timedelta(minutes=1))

    found = await summaries.messages_after(contact_id, None, 10)

    assert [m.id for m in found] == [early, tie_a, tie_b, late]
    assert [m.id for m in await summaries.messages_after(contact_id, None, 2)] == [early, tie_a]
    assert await summaries.messages_after(contact_id, None, 0) == []


async def test_messages_after_a_cursor_respect_equal_timestamps(
    contacts: SqlContactRepository,
    modes: SqlModeRepository,
    messages: SqlMessageRepository,
    summaries: SqlSummaryRepository,
) -> None:
    contact_id = await _contact(contacts, modes, 1)
    other = await _contact(contacts, modes, 2)
    at = NOW - timedelta(minutes=1)
    first = await _say(messages, contact_id, 1, at)
    second = await _say(messages, contact_id, 2, at)
    third = await _say(messages, contact_id, 3, at)
    await _say(messages, other, 4, NOW)
    cursor = SummaryCursor(created_at=at, message_id=second)

    found = await summaries.messages_after(contact_id, cursor, 10)

    assert [m.id for m in found] == [third]
    assert await summaries.count_after(contact_id, cursor) == 1
    assert await summaries.count_after(contact_id, None) == 3
    assert first < second


async def test_a_message_stored_later_but_written_earlier_is_not_after_the_cursor(
    contacts: SqlContactRepository,
    modes: SqlModeRepository,
    messages: SqlMessageRepository,
    summaries: SqlSummaryRepository,
) -> None:
    contact_id = await _contact(contacts, modes, 1)
    newest = await _say(messages, contact_id, 1, NOW)
    await _say(messages, contact_id, 2, NOW - timedelta(hours=1))
    cursor = SummaryCursor(created_at=NOW, message_id=newest)

    assert await summaries.messages_after(contact_id, cursor, 10) == []
    assert await summaries.count_after(contact_id, cursor) == 0


async def test_store_is_a_compare_and_set_on_the_cursor(
    contacts: SqlContactRepository,
    modes: SqlModeRepository,
    messages: SqlMessageRepository,
    summaries: SqlSummaryRepository,
) -> None:
    contact_id = await _contact(contacts, modes, 1)
    first_id = await _say(messages, contact_id, 1, NOW)
    second_id = await _say(messages, contact_id, 2, NOW + timedelta(seconds=1))
    first = SummaryCursor(created_at=NOW, message_id=first_id)
    second = SummaryCursor(created_at=NOW + timedelta(seconds=1), message_id=second_id)

    assert await summaries.store_summary(contact_id, _stored(first, "One"), None) is True
    assert await summaries.store_summary(contact_id, _stored(second, "Lost"), None) is False
    assert await summaries.store_summary(contact_id, _stored(second, "Wrong"), second) is False
    assert await summaries.store_summary(contact_id, _stored(second, "Two"), first) is True

    contact = await contacts.get_by_id(contact_id)
    assert contact is not None
    stored = read_stored_summary(contact.summary)
    assert stored is not None
    assert stored.cursor == second
    assert stored.data.name == "Two"


async def test_an_unreadable_stored_summary_counts_as_no_summary_for_the_compare(
    contacts: SqlContactRepository,
    modes: SqlModeRepository,
    messages: SqlMessageRepository,
    summaries: SqlSummaryRepository,
) -> None:
    contact_id = await _contact(contacts, modes, 1)
    message_id = await _say(messages, contact_id, 1, NOW)
    await contacts.update(contact_id, ContactUpdate(summary={"topic": "old shape"}))
    cursor = SummaryCursor(created_at=NOW, message_id=message_id)

    assert await summaries.store_summary(contact_id, _stored(cursor), None) is True


async def test_the_failed_marker_is_set_and_cleared_by_a_successful_store(
    contacts: SqlContactRepository,
    modes: SqlModeRepository,
    messages: SqlMessageRepository,
    summaries: SqlSummaryRepository,
) -> None:
    contact_id = await _contact(contacts, modes, 1)
    message_id = await _say(messages, contact_id, 1, NOW)

    await summaries.mark_summary_failed(contact_id, NOW)
    marked = await contacts.get_by_id(contact_id)
    assert marked is not None
    assert marked.summary_failed_at == NOW

    cursor = SummaryCursor(created_at=NOW, message_id=message_id)
    assert await summaries.store_summary(contact_id, _stored(cursor), None) is True
    stored = await contacts.get_by_id(contact_id)
    assert stored is not None
    assert stored.summary_failed_at is None


async def test_a_refused_store_keeps_the_failed_marker(
    contacts: SqlContactRepository,
    modes: SqlModeRepository,
    messages: SqlMessageRepository,
    summaries: SqlSummaryRepository,
) -> None:
    contact_id = await _contact(contacts, modes, 1)
    message_id = await _say(messages, contact_id, 1, NOW)
    cursor = SummaryCursor(created_at=NOW, message_id=message_id)
    await summaries.mark_summary_failed(contact_id, NOW)

    assert await summaries.store_summary(contact_id, _stored(cursor), cursor) is False
    contact = await contacts.get_by_id(contact_id)
    assert contact is not None
    assert contact.summary_failed_at == NOW
    assert contact.summary == {}


async def test_clear_removes_the_summary_and_the_marker(
    contacts: SqlContactRepository,
    modes: SqlModeRepository,
    messages: SqlMessageRepository,
    summaries: SqlSummaryRepository,
) -> None:
    contact_id = await _contact(contacts, modes, 1)
    message_id = await _say(messages, contact_id, 1, NOW)
    await summaries.store_summary(
        contact_id, _stored(SummaryCursor(created_at=NOW, message_id=message_id)), None
    )
    await summaries.mark_summary_failed(contact_id, NOW)

    assert await summaries.clear_summary(contact_id) is True
    assert await summaries.clear_summary(contact_id) is False
    contact = await contacts.get_by_id(contact_id)
    assert contact is not None
    assert contact.summary == {}
    assert contact.summary_failed_at is None


async def test_writes_to_a_missing_contact_raise_contact_not_found(
    summaries: SqlSummaryRepository,
) -> None:
    cursor = SummaryCursor(created_at=NOW, message_id=1)
    with pytest.raises(ContactNotFound):
        await summaries.store_summary(424242, _stored(cursor), None)
    with pytest.raises(ContactNotFound):
        await summaries.mark_summary_failed(424242, NOW)
    with pytest.raises(ContactNotFound):
        await summaries.clear_summary(424242)


async def test_a_naive_failure_time_is_refused(
    contacts: SqlContactRepository, modes: SqlModeRepository, summaries: SqlSummaryRepository
) -> None:
    contact_id = await _contact(contacts, modes, 1)
    with pytest.raises(ValueError, match="timezone-aware"):
        await summaries.mark_summary_failed(contact_id, NOW.replace(tzinfo=None))


async def _messages(
    messages: SqlMessageRepository, contact_id: int, count: int, start: int
) -> list[int]:
    ids: list[int] = []
    for offset in range(count):
        direction = MessageDirection.INCOMING if offset % 2 else MessageDirection.OUTGOING
        ids.append(
            await _say(
                messages,
                contact_id,
                start + offset,
                NOW - timedelta(minutes=60 - offset),
                direction,
            )
        )
    return ids


async def test_due_contacts_by_status_threshold_both_directions_and_ordering(
    contacts: SqlContactRepository,
    modes: SqlModeRepository,
    messages: SqlMessageRepository,
    summaries: SqlSummaryRepository,
) -> None:
    active = await _contact(contacts, modes, 1)
    paused = await _contact(contacts, modes, 2, ContactStatus.PAUSED)
    below = await _contact(contacts, modes, 3)
    statuses = (ContactStatus.NEW, ContactStatus.PENDING, ContactStatus.IGNORED)
    excluded = [
        await _contact(contacts, modes, 10 + n, status) for n, status in enumerate(statuses)
    ]
    active_ids = await _messages(messages, active, 5, 100)
    paused_ids = await _messages(messages, paused, 7, 200)
    await _messages(messages, below, 3, 300)
    for number, contact_id in enumerate(excluded):
        await _messages(messages, contact_id, 9, 400 + 20 * number)

    due = await summaries.contacts_due_for_summary(every=4, cooldown=COOLDOWN, now=NOW, limit=10)

    assert [(d.contact_id, d.waiting) for d in due] == [(paused, 7), (active, 5)]
    assert [d.up_to_message_id for d in due] == [max(paused_ids), max(active_ids)]


async def test_due_counts_only_messages_after_the_cursor(
    contacts: SqlContactRepository,
    modes: SqlModeRepository,
    messages: SqlMessageRepository,
    summaries: SqlSummaryRepository,
) -> None:
    contact_id = await _contact(contacts, modes, 1)
    ids = await _messages(messages, contact_id, 6, 100)
    stored = await summaries.messages_after(contact_id, None, 10)
    cursor = SummaryCursor(created_at=stored[2].created_at, message_id=ids[2])
    await summaries.store_summary(contact_id, _stored(cursor), None)

    assert (
        await summaries.contacts_due_for_summary(every=4, cooldown=COOLDOWN, now=NOW, limit=10)
        == []
    )
    [due] = await summaries.contacts_due_for_summary(every=3, cooldown=COOLDOWN, now=NOW, limit=10)
    assert (due.contact_id, due.waiting, due.up_to_message_id) == (contact_id, 3, ids[-1])


async def test_due_respects_the_failure_cooldown_and_its_boundary(
    contacts: SqlContactRepository,
    modes: SqlModeRepository,
    messages: SqlMessageRepository,
    summaries: SqlSummaryRepository,
) -> None:
    contact_id = await _contact(contacts, modes, 1)
    await _messages(messages, contact_id, 5, 100)

    await summaries.mark_summary_failed(contact_id, NOW - COOLDOWN + timedelta(seconds=1))
    assert (
        await summaries.contacts_due_for_summary(every=4, cooldown=COOLDOWN, now=NOW, limit=10)
        == []
    )

    await summaries.mark_summary_failed(contact_id, NOW - COOLDOWN)
    due = await summaries.contacts_due_for_summary(every=4, cooldown=COOLDOWN, now=NOW, limit=10)
    assert [d.contact_id for d in due] == [contact_id]


async def test_due_honours_the_limit_and_ties_by_contact_id(
    contacts: SqlContactRepository,
    modes: SqlModeRepository,
    messages: SqlMessageRepository,
    summaries: SqlSummaryRepository,
) -> None:
    ids = [await _contact(contacts, modes, n) for n in range(1, 4)]
    for number, contact_id in enumerate(ids):
        await _messages(messages, contact_id, 4, 100 + 10 * number)

    due = await summaries.contacts_due_for_summary(every=4, cooldown=COOLDOWN, now=NOW, limit=2)

    assert [d.contact_id for d in due] == sorted(ids)[:2]
    assert (
        await summaries.contacts_due_for_summary(every=4, cooldown=COOLDOWN, now=NOW, limit=0) == []
    )


async def test_due_with_no_eligible_contact_is_empty(summaries: SqlSummaryRepository) -> None:
    assert (
        await summaries.contacts_due_for_summary(every=4, cooldown=COOLDOWN, now=NOW, limit=10)
        == []
    )


async def test_an_unreadable_summary_makes_every_message_count(
    contacts: SqlContactRepository,
    modes: SqlModeRepository,
    messages: SqlMessageRepository,
    summaries: SqlSummaryRepository,
) -> None:
    contact_id = await _contact(contacts, modes, 1)
    await _messages(messages, contact_id, 4, 100)
    await contacts.update(contact_id, ContactUpdate(summary={"version": 99, "cursor": "bad"}))

    [due] = await summaries.contacts_due_for_summary(every=4, cooldown=COOLDOWN, now=NOW, limit=10)
    assert due.waiting == 4
