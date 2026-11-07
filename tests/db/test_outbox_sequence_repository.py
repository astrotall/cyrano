from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from cyrano.db.repositories import (
    SqlContactRepository,
    SqlMessageRepository,
    SqlModeRepository,
    SqlOutboxRepository,
)
from cyrano.domain.enums import ContactStatus, MessageDirection, OutboxSource, OutboxState
from cyrano.domain.errors import ContactNotFound, OutboxRowInvalid
from cyrano.domain.models import Contact, Message, MessageCreate
from cyrano.domain.outbox import (
    CANCEL_REASON_OWNER,
    CANCEL_REASON_PREDECESSOR,
    CANCEL_REASON_SUPERSEDED,
    ScheduledMessage,
)
from tests.privacy import FAKE_INTERNAL_ID, FAKE_TELEGRAM_ID
from tests.services.outbox_fakes import FAKE_NOW, stored_contact

PARTS = ("invented part one", "invented part two", "invented part three")
TRIGGER = 52
OTHER_TRIGGER = 53
EARLIER = FAKE_NOW - timedelta(minutes=5)
MUCH_EARLIER = FAKE_NOW - timedelta(minutes=10)
LATER = FAKE_NOW + timedelta(minutes=5)
SENT_AT = FAKE_NOW + timedelta(seconds=3)
FIRST_TG_MESSAGE_ID = 702000
FIRST_MESSAGE_NUMBER = 93000
CLAIMERS = 6


@pytest.fixture
async def contact(contacts: SqlContactRepository, modes: SqlModeRepository) -> Contact:
    return await stored_contact(contacts, modes, ContactStatus.ACTIVE, tg_id=FAKE_TELEGRAM_ID)


async def _chain(
    outbox: SqlOutboxRepository,
    contact: Contact,
    texts: tuple[str, ...] = PARTS,
    *,
    send_at: datetime = EARLIER,
    trigger: int | None = TRIGGER,
) -> list[ScheduledMessage]:
    return await outbox.enqueue_sequence(
        contact.id, texts, send_at, OutboxSource.AUTOPILOT, trigger
    )


async def _state(outbox: SqlOutboxRepository, row_id: int) -> tuple[OutboxState, str | None]:
    row = await outbox.get(row_id)
    assert row is not None
    return row.state, row.cancel_reason


async def _send(outbox: SqlOutboxRepository, row: ScheduledMessage, number: int) -> None:
    await outbox.complete_send(
        row.id, row.contact_id, row.text, FIRST_TG_MESSAGE_ID + number, SENT_AT
    )


async def _message(
    messages: SqlMessageRepository, contact: Contact, number: int, at: datetime
) -> Message:
    return await messages.append(
        MessageCreate(
            contact_id=contact.id,
            direction=MessageDirection.INCOMING,
            text="invented incoming text",
            tg_message_id=FIRST_MESSAGE_NUMBER + number,
            created_at=at,
        )
    )


async def test_a_sequence_is_one_chain_with_the_trigger_on_the_first_part_only(
    outbox: SqlOutboxRepository, contact: Contact
) -> None:
    first, second, third = await _chain(outbox, contact)

    assert [row.text for row in (first, second, third)] == list(PARTS)
    assert (first.trigger_message_id, first.follows_id) == (TRIGGER, None)
    assert (second.trigger_message_id, second.follows_id) == (None, first.id)
    assert (third.trigger_message_id, third.follows_id) == (None, second.id)
    for row in (first, second, third):
        assert (row.send_at, row.due_at) == (EARLIER, EARLIER)
        assert row.state is OutboxState.SCHEDULED
        assert row.source is OutboxSource.AUTOPILOT
        assert row.contact_id == contact.id
    assert await outbox.get(second.id) == second


async def test_a_sequence_of_one_text_is_one_plain_row(
    outbox: SqlOutboxRepository, contact: Contact
) -> None:
    [row] = await _chain(outbox, contact, PARTS[:1])

    assert row.follows_id is None
    assert row.trigger_message_id == TRIGGER
    assert len(await outbox.list_rows(None, 10)) == 1


async def test_a_second_sequence_for_the_same_trigger_returns_the_stored_chain(
    outbox: SqlOutboxRepository, contact: Contact
) -> None:
    stored = await _chain(outbox, contact)

    again = await _chain(outbox, contact, ("another invented text",), send_at=LATER)

    assert again == stored
    assert len(await outbox.list_rows(None, 10)) == len(PARTS)


