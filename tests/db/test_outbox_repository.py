from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError

from cyrano.db.repositories import (
    SqlContactRepository,
    SqlMessageRepository,
    SqlModeRepository,
    SqlOutboxRepository,
)
from cyrano.db.session import SessionFactory
from cyrano.domain.enums import ContactStatus, MessageDirection, OutboxSource, OutboxState
from cyrano.domain.errors import (
    ContactNotFound,
    OutboxRowInvalid,
    OutboxRowNotFound,
    OutboxStateConflict,
)
from cyrano.domain.models import Contact
from cyrano.domain.outbox import ScheduledMessage
from tests.db.conftest import execute, fetch_all
from tests.privacy import FAKE_INTERNAL_ID, FAKE_TELEGRAM_ID
from tests.services.outbox_fakes import FAKE_NOW, stored_contact

TEXT = "fake outbox text"
TRIGGER = 41
EARLIER = FAKE_NOW - timedelta(minutes=5)
LATER = FAKE_NOW + timedelta(minutes=5)
TG_MESSAGE_ID = 700001
SENT_AT = FAKE_NOW + timedelta(seconds=3)
CLAIMERS = 6


@pytest.fixture
async def contact(contacts: SqlContactRepository, modes: SqlModeRepository) -> Contact:
    return await stored_contact(contacts, modes, ContactStatus.ACTIVE, tg_id=FAKE_TELEGRAM_ID)


async def _enqueue(
    outbox: SqlOutboxRepository,
    contact: Contact,
    send_at: datetime = EARLIER,
    trigger: int | None = None,
) -> int:
    row = await outbox.enqueue(contact.id, TEXT, send_at, OutboxSource.MANUAL_TEST, trigger)
    return row.id


async def test_enqueue_stores_a_scheduled_row(
    outbox: SqlOutboxRepository, contact: Contact
) -> None:
    row = await outbox.enqueue(contact.id, TEXT, LATER, OutboxSource.AUTOPILOT, TRIGGER)

    assert row.contact_id == contact.id
    assert row.trigger_message_id == TRIGGER
    assert row.text == TEXT
    assert row.source is OutboxSource.AUTOPILOT
    assert row.send_at == LATER
    assert row.due_at == LATER
    assert row.state is OutboxState.SCHEDULED
    assert (row.error_class, row.cancel_reason, row.claimed_at, row.sent_tg_message_id) == (
        None,
        None,
        None,
        None,
    )


async def test_enqueue_is_idempotent_on_the_trigger(
    outbox: SqlOutboxRepository, contact: Contact
) -> None:
    first = await outbox.enqueue(contact.id, TEXT, LATER, OutboxSource.AUTOPILOT, TRIGGER)
    second = await outbox.enqueue(
        contact.id, "another text", EARLIER, OutboxSource.AUTOPILOT, TRIGGER
    )

    assert second == first
    assert len(await outbox.list_rows(None, 10)) == 1


async def test_concurrent_enqueues_on_one_trigger_create_one_row(
    outbox: SqlOutboxRepository, contact: Contact
) -> None:
    rows = await asyncio.gather(
        *(
            outbox.enqueue(contact.id, TEXT, LATER, OutboxSource.AUTOPILOT, TRIGGER)
            for _ in range(CLAIMERS)
        )
    )

    assert len({row.id for row in rows}) == 1
    assert len(await outbox.list_rows(None, 10)) == 1


async def test_rows_without_a_trigger_are_never_duplicates(
    outbox: SqlOutboxRepository, contact: Contact
) -> None:
    await _enqueue(outbox, contact)
    await _enqueue(outbox, contact)

    assert len(await outbox.list_rows(None, 10)) == 2


async def test_the_same_trigger_on_another_contact_is_a_new_row(
    outbox: SqlOutboxRepository,
    contacts: SqlContactRepository,
    modes: SqlModeRepository,
    contact: Contact,
) -> None:
    other = await stored_contact(contacts, modes, ContactStatus.ACTIVE, tg_id=FAKE_TELEGRAM_ID + 1)

    first = await _enqueue(outbox, contact, trigger=TRIGGER)
    second = await _enqueue(outbox, other, trigger=TRIGGER)

    assert first != second


