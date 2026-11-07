from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.engine import URL

from cyrano.db.repositories import (
    SqlAutopilotStateRepository,
    SqlContactRepository,
    SqlMessageRepository,
    SqlModeRepository,
    SqlOutboxRepository,
    SqlReplyBacklogRepository,
)
from cyrano.db.session import SessionFactory
from cyrano.domain.enums import (
    AutopilotPauseReason,
    AutopilotStopReason,
    ContactStatus,
    MessageDirection,
    OutboxSource,
    OutboxState,
)
from cyrano.domain.errors import AutopilotStateMissing, ReplyAfterRefused
from cyrano.domain.models import DEFAULT_MODE_NAME, Contact, ContactCreate, MessageCreate
from tests.db.conftest import execute
from tests.privacy import FAKE_TELEGRAM_ID

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
UNAVAILABLE = AutopilotPauseReason.PROVIDER_UNAVAILABLE


@pytest.fixture
def state(session_factory: SessionFactory) -> SqlAutopilotStateRepository:
    return SqlAutopilotStateRepository(session_factory)


@pytest.fixture
def backlog(session_factory: SessionFactory) -> SqlReplyBacklogRepository:
    return SqlReplyBacklogRepository(session_factory)


async def _active(
    contacts: SqlContactRepository, modes: SqlModeRepository, tg_id: int = FAKE_TELEGRAM_ID
) -> Contact:
    contact = await contacts.create(ContactCreate(tg_id=tg_id))
    await contacts.set_status(contact.id, ContactStatus.PENDING)
    default = await modes.get_by_name(DEFAULT_MODE_NAME)
    assert default is not None
    return await contacts.set_status(contact.id, ContactStatus.ACTIVE, mode_id=default.id)


async def test_the_seeded_state_is_running(state: SqlAutopilotStateRepository) -> None:
    current = await state.get()

    assert (current.stopped, current.stop_reason, current.paused_until, current.pause_reason) == (
        False,
        None,
        None,
        None,
    )
    assert current.is_running(NOW)


async def test_stop_reports_only_the_first_stop_and_keeps_its_reason(
    state: SqlAutopilotStateRepository,
) -> None:
    assert await state.stop(AutopilotStopReason.PROVIDER_AUTH) is True
    assert await state.stop(AutopilotStopReason.PERSONA_MISSING) is False

    current = await state.get()
    assert (current.stopped, current.stop_reason) == (True, AutopilotStopReason.PROVIDER_AUTH)
    assert not current.is_running(NOW)


async def test_two_simultaneous_stops_report_one_new_stop(
    state: SqlAutopilotStateRepository,
) -> None:
    results = await asyncio.gather(
        state.stop(AutopilotStopReason.PROVIDER_AUTH),
        state.stop(AutopilotStopReason.PROVIDER_REQUEST),
    )

    assert sorted(results) == [False, True]


async def test_a_pause_only_extends_and_reports_whether_it_is_new(
    state: SqlAutopilotStateRepository,
) -> None:
    later = NOW + timedelta(minutes=15)

    assert await state.pause_until(later, UNAVAILABLE, now=NOW) is True
    assert await state.pause_until(NOW + timedelta(minutes=5), UNAVAILABLE, now=NOW) is False
    assert (await state.get()).paused_until == later
    assert await state.pause_until(later + timedelta(minutes=1), UNAVAILABLE, now=NOW) is False
    assert (await state.get()).paused_until == later + timedelta(minutes=1)
    ended = later + timedelta(minutes=1)
    assert await state.pause_until(ended + timedelta(hours=1), UNAVAILABLE, now=ended) is True


async def test_a_pause_ends_by_itself(state: SqlAutopilotStateRepository) -> None:
    await state.pause_until(NOW + timedelta(seconds=10), UNAVAILABLE, now=NOW)
    current = await state.get()

    assert current.is_paused(NOW)
    assert not current.is_paused(NOW + timedelta(seconds=10))


async def test_resume_clears_both_and_returns_what_it_cleared(
    state: SqlAutopilotStateRepository,
) -> None:
    await state.stop(AutopilotStopReason.PROVIDER_REQUEST)
    await state.pause_until(NOW + timedelta(minutes=15), UNAVAILABLE, now=NOW)

    before = await state.resume()

    assert before.stopped
    assert before.pause_reason is UNAVAILABLE
    assert (await state.get()).is_running(NOW)
    assert (await state.get()).stop_reason is None


async def test_a_missing_row_raises(state: SqlAutopilotStateRepository, migrated_url: URL) -> None:
    await execute(migrated_url, "DELETE FROM autopilot_state")

    with pytest.raises(AutopilotStateMissing):
        await state.get()
    with pytest.raises(AutopilotStateMissing):
        await state.stop(AutopilotStopReason.PROVIDER_AUTH)
    with pytest.raises(AutopilotStateMissing):
        await state.pause_until(NOW, UNAVAILABLE, now=NOW)
    with pytest.raises(AutopilotStateMissing):
        await state.resume()


async def test_a_naive_pause_time_is_refused(state: SqlAutopilotStateRepository) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        await state.pause_until(datetime(2026, 1, 1), UNAVAILABLE, now=NOW)


@pytest.mark.parametrize(
    "status",
    [ContactStatus.NEW, ContactStatus.PENDING, ContactStatus.PAUSED, ContactStatus.IGNORED],
)
async def test_reply_after_is_refused_without_the_active_status(
    contacts: SqlContactRepository, modes: SqlModeRepository, status: ContactStatus
) -> None:
    contact = await _active(contacts, modes)

    with pytest.raises(ReplyAfterRefused):
        await contacts.set_status(contact.id, status, reply_after=NOW)

    current = await contacts.get_by_id(contact.id)
    assert current is not None
    assert (current.status, current.reply_after, current.updated_at) == (
        ContactStatus.ACTIVE,
        None,
        contact.updated_at,
    )


