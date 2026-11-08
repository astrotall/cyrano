from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from cyrano.db.repositories import (
    SqlContactRepository,
    SqlMessageRepository,
    SqlModeRepository,
    SqlOutboxRepository,
    SqlProactiveRepository,
)
from cyrano.db.session import SessionFactory
from cyrano.domain.enums import ContactStatus, MessageDirection, OutboxSource
from cyrano.domain.errors import ContactNotFound
from cyrano.domain.models import Contact, Message, MessageCreate
from cyrano.domain.outbox import CANCEL_REASON_OWNER, ScheduledMessage
from cyrano.services.repositories import ProactiveRepository
from tests.services.outbox_fakes import stored_contact

NOW = datetime(2026, 11, 4, 15, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)
FIRST_TG_ID = 660011223344
TEXT = "invented-proactive-repo-text-zq"


class _World:
    def __init__(self, session_factory: SessionFactory) -> None:
        self.contacts = SqlContactRepository(session_factory)
        self.messages = SqlMessageRepository(session_factory)
        self.modes = SqlModeRepository(session_factory)
        self.outbox = SqlOutboxRepository(session_factory)
        self.repo = SqlProactiveRepository(session_factory)
        self.counter = 0

    async def contact(self, status: ContactStatus = ContactStatus.ACTIVE) -> Contact:
        self.counter += 1
        return await stored_contact(
            self.contacts, self.modes, status, tg_id=FIRST_TG_ID + self.counter
        )

    async def say(self, contact: Contact, direction: MessageDirection, at: datetime) -> Message:
        self.counter += 1
        return await self.messages.append(
            MessageCreate(
                contact_id=contact.id,
                direction=direction,
                text=TEXT,
                tg_message_id=self.counter,
                created_at=at,
            )
        )

    async def silent(self, contact: Contact, silence: timedelta = 7 * HOUR) -> Message:
        await self.say(contact, MessageDirection.INCOMING, NOW - silence - HOUR)
        return await self.say(contact, MessageDirection.OUTGOING, NOW - silence)

    async def row(
        self,
        contact: Contact,
        trigger: Message,
        due: datetime,
        *,
        source: OutboxSource = OutboxSource.PROACTIVE,
        parts: int = 1,
    ) -> list[ScheduledMessage]:
        return await self.outbox.enqueue_sequence(
            contact.id, [TEXT] * parts, due, source, trigger.id
        )

    async def due(self, *, max_per_silence: int = 2, limit: int = 10) -> list[tuple[int, int]]:
        found = await self.repo.contacts_due_for_proactive(
            silent_since=NOW - 6 * HOUR,
            now=NOW,
            max_per_silence=max_per_silence,
            interval_since=NOW - 24 * HOUR,
            limit=limit,
        )
        return [(item.contact_id, item.last_message_id) for item in found]


@pytest.fixture
def world(session_factory: SessionFactory) -> _World:
    return _World(session_factory)


def test_the_repository_satisfies_its_protocol(session_factory: SessionFactory) -> None:
    repo: ProactiveRepository = SqlProactiveRepository(session_factory)

    assert repo is not None


async def test_the_tail_is_the_last_message_and_the_last_incoming_by_time_then_id(
    world: _World,
) -> None:
    contact = await world.contact()
    await world.say(contact, MessageDirection.INCOMING, NOW - 3 * HOUR)
    incoming = await world.say(contact, MessageDirection.INCOMING, NOW - 2 * HOUR)
    await world.say(contact, MessageDirection.OUTGOING, NOW - HOUR)
    last = await world.say(contact, MessageDirection.OUTGOING, NOW - HOUR)
    await world.say(contact, MessageDirection.INCOMING, NOW - 9 * HOUR)

    tail = await world.repo.dialogue_tail(contact.id)

    assert tail.last is not None and tail.last.id == last.id
    assert tail.last_incoming is not None and tail.last_incoming.id == incoming.id


async def test_an_empty_history_has_an_empty_tail(world: _World) -> None:
    contact = await world.contact()

    tail = await world.repo.dialogue_tail(contact.id)

    assert tail.last is None
    assert tail.last_incoming is None