async def test_the_unique_index_refuses_a_second_row_with_the_same_trigger(
    migrated_url: URL, contact: Contact
) -> None:
    insert = (
        "INSERT INTO scheduled_messages "  # noqa: S608
        "(contact_id, trigger_message_id, text, source, send_at, due_at) "
        f"VALUES ({contact.id}, {TRIGGER}, 'x', 'autopilot', now(), now())"
    )
    await execute(migrated_url, insert)

    with pytest.raises(DBAPIError, match="uq_scheduled_messages"):
        await execute(migrated_url, insert)


@pytest.mark.parametrize(
    ("column", "value", "constraint"),
    [
        ("text", "''", "ck_scheduled_messages_text_not_empty"),
        ("state", "'unknown'", "ck_scheduled_messages_state_allowed"),
        ("source", "'unknown'", "ck_scheduled_messages_source_allowed"),
    ],
)
async def test_check_constraints_refuse_bad_values(
    migrated_url: URL, contact: Contact, column: str, value: str, constraint: str
) -> None:
    values = {"text": "'x'", "state": "'scheduled'", "source": "'manual_test'", column: value}
    statement = (
        "INSERT INTO scheduled_messages (contact_id, text, state, source, send_at, due_at) "  # noqa: S608
        f"VALUES ({contact.id}, {values['text']}, {values['state']}, {values['source']}, "
        "now(), now())"
    )

    with pytest.raises(DBAPIError, match=constraint):
        await execute(migrated_url, statement)


async def test_enqueue_for_an_unknown_contact_is_a_domain_error(
    outbox: SqlOutboxRepository,
) -> None:
    with pytest.raises(ContactNotFound):
        await outbox.enqueue(FAKE_INTERNAL_ID, TEXT, LATER, OutboxSource.MANUAL_TEST)


async def test_enqueue_of_an_empty_text_is_a_domain_error_without_values(
    outbox: SqlOutboxRepository, contact: Contact
) -> None:
    with pytest.raises(OutboxRowInvalid) as raised:
        await outbox.enqueue(contact.id, "", LATER, OutboxSource.MANUAL_TEST)

    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__ is True


async def test_a_naive_time_is_refused(outbox: SqlOutboxRepository, contact: Contact) -> None:
    naive = datetime(2025, 1, 1)

    with pytest.raises(ValueError, match="timezone-aware"):
        await outbox.enqueue(contact.id, TEXT, naive, OutboxSource.MANUAL_TEST)
    with pytest.raises(ValueError, match="timezone-aware"):
        await outbox.claim_due(naive)
    with pytest.raises(ValueError, match="timezone-aware"):
        await outbox.stale_sending(naive)


async def test_claim_due_takes_the_earliest_then_the_lowest_id(
    outbox: SqlOutboxRepository, contact: Contact
) -> None:
    later = await _enqueue(outbox, contact, FAKE_NOW)
    tied_first = await _enqueue(outbox, contact, EARLIER)
    tied_second = await _enqueue(outbox, contact, EARLIER)

    claimed = [await outbox.claim_due(FAKE_NOW) for _ in range(3)]

    assert [row.id for row in claimed if row is not None] == [tied_first, tied_second, later]


async def test_claim_due_marks_the_row_sending_and_stores_the_claim_time(
    outbox: SqlOutboxRepository, contact: Contact
) -> None:
    await _enqueue(outbox, contact)

    row = await outbox.claim_due(FAKE_NOW)

    assert row is not None
    assert row.state is OutboxState.SENDING
    assert row.claimed_at == FAKE_NOW


async def test_claim_due_ignores_future_rows(outbox: SqlOutboxRepository, contact: Contact) -> None:
    await _enqueue(outbox, contact, LATER)

    assert await outbox.claim_due(FAKE_NOW) is None