async def test_concurrent_sequences_for_one_trigger_create_one_chain(
    outbox: SqlOutboxRepository, contact: Contact
) -> None:
    chains = await asyncio.gather(*(_chain(outbox, contact) for _ in range(CLAIMERS)))

    assert all(chain == chains[0] for chain in chains)
    assert len(await outbox.list_rows(None, 20)) == len(PARTS)


async def test_sequences_without_a_trigger_are_never_duplicates(
    outbox: SqlOutboxRepository, contact: Contact
) -> None:
    await _chain(outbox, contact, trigger=None)
    await _chain(outbox, contact, trigger=None)

    assert len(await outbox.list_rows(None, 20)) == 2 * len(PARTS)


async def test_a_failure_in_the_middle_of_a_sequence_stores_nothing(
    outbox: SqlOutboxRepository, contact: Contact
) -> None:
    with pytest.raises(OutboxRowInvalid):
        await _chain(outbox, contact, (PARTS[0], PARTS[1], ""))

    assert await outbox.list_rows(None, 10) == []
    assert await outbox.has_row_for_trigger(contact.id, TRIGGER) is False


async def test_an_empty_sequence_is_refused(outbox: SqlOutboxRepository, contact: Contact) -> None:
    with pytest.raises(OutboxRowInvalid):
        await _chain(outbox, contact, ())

    assert await outbox.list_rows(None, 10) == []


async def test_a_sequence_for_an_unknown_contact_is_refused(outbox: SqlOutboxRepository) -> None:
    with pytest.raises(ContactNotFound):
        await outbox.enqueue_sequence(
            FAKE_INTERNAL_ID, PARTS, EARLIER, OutboxSource.AUTOPILOT, TRIGGER
        )


async def test_a_sequence_needs_an_aware_time(
    outbox: SqlOutboxRepository, contact: Contact
) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        await _chain(outbox, contact, send_at=EARLIER.replace(tzinfo=None))


async def test_a_follower_is_claimed_only_after_its_predecessor_was_sent(
    outbox: SqlOutboxRepository, contact: Contact
) -> None:
    first, second, third = await _chain(outbox, contact)

    claimed = await outbox.claim_due(FAKE_NOW)
    assert claimed is not None
    assert claimed.id == first.id
    assert await outbox.claim_due(FAKE_NOW) is None

    await _send(outbox, claimed, 1)
    claimed = await outbox.claim_due(FAKE_NOW)
    assert claimed is not None
    assert claimed.id == second.id
    assert await outbox.claim_due(FAKE_NOW) is None

    await _send(outbox, claimed, 2)
    claimed = await outbox.claim_due(FAKE_NOW)
    assert claimed is not None
    assert claimed.id == third.id


@pytest.mark.parametrize("ended", [OutboxState.FAILED, OutboxState.CANCELLED])
async def test_a_follower_of_a_failed_or_cancelled_part_is_never_claimed(
    outbox: SqlOutboxRepository, contact: Contact, ended: OutboxState
) -> None:
    first, _, _ = await _chain(outbox, contact)
    await outbox.claim_due(FAKE_NOW)
    if ended is OutboxState.FAILED:
        await outbox.mark_failed(first.id, "FakeError")
    else:
        await outbox.mark_cancelled(first.id, CANCEL_REASON_OWNER)

    assert await outbox.claim_due(FAKE_NOW + timedelta(hours=1)) is None


async def test_a_waiting_follower_does_not_block_other_rows_and_the_order_stays(
    outbox: SqlOutboxRepository,
    contacts: SqlContactRepository,
    modes: SqlModeRepository,
    contact: Contact,
) -> None:
    other = await stored_contact(contacts, modes, ContactStatus.ACTIVE, tg_id=FAKE_TELEGRAM_ID + 1)
    first, second, _ = await _chain(outbox, contact, send_at=MUCH_EARLIER)
    lone = await outbox.enqueue(other.id, PARTS[0], EARLIER, OutboxSource.MANUAL_TEST)
    later = await outbox.enqueue(other.id, PARTS[1], FAKE_NOW, OutboxSource.MANUAL_TEST)

    claimed = [await outbox.claim_due(FAKE_NOW) for _ in range(3)]

    assert [row.id if row is not None else None for row in claimed] == [first.id, lone.id, later.id]
    assert await outbox.claim_due(FAKE_NOW) is None
    await _send(outbox, first, 1)
    claimed_next = await outbox.claim_due(FAKE_NOW)
    assert claimed_next is not None
    assert claimed_next.id == second.id