async def test_the_silence_count_takes_first_parts_after_the_message_and_skips_cancelled(
    world: _World,
) -> None:
    contact = await world.contact()
    other = await world.contact()
    old_ours = await world.say(contact, MessageDirection.OUTGOING, NOW - 30 * HOUR)
    incoming = await world.say(contact, MessageDirection.INCOMING, NOW - 20 * HOUR)
    ours = await world.say(contact, MessageDirection.OUTGOING, NOW - 19 * HOUR)
    proactive = await world.say(contact, MessageDirection.OUTGOING, NOW - 10 * HOUR)
    later = await world.say(contact, MessageDirection.OUTGOING, NOW - 9 * HOUR)
    other_last = await world.say(other, MessageDirection.OUTGOING, NOW - 9 * HOUR)
    await world.row(contact, old_ours, NOW - 29 * HOUR)
    await world.row(contact, ours, NOW - 18 * HOUR, parts=3)
    await world.row(contact, proactive, NOW - 9 * HOUR, source=OutboxSource.AUTOPILOT)
    [cancelled] = await world.row(contact, later, NOW)
    await world.outbox.cancel_scheduled(cancelled.id, CANCEL_REASON_OWNER)
    await world.row(other, other_last, NOW)

    assert await world.repo.count_proactive_after(contact.id, incoming.id) == 1
    assert await world.repo.count_proactive_after(contact.id, old_ours.id) == 1
    assert await world.repo.count_proactive_after(contact.id, later.id) == 0


async def test_the_newest_due_time_skips_cancelled_rows_and_other_sources(
    world: _World,
) -> None:
    contact = await world.contact()
    first = await world.say(contact, MessageDirection.OUTGOING, NOW - 30 * HOUR)
    second = await world.say(contact, MessageDirection.OUTGOING, NOW - 20 * HOUR)
    third = await world.say(contact, MessageDirection.OUTGOING, NOW - 10 * HOUR)
    assert await world.repo.newest_proactive_due_at(contact.id) is None
    await world.row(contact, first, NOW - 29 * HOUR)
    await world.row(contact, second, NOW + HOUR, source=OutboxSource.AUTOPILOT)
    [cancelled] = await world.row(contact, third, NOW + 2 * HOUR)
    await world.outbox.cancel_scheduled(cancelled.id, CANCEL_REASON_OWNER)

    assert await world.repo.newest_proactive_due_at(contact.id) == NOW - 29 * HOUR


async def test_the_daily_count_spans_every_contact_and_its_boundary_is_open(
    world: _World,
) -> None:
    one = await world.contact()
    two = await world.contact()
    a = await world.say(one, MessageDirection.OUTGOING, NOW - 30 * HOUR)
    b = await world.say(one, MessageDirection.OUTGOING, NOW - 20 * HOUR)
    c = await world.say(two, MessageDirection.OUTGOING, NOW - 20 * HOUR)
    d = await world.say(two, MessageDirection.OUTGOING, NOW - 10 * HOUR)
    e = await world.say(two, MessageDirection.OUTGOING, NOW - 9 * HOUR)
    await world.row(one, a, NOW - 24 * HOUR)
    await world.row(one, b, NOW - 23 * HOUR, parts=2)
    await world.row(two, c, NOW + 3 * HOUR)
    [cancelled] = await world.row(two, d, NOW)
    await world.outbox.cancel_scheduled(cancelled.id, CANCEL_REASON_OWNER)
    await world.row(two, e, NOW, source=OutboxSource.AUTOPILOT)

    assert await world.repo.count_proactive_due_since(NOW - 24 * HOUR) == 2


async def test_the_due_scan_finds_a_silent_active_contact_with_its_last_message(
    world: _World,
) -> None:
    contact = await world.contact()
    last = await world.silent(contact)

    assert await world.due() == [(contact.id, last.id)]


async def test_a_silence_of_exactly_the_threshold_is_due(world: _World) -> None:
    contact = await world.contact()
    last = await world.silent(contact, 6 * HOUR)
    short = await world.contact()
    await world.silent(short, 6 * HOUR - timedelta(seconds=1))

    assert await world.due() == [(contact.id, last.id)]


@pytest.mark.parametrize(
    "status",
    [ContactStatus.NEW, ContactStatus.PENDING, ContactStatus.PAUSED, ContactStatus.IGNORED],
)
async def test_the_due_scan_leaves_out_a_contact_that_is_not_active(
    world: _World, status: ContactStatus
) -> None:
    contact = await world.contact(status)
    await world.silent(contact)

    assert await world.due() == []