async def test_a_row_due_exactly_now_is_claimed(
    outbox: SqlOutboxRepository, contact: Contact
) -> None:
    await _enqueue(outbox, contact, FAKE_NOW)

    assert await outbox.claim_due(FAKE_NOW) is not None


async def test_claim_due_never_returns_sending_or_finished_rows(
    outbox: SqlOutboxRepository, contact: Contact
) -> None:
    sending = await _enqueue(outbox, contact)
    await outbox.claim_due(FAKE_NOW)
    failed = await _enqueue(outbox, contact)
    await outbox.claim_due(FAKE_NOW)
    await outbox.mark_failed(failed, "FakeError")
    sent = await _enqueue(outbox, contact)
    await outbox.claim_due(FAKE_NOW)
    await outbox.complete_send(sent, contact.id, TEXT, TG_MESSAGE_ID, SENT_AT)
    cancelled = await _enqueue(outbox, contact)
    await outbox.mark_cancelled(cancelled, "fake_reason")

    assert await outbox.claim_due(FAKE_NOW + timedelta(days=1)) is None
    stored = await outbox.get(sending)
    assert stored is not None
    assert stored.state is OutboxState.SENDING


async def test_two_concurrent_claimers_never_get_the_same_row(
    session_factory: SessionFactory, contact: Contact
) -> None:
    repositories = [SqlOutboxRepository(session_factory) for _ in range(CLAIMERS)]
    await _enqueue(repositories[0], contact)

    claims = await asyncio.gather(*(repo.claim_due(FAKE_NOW) for repo in repositories))

    assert len([row for row in claims if row is not None]) == 1


async def test_concurrent_claimers_get_different_rows(
    session_factory: SessionFactory, contact: Contact
) -> None:
    repositories = [SqlOutboxRepository(session_factory) for _ in range(CLAIMERS)]
    expected = {await _enqueue(repositories[0], contact) for _ in range(CLAIMERS)}

    results: list[int] = []
    for _ in range(CLAIMERS):
        claims = await asyncio.gather(*(repo.claim_due(FAKE_NOW) for repo in repositories))
        results.extend(row.id for row in claims if row is not None)

    assert sorted(results) == sorted(expected)


async def test_reschedule_moves_a_claimed_row_back(
    outbox: SqlOutboxRepository, contact: Contact
) -> None:
    await _enqueue(outbox, contact)
    claimed = await outbox.claim_due(FAKE_NOW)
    assert claimed is not None

    row = await outbox.reschedule(claimed.id, LATER, "FakeError")

    assert row.state is OutboxState.SCHEDULED
    assert row.send_at == LATER
    assert row.claimed_at is None
    assert row.error_class == "FakeError"


async def test_reschedule_never_changes_the_due_time(
    outbox: SqlOutboxRepository, contact: Contact
) -> None:
    row_id = await _enqueue(outbox, contact)

    for later in (LATER, LATER + timedelta(hours=1)):
        claimed = await outbox.claim_due(later)
        assert claimed is not None
        assert claimed.due_at == EARLIER
        moved = await outbox.reschedule(row_id, later, "FakeError")
        assert moved.send_at == later
        assert moved.due_at == EARLIER


async def test_a_repeated_enqueue_keeps_the_due_time_of_the_stored_row(
    outbox: SqlOutboxRepository, contact: Contact
) -> None:
    first = await outbox.enqueue(contact.id, TEXT, EARLIER, OutboxSource.AUTOPILOT, TRIGGER)

    repeated = await outbox.enqueue(contact.id, TEXT, LATER, OutboxSource.AUTOPILOT, TRIGGER)

    assert repeated.id == first.id
    assert (repeated.send_at, repeated.due_at) == (EARLIER, EARLIER)


async def test_the_due_time_is_required_by_the_database(
    migrated_url: URL, contact: Contact
) -> None:
    with pytest.raises(DBAPIError, match="due_at"):
        await execute(
            migrated_url,
            "INSERT INTO scheduled_messages (contact_id, text, source, send_at) "  # noqa: S608
            f"VALUES ({contact.id}, 'x', 'manual_test', now())",
        )