async def test_reply_after_is_stored_on_the_resume_and_kept_on_a_same_status_call(
    contacts: SqlContactRepository, modes: SqlModeRepository
) -> None:
    contact = await _active(contacts, modes)
    assert contact.reply_after is None
    await contacts.set_status(contact.id, ContactStatus.PAUSED)

    resumed = await contacts.set_status(contact.id, ContactStatus.ACTIVE, reply_after=NOW)
    again = await contacts.set_status(
        contact.id, ContactStatus.ACTIVE, reply_after=NOW + timedelta(hours=1)
    )

    assert (resumed.changed, resumed.reply_after) == (True, NOW)
    assert (again.changed, again.reply_after) == (False, NOW)


async def test_a_naive_reply_after_is_refused(
    contacts: SqlContactRepository, modes: SqlModeRepository
) -> None:
    contact = await _active(contacts, modes)
    await contacts.set_status(contact.id, ContactStatus.PAUSED)

    with pytest.raises(ValueError, match="timezone-aware"):
        await contacts.set_status(
            contact.id,
            ContactStatus.ACTIVE,
            reply_after=datetime(2026, 1, 1),
        )


async def test_a_message_is_read_by_its_internal_id(
    contacts: SqlContactRepository, messages: SqlMessageRepository, modes: SqlModeRepository
) -> None:
    contact = await _active(contacts, modes)
    stored = await messages.append(
        MessageCreate(
            contact_id=contact.id, direction=MessageDirection.INCOMING, text="hi", tg_message_id=5
        )
    )

    assert await messages.get(stored.id) == stored
    assert await messages.get(stored.id + 1000) is None


async def test_the_outbox_knows_a_row_for_a_trigger_and_counts_by_source(
    contacts: SqlContactRepository, modes: SqlModeRepository, outbox: SqlOutboxRepository
) -> None:
    contact = await _active(contacts, modes)
    await outbox.enqueue(contact.id, "invented", NOW, OutboxSource.AUTOPILOT, 31)
    await outbox.enqueue(contact.id, "invented test", NOW, OutboxSource.MANUAL_TEST)

    assert await outbox.has_row_for_trigger(contact.id, 31) is True
    assert await outbox.has_row_for_trigger(contact.id, 32) is False
    assert await outbox.has_row_for_trigger(contact.id + 1, 31) is False
    autopilot = await outbox.count_by_state(OutboxSource.AUTOPILOT)
    everything = await outbox.count_by_state()
    assert autopilot[OutboxState.SCHEDULED] == 1
    assert everything[OutboxState.SCHEDULED] == 2


async def _say(
    messages: SqlMessageRepository,
    contact: Contact,
    direction: MessageDirection,
    number: int,
    at: datetime,
) -> None:
    await messages.append(
        MessageCreate(
            contact_id=contact.id,
            direction=direction,
            text="invented",
            tg_message_id=number,
            created_at=at,
        )
    )


async def test_the_unanswered_count_covers_active_contacts_whose_last_message_is_incoming(
    contacts: SqlContactRepository,
    messages: SqlMessageRepository,
    modes: SqlModeRepository,
    backlog: SqlReplyBacklogRepository,
) -> None:
    waiting = await _active(contacts, modes, FAKE_TELEGRAM_ID)
    await _say(messages, waiting, MessageDirection.INCOMING, 1, NOW - timedelta(days=3))
    answered = await _active(contacts, modes, FAKE_TELEGRAM_ID + 1)
    await _say(messages, answered, MessageDirection.INCOMING, 2, NOW - timedelta(minutes=2))
    await _say(messages, answered, MessageDirection.OUTGOING, 3, NOW - timedelta(minutes=1))
    pending = await contacts.create(ContactCreate(tg_id=FAKE_TELEGRAM_ID + 2))
    await _say(messages, pending, MessageDirection.INCOMING, 4, NOW)
    resumed = await _active(contacts, modes, FAKE_TELEGRAM_ID + 3)
    await _say(messages, resumed, MessageDirection.INCOMING, 5, NOW - timedelta(minutes=5))
    await contacts.set_status(resumed.id, ContactStatus.PAUSED)
    await contacts.set_status(resumed.id, ContactStatus.ACTIVE, reply_after=NOW)

    assert await backlog.count_unanswered_active() == 1


async def test_awaiting_reply_returns_the_latest_message_once_per_contact_oldest_first(
    contacts: SqlContactRepository,
    messages: SqlMessageRepository,
    modes: SqlModeRepository,
    backlog: SqlReplyBacklogRepository,
) -> None:
    first = await _active(contacts, modes, FAKE_TELEGRAM_ID)
    await _say(messages, first, MessageDirection.INCOMING, 1, NOW - timedelta(minutes=9))
    await _say(messages, first, MessageDirection.INCOMING, 2, NOW - timedelta(minutes=3))
    second = await _active(contacts, modes, FAKE_TELEGRAM_ID + 1)
    await _say(messages, second, MessageDirection.INCOMING, 3, NOW - timedelta(minutes=5))

    found = await backlog.awaiting_reply(newer_than=NOW - timedelta(hours=6), older_than=NOW)

    assert [(m.contact_id, m.tg_message_id) for m in found] == [(second.id, 3), (first.id, 2)]