async def test_the_due_scan_leaves_out_every_contact_that_breaks_one_storage_rule(
    world: _World,
) -> None:
    answered_last = await world.contact()
    await world.say(answered_last, MessageDirection.OUTGOING, NOW - 9 * HOUR)
    await world.say(answered_last, MessageDirection.INCOMING, NOW - 8 * HOUR)
    never_wrote = await world.contact()
    await world.say(never_wrote, MessageDirection.OUTGOING, NOW - 9 * HOUR)
    postponed = await world.contact()
    await world.silent(postponed)
    await world.repo.set_proactive_not_before(postponed.id, NOW + timedelta(seconds=1))
    resumed = await world.contact()
    await world.silent(resumed)
    await world.contacts.set_status(resumed.id, ContactStatus.PAUSED)
    await world.contacts.set_status(resumed.id, ContactStatus.ACTIVE, reply_after=NOW - HOUR)
    full = await world.contact()
    first = await world.say(full, MessageDirection.INCOMING, NOW - 60 * HOUR)
    ours = await world.say(full, MessageDirection.OUTGOING, NOW - 59 * HOUR)
    nudge = await world.say(full, MessageDirection.OUTGOING, NOW - 30 * HOUR)
    await world.row(full, ours, NOW - 50 * HOUR)
    await world.row(full, nudge, NOW - 29 * HOUR)
    recent = await world.contact()
    last = await world.silent(recent)
    await world.row(recent, last, NOW - 24 * HOUR + timedelta(seconds=1))
    assert first.id > 0

    assert await world.due() == []


async def test_a_passed_postponement_and_an_old_resume_do_not_hide_a_contact(
    world: _World,
) -> None:
    contact = await world.contact()
    last = await world.silent(contact)
    await world.repo.set_proactive_not_before(contact.id, NOW)
    await world.contacts.set_status(contact.id, ContactStatus.PAUSED)
    await world.contacts.set_status(contact.id, ContactStatus.ACTIVE, reply_after=NOW - 6 * HOUR)

    assert await world.due() == [(contact.id, last.id)]


async def test_cancelled_rows_and_a_previous_silence_do_not_count_in_the_scan(
    world: _World,
) -> None:
    contact = await world.contact()
    old = await world.say(contact, MessageDirection.OUTGOING, NOW - 90 * HOUR)
    await world.say(contact, MessageDirection.INCOMING, NOW - 80 * HOUR)
    ours = await world.say(contact, MessageDirection.OUTGOING, NOW - 60 * HOUR)
    nudge = await world.say(contact, MessageDirection.OUTGOING, NOW - 30 * HOUR)
    await world.row(contact, old, NOW - 89 * HOUR)
    await world.row(contact, old, NOW - 88 * HOUR)
    [cancelled] = await world.row(contact, ours, NOW - 50 * HOUR)
    await world.outbox.cancel_scheduled(cancelled.id, CANCEL_REASON_OWNER)
    await world.row(contact, nudge, NOW - 29 * HOUR)

    assert await world.due(max_per_silence=2) == [(contact.id, nudge.id)]
    assert await world.due(max_per_silence=1) == []


async def test_the_scan_puts_the_most_recent_silence_first_and_keeps_to_its_limit(
    world: _World,
) -> None:
    found = []
    for hours in (30, 8, 12):
        contact = await world.contact()
        last = await world.silent(contact, hours * HOUR)
        found.append((hours, contact.id, last.id))

    expected = [(contact_id, last_id) for _, contact_id, last_id in sorted(found)]
    assert await world.due() == expected
    assert await world.due(limit=2) == expected[:2]
    assert await world.due(limit=0) == []


async def test_the_postponement_is_stored_on_the_contact(world: _World) -> None:
    contact = await world.contact()

    await world.repo.set_proactive_not_before(contact.id, NOW + 12 * HOUR)

    stored = await world.contacts.get_by_id(contact.id)
    assert stored is not None
    assert stored.proactive_not_before == NOW + 12 * HOUR


async def test_the_postponement_of_an_unknown_contact_is_refused(world: _World) -> None:
    with pytest.raises(ContactNotFound):
        await world.repo.set_proactive_not_before(909090909090, NOW)


async def test_a_naive_time_is_refused(world: _World) -> None:
    contact = await world.contact()

    with pytest.raises(ValueError, match="aware"):
        await world.repo.set_proactive_not_before(contact.id, datetime(2026, 11, 4, 12, 0))
    with pytest.raises(ValueError, match="aware"):
        await world.repo.count_proactive_due_since(datetime(2026, 11, 4, 12, 0))