async def test_a_row_failed_as_interrupted_can_never_be_rescheduled(
    outbox: SqlOutboxRepository, contact: Contact
) -> None:
    await _enqueue(outbox, contact)
    claimed = await outbox.claim_due(FAKE_NOW)
    assert claimed is not None
    await outbox.mark_failed(claimed.id, "interrupted")

    with pytest.raises(OutboxStateConflict):
        await outbox.reschedule(claimed.id, LATER)

    stored = await outbox.get(claimed.id)
    assert stored is not None
    assert stored.state is OutboxState.FAILED


async def test_complete_send_stores_the_message_and_marks_the_row_sent(
    outbox: SqlOutboxRepository, messages: SqlMessageRepository, contact: Contact
) -> None:
    await _enqueue(outbox, contact)
    claimed = await outbox.claim_due(FAKE_NOW)
    assert claimed is not None

    row = await outbox.complete_send(claimed.id, contact.id, TEXT, TG_MESSAGE_ID, SENT_AT)

    assert row.state is OutboxState.SENT
    assert row.sent_tg_message_id == TG_MESSAGE_ID
    stored = await messages.list_last(contact.id, 10)
    assert [(m.direction, m.text, m.tg_message_id, m.created_at) for m in stored] == [
        (MessageDirection.OUTGOING, TEXT, TG_MESSAGE_ID, SENT_AT)
    ]


async def test_complete_send_does_not_move_last_message_at(
    outbox: SqlOutboxRepository, contacts: SqlContactRepository, contact: Contact
) -> None:
    await contacts.touch_last_message_at(contact.id, EARLIER)
    await _enqueue(outbox, contact)
    claimed = await outbox.claim_due(FAKE_NOW)
    assert claimed is not None

    await outbox.complete_send(claimed.id, contact.id, TEXT, TG_MESSAGE_ID, SENT_AT)

    stored = await contacts.get_by_id(contact.id)
    assert stored is not None
    assert stored.last_message_at == EARLIER


async def test_complete_send_is_idempotent_on_the_message(
    outbox: SqlOutboxRepository, messages: SqlMessageRepository, contact: Contact
) -> None:
    await _enqueue(outbox, contact)
    claimed = await outbox.claim_due(FAKE_NOW)
    assert claimed is not None

    await outbox.complete_send(claimed.id, contact.id, TEXT, TG_MESSAGE_ID, SENT_AT)
    await outbox.complete_send(claimed.id, contact.id, TEXT, TG_MESSAGE_ID, SENT_AT)

    assert len(await messages.list_last(contact.id, 10)) == 1


async def test_complete_send_is_atomic(
    outbox: SqlOutboxRepository,
    messages: SqlMessageRepository,
    migrated_url: URL,
    contact: Contact,
) -> None:
    await _enqueue(outbox, contact)
    claimed = await outbox.claim_due(FAKE_NOW)
    assert claimed is not None
    await execute(
        migrated_url,
        "CREATE FUNCTION refuse_update() RETURNS trigger AS "
        "$$ BEGIN RAISE EXCEPTION 'fake failure inside the transaction'; END; $$ "
        "LANGUAGE plpgsql",
    )
    await execute(
        migrated_url,
        "CREATE TRIGGER refuse_update BEFORE UPDATE ON scheduled_messages "
        "FOR EACH ROW EXECUTE FUNCTION refuse_update()",
    )

    with pytest.raises(DBAPIError):
        await outbox.complete_send(claimed.id, contact.id, TEXT, TG_MESSAGE_ID, SENT_AT)

    stored = await outbox.get(claimed.id)
    assert stored is not None
    assert stored.state is OutboxState.SENDING
    assert stored.sent_tg_message_id is None
    assert await messages.list_last(contact.id, 10) == []


async def test_a_message_is_stored_even_when_the_row_was_already_failed_as_interrupted(
    outbox: SqlOutboxRepository, messages: SqlMessageRepository, contact: Contact
) -> None:
    await _enqueue(outbox, contact)
    claimed = await outbox.claim_due(FAKE_NOW)
    assert claimed is not None
    await outbox.mark_failed(claimed.id, "interrupted")

    row = await outbox.complete_send(claimed.id, contact.id, TEXT, TG_MESSAGE_ID, SENT_AT)

    assert row.state is OutboxState.SENT
    assert len(await messages.list_last(contact.id, 10)) == 1