async def test_concurrent_claimers_get_only_the_first_part_of_a_due_chain(
    outbox: SqlOutboxRepository, contact: Contact
) -> None:
    first, _, _ = await _chain(outbox, contact)

    claimed = await asyncio.gather(*(outbox.claim_due(FAKE_NOW) for _ in range(CLAIMERS)))

    assert [row.id for row in claimed if row is not None] == [first.id]


async def test_schedule_followers_moves_only_the_next_scheduled_part(
    outbox: SqlOutboxRepository, contact: Contact
) -> None:
    first, second, third = await _chain(outbox, contact)
    await outbox.claim_due(FAKE_NOW)
    await _send(outbox, first, 1)

    assert await outbox.schedule_followers(first.id, LATER) == 1

    moved = await outbox.get(second.id)
    untouched = await outbox.get(third.id)
    assert moved is not None
    assert untouched is not None
    assert (moved.send_at, moved.due_at, moved.state) == (LATER, EARLIER, OutboxState.SCHEDULED)
    assert (untouched.send_at, untouched.due_at) == (EARLIER, EARLIER)
    assert await outbox.claim_due(FAKE_NOW) is None
    claimed = await outbox.claim_due(LATER)
    assert claimed is not None
    assert claimed.id == second.id


async def test_schedule_followers_leaves_a_cancelled_follower_and_a_lone_row_alone(
    outbox: SqlOutboxRepository, contact: Contact
) -> None:
    first, second, _ = await _chain(outbox, contact)
    await outbox.cancel_scheduled(second.id, CANCEL_REASON_OWNER)
    lone = await outbox.enqueue(contact.id, PARTS[0], EARLIER, OutboxSource.MANUAL_TEST)

    assert await outbox.schedule_followers(first.id, LATER) == 0
    assert await outbox.schedule_followers(lone.id, LATER) == 0
    stored = await outbox.get(second.id)
    assert stored is not None
    assert stored.send_at == EARLIER


@pytest.mark.parametrize("ended", [OutboxState.FAILED, OutboxState.CANCELLED])
async def test_orphaned_followers_are_cancelled_down_the_whole_chain(
    outbox: SqlOutboxRepository, contact: Contact, ended: OutboxState
) -> None:
    first, second, third = await _chain(outbox, contact)
    await outbox.claim_due(FAKE_NOW)
    if ended is OutboxState.FAILED:
        await outbox.mark_failed(first.id, "FakeError")
    else:
        await outbox.mark_cancelled(first.id, CANCEL_REASON_SUPERSEDED)

    assert await outbox.cancel_orphaned_followers() == 2

    assert await _state(outbox, second.id) == (OutboxState.CANCELLED, CANCEL_REASON_PREDECESSOR)
    assert await _state(outbox, third.id) == (OutboxState.CANCELLED, CANCEL_REASON_PREDECESSOR)
    assert await outbox.cancel_orphaned_followers() == 0


async def test_an_orphan_after_a_sent_part_is_cancelled_and_the_sent_part_stays(
    outbox: SqlOutboxRepository, contact: Contact
) -> None:
    first, second, third = await _chain(outbox, contact)
    await outbox.claim_due(FAKE_NOW)
    await _send(outbox, first, 1)
    await outbox.claim_due(FAKE_NOW)
    await outbox.mark_failed(second.id, "FakeError")

    assert await outbox.cancel_orphaned_followers() == 1

    assert await _state(outbox, first.id) == (OutboxState.SENT, None)
    assert await _state(outbox, second.id) == (OutboxState.FAILED, None)
    assert await _state(outbox, third.id) == (OutboxState.CANCELLED, CANCEL_REASON_PREDECESSOR)


async def test_followers_of_a_waiting_or_sent_part_are_not_orphans(
    outbox: SqlOutboxRepository, contact: Contact
) -> None:
    first, second, third = await _chain(outbox, contact)
    assert await outbox.cancel_orphaned_followers() == 0
    await outbox.claim_due(FAKE_NOW)
    assert await outbox.cancel_orphaned_followers() == 0
    await _send(outbox, first, 1)

    assert await outbox.cancel_orphaned_followers() == 0
    for row in (second, third):
        assert await _state(outbox, row.id) == (OutboxState.SCHEDULED, None)