async def test_unknown_rows_are_domain_errors(outbox: SqlOutboxRepository) -> None:
    with pytest.raises(OutboxRowNotFound):
        await outbox.reschedule(FAKE_INTERNAL_ID, LATER)
    with pytest.raises(OutboxRowNotFound):
        await outbox.mark_failed(FAKE_INTERNAL_ID, "FakeError")
    with pytest.raises(OutboxRowNotFound):
        await outbox.mark_cancelled(FAKE_INTERNAL_ID, "fake_reason")
    with pytest.raises(OutboxRowNotFound):
        await outbox.cancel_scheduled(FAKE_INTERNAL_ID, "fake_reason")
    with pytest.raises(OutboxRowNotFound):
        await outbox.complete_send(FAKE_INTERNAL_ID, 1, TEXT, TG_MESSAGE_ID, SENT_AT)
    assert await outbox.get(FAKE_INTERNAL_ID) is None


async def test_mark_cancelled_stores_the_reason_from_scheduled_and_from_sending(
    outbox: SqlOutboxRepository, contact: Contact
) -> None:
    scheduled = await _enqueue(outbox, contact, LATER)
    sending = await _enqueue(outbox, contact)
    await outbox.claim_due(FAKE_NOW)

    first = await outbox.mark_cancelled(scheduled, "fake_reason")
    second = await outbox.mark_cancelled(sending, "fake_reason")

    assert (first.state, first.cancel_reason) == (OutboxState.CANCELLED, "fake_reason")
    assert (second.state, second.cancel_reason) == (OutboxState.CANCELLED, "fake_reason")


async def test_cancel_scheduled_refuses_a_row_that_is_sending(
    outbox: SqlOutboxRepository, contact: Contact
) -> None:
    await _enqueue(outbox, contact)
    claimed = await outbox.claim_due(FAKE_NOW)
    assert claimed is not None

    with pytest.raises(OutboxStateConflict) as raised:
        await outbox.cancel_scheduled(claimed.id, "fake_reason")

    assert raised.value.state is OutboxState.SENDING


async def test_mark_failed_only_works_on_a_sending_row(
    outbox: SqlOutboxRepository, contact: Contact
) -> None:
    row_id = await _enqueue(outbox, contact, LATER)

    with pytest.raises(OutboxStateConflict):
        await outbox.mark_failed(row_id, "FakeError")


async def test_mark_failed_stores_the_error_class(
    outbox: SqlOutboxRepository, contact: Contact
) -> None:
    await _enqueue(outbox, contact)
    claimed = await outbox.claim_due(FAKE_NOW)
    assert claimed is not None

    row = await outbox.mark_failed(claimed.id, "FakeError")

    assert (row.state, row.error_class) == (OutboxState.FAILED, "FakeError")


async def test_the_unused_attempts_column_stays_in_the_schema_at_zero(
    outbox: SqlOutboxRepository, contact: Contact, migrated_url: URL
) -> None:
    await _enqueue(outbox, contact)
    claimed = await outbox.claim_due(FAKE_NOW)
    assert claimed is not None
    await outbox.mark_failed(claimed.id, "FakeError")
    await outbox.retry_failed(claimed.id, LATER)

    assert await fetch_all(migrated_url, "SELECT attempts FROM scheduled_messages") == [(0,)]


async def test_retry_failed_moves_a_failed_row_back_and_clears_its_marks(
    outbox: SqlOutboxRepository, contact: Contact, migrated_url: URL
) -> None:
    await _enqueue(outbox, contact)
    claimed = await outbox.claim_due(FAKE_NOW)
    assert claimed is not None
    await outbox.mark_failed(claimed.id, "uncertain")
    await execute(migrated_url, "UPDATE scheduled_messages SET cancel_reason = 'fake_reason'")

    row = await outbox.retry_failed(claimed.id, LATER)

    assert row.id == claimed.id
    assert row.state is OutboxState.SCHEDULED
    assert row.send_at == LATER
    assert row.due_at == LATER
    assert (row.error_class, row.cancel_reason, row.claimed_at) == (None, None, None)
    assert row.text == TEXT
    assert await outbox.claim_due(FAKE_NOW) is None
    assert await outbox.claim_due(LATER) is not None


@pytest.mark.parametrize(
    "state",
    [OutboxState.SCHEDULED, OutboxState.SENDING, OutboxState.SENT, OutboxState.CANCELLED],
)
async def test_retry_failed_refuses_every_other_state_and_changes_nothing(
    outbox: SqlOutboxRepository, contact: Contact, state: OutboxState
) -> None:
    row_id = await _enqueue(outbox, contact)
    if state is OutboxState.CANCELLED:
        await outbox.mark_cancelled(row_id, "fake_reason")
    elif state is not OutboxState.SCHEDULED:
        await outbox.claim_due(FAKE_NOW)
    if state is OutboxState.SENT:
        await outbox.complete_send(row_id, contact.id, TEXT, TG_MESSAGE_ID, SENT_AT)
    before = await outbox.get(row_id)

    with pytest.raises(OutboxStateConflict) as raised:
        await outbox.retry_failed(row_id, LATER)

    assert raised.value.state is state
    assert await outbox.get(row_id) == before


async def test_retry_failed_refuses_a_naive_time_and_an_unknown_row(
    outbox: SqlOutboxRepository,
) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        await outbox.retry_failed(FAKE_INTERNAL_ID, datetime(2025, 1, 1))
    with pytest.raises(OutboxRowNotFound):
        await outbox.retry_failed(FAKE_INTERNAL_ID, LATER)


async def test_two_concurrent_retries_of_one_row_schedule_it_once(
    session_factory: SessionFactory, contact: Contact
) -> None:
    repositories = [SqlOutboxRepository(session_factory) for _ in range(CLAIMERS)]
    await _enqueue(repositories[0], contact)
    claimed = await repositories[0].claim_due(FAKE_NOW)
    assert claimed is not None
    await repositories[0].mark_failed(claimed.id, "uncertain")

    results = await asyncio.gather(
        *(repo.retry_failed(claimed.id, LATER) for repo in repositories),
        return_exceptions=True,
    )

    assert len([r for r in results if isinstance(r, ScheduledMessage)]) == 1
    assert len([r for r in results if isinstance(r, OutboxStateConflict)]) == CLAIMERS - 1


async def test_cancel_pending_for_contact_touches_only_its_scheduled_rows(
    outbox: SqlOutboxRepository,
    contacts: SqlContactRepository,
    modes: SqlModeRepository,
    contact: Contact,
) -> None:
    other = await stored_contact(contacts, modes, ContactStatus.ACTIVE, tg_id=FAKE_TELEGRAM_ID + 1)
    sending = await _enqueue(outbox, contact)
    await outbox.claim_due(FAKE_NOW)
    pending_one = await _enqueue(outbox, contact, LATER)
    pending_two = await _enqueue(outbox, contact, LATER)
    foreign = await _enqueue(outbox, other, LATER)

    count = await outbox.cancel_pending_for_contact(contact.id, "fake_reason")

    assert count == 2
    states = {row.id: row.state for row in await outbox.list_rows(None, 10)}
    assert states == {
        sending: OutboxState.SENDING,
        pending_one: OutboxState.CANCELLED,
        pending_two: OutboxState.CANCELLED,
        foreign: OutboxState.SCHEDULED,
    }


async def test_stale_sending_lists_only_old_sending_rows(
    outbox: SqlOutboxRepository, contact: Contact
) -> None:
    await _enqueue(outbox, contact)
    old = await outbox.claim_due(EARLIER)
    await _enqueue(outbox, contact)
    await outbox.claim_due(FAKE_NOW)
    await _enqueue(outbox, contact, LATER)
    assert old is not None

    stale = await outbox.stale_sending(EARLIER)

    assert [row.id for row in stale] == [old.id]