async def test_the_root_trigger_is_found_from_every_part(
    outbox: SqlOutboxRepository, contact: Contact
) -> None:
    chain = await _chain(outbox, contact)
    lone = await outbox.enqueue(contact.id, PARTS[0], EARLIER, OutboxSource.MANUAL_TEST)

    assert [await outbox.root_trigger_id(row.id) for row in chain] == [TRIGGER] * len(PARTS)
    assert await outbox.root_trigger_id(lone.id) is None
    assert await outbox.root_trigger_id(FAKE_INTERNAL_ID) is None


async def test_a_new_reply_never_touches_an_older_reply_whose_first_part_was_sent(
    outbox: SqlOutboxRepository, messages: SqlMessageRepository, contact: Contact
) -> None:
    older = await _message(messages, contact, 1, MUCH_EARLIER)
    newer = await _message(messages, contact, 2, EARLIER)
    first, second, third = await _chain(outbox, contact, trigger=older.id)
    await outbox.claim_due(FAKE_NOW)
    await _send(outbox, first, 1)
    current = await _chain(outbox, contact, trigger=newer.id)

    cancelled = await outbox.cancel_superseded_autopilot_rows(
        contact.id, newer.id, CANCEL_REASON_SUPERSEDED
    )

    assert cancelled == 0
    assert await _state(outbox, first.id) == (OutboxState.SENT, None)
    for row in (second, third, *current):
        assert await _state(outbox, row.id) == (OutboxState.SCHEDULED, None)


async def test_a_new_reply_never_touches_an_older_reply_whose_first_part_is_being_sent(
    outbox: SqlOutboxRepository, messages: SqlMessageRepository, contact: Contact
) -> None:
    older = await _message(messages, contact, 1, MUCH_EARLIER)
    newer = await _message(messages, contact, 2, EARLIER)
    first, second, third = await _chain(outbox, contact, trigger=older.id)
    claimed = await outbox.claim_due(FAKE_NOW)

    cancelled = await outbox.cancel_superseded_autopilot_rows(
        contact.id, newer.id, CANCEL_REASON_SUPERSEDED
    )

    assert claimed is not None
    assert claimed.id == first.id
    assert cancelled == 0
    assert await _state(outbox, first.id) == (OutboxState.SENDING, None)
    for row in (second, third):
        assert await _state(outbox, row.id) == (OutboxState.SCHEDULED, None)


async def test_a_new_reply_cancels_a_whole_waiting_older_chain(
    outbox: SqlOutboxRepository, messages: SqlMessageRepository, contact: Contact
) -> None:
    older = await _message(messages, contact, 1, MUCH_EARLIER)
    newer = await _message(messages, contact, 2, EARLIER)
    chain = await _chain(outbox, contact, trigger=older.id)

    assert (
        await outbox.cancel_superseded_autopilot_rows(
            contact.id, newer.id, CANCEL_REASON_SUPERSEDED
        )
        == 3
    )
    for row in chain:
        assert await _state(outbox, row.id) == (OutboxState.CANCELLED, CANCEL_REASON_SUPERSEDED)


async def test_the_followers_of_a_newer_reply_are_not_cancelled_by_an_older_trigger(
    outbox: SqlOutboxRepository, messages: SqlMessageRepository, contact: Contact
) -> None:
    older = await _message(messages, contact, 1, MUCH_EARLIER)
    newer = await _message(messages, contact, 2, EARLIER)
    chain = await _chain(outbox, contact, trigger=newer.id)

    assert (
        await outbox.cancel_superseded_autopilot_rows(
            contact.id, older.id, CANCEL_REASON_SUPERSEDED
        )
        == 0
    )
    for row in chain:
        assert await _state(outbox, row.id) == (OutboxState.SCHEDULED, None)


async def test_cancel_pending_for_contact_cancels_every_waiting_part(
    outbox: SqlOutboxRepository, contact: Contact
) -> None:
    first, _, _ = await _chain(outbox, contact)
    await outbox.claim_due(FAKE_NOW)
    await _send(outbox, first, 1)

    assert await outbox.cancel_pending_for_contact(contact.id, "manual_message") == 2
    assert await outbox.claim_due(FAKE_NOW + timedelta(hours=1)) is None


async def test_the_other_trigger_starts_its_own_chain(
    outbox: SqlOutboxRepository, contact: Contact
) -> None:
    first = await _chain(outbox, contact)
    second = await _chain(outbox, contact, trigger=OTHER_TRIGGER)

    assert {row.id for row in first}.isdisjoint({row.id for row in second})
    assert second[0].follows_id is None