async def test_list_rows_filters_limits_and_starts_with_the_newest(
    outbox: SqlOutboxRepository, contact: Contact
) -> None:
    first = await _enqueue(outbox, contact, LATER)
    second = await _enqueue(outbox, contact, LATER)
    third = await _enqueue(outbox, contact, LATER)
    await outbox.mark_cancelled(second, "fake_reason")

    assert [row.id for row in await outbox.list_rows(None, 2)] == [third, second]
    assert [row.id for row in await outbox.list_rows(OutboxState.SCHEDULED, 10)] == [third, first]


async def test_timestamps_are_utc_whatever_zone_came_in(
    outbox: SqlOutboxRepository, contact: Contact, migrated_url: URL
) -> None:
    moscow = timezone(timedelta(hours=3))
    row = await outbox.enqueue(
        contact.id, TEXT, datetime(2025, 3, 4, 8, 6, 7, tzinfo=moscow), OutboxSource.MANUAL_TEST
    )

    assert row.send_at == FAKE_NOW
    assert row.due_at == FAKE_NOW
    for moment in (row.send_at, row.due_at, row.created_at, row.updated_at):
        assert moment.tzinfo is UTC
    claimed = await outbox.claim_due(FAKE_NOW)
    assert claimed is not None
    assert claimed.claimed_at is not None
    assert claimed.claimed_at.tzinfo is UTC
    columns = await fetch_all(
        migrated_url,
        "SELECT data_type FROM information_schema.columns WHERE table_name = 'scheduled_messages' "
        "AND column_name IN ('send_at', 'claimed_at', 'created_at', 'updated_at')",
    )
    assert {column[0] for column in columns} == {"timestamp with time zone"}


async def test_the_row_repr_does_not_carry_the_text(
    outbox: SqlOutboxRepository, contact: Contact
) -> None:
    row = await outbox.enqueue(contact.id, TEXT, LATER, OutboxSource.MANUAL_TEST)

    assert TEXT not in repr(row)


async def test_has_sending_row_is_false_without_rows(
    outbox: SqlOutboxRepository, contact: Contact
) -> None:
    assert await outbox.has_sending_row(contact.id) is False


async def test_has_sending_row_is_true_only_while_a_row_is_sending(
    outbox: SqlOutboxRepository, contact: Contact
) -> None:
    row_id = await _enqueue(outbox, contact)
    assert await outbox.has_sending_row(contact.id) is False

    await outbox.claim_due(FAKE_NOW)
    assert await outbox.has_sending_row(contact.id) is True

    await outbox.mark_cancelled(row_id, "fake_reason")
    assert await outbox.has_sending_row(contact.id) is False


async def test_has_sending_row_does_not_mix_contacts(
    outbox: SqlOutboxRepository,
    contact: Contact,
    contacts: SqlContactRepository,
    modes: SqlModeRepository,
) -> None:
    other = await stored_contact(contacts, modes, ContactStatus.ACTIVE, tg_id=FAKE_TELEGRAM_ID + 1)
    await _enqueue(outbox, contact)
    await outbox.claim_due(FAKE_NOW)

    assert await outbox.has_sending_row(other.id) is False
    assert await outbox.has_sending_row(contact.id) is True


async def test_cancelling_pending_rows_leaves_a_sending_row_alone(
    outbox: SqlOutboxRepository, contact: Contact
) -> None:
    sending_id = await _enqueue(outbox, contact, EARLIER)
    await outbox.claim_due(FAKE_NOW)
    scheduled_id = await _enqueue(outbox, contact, LATER)

    cancelled = await outbox.cancel_pending_for_contact(contact.id, "manual_message")

    assert cancelled == 1
    sending = await outbox.get(sending_id)
    scheduled = await outbox.get(scheduled_id)
    assert sending is not None
    assert sending.state is OutboxState.SENDING
    assert scheduled is not None
    assert (scheduled.state, scheduled.cancel_reason) == (OutboxState.CANCELLED, "manual_message")
