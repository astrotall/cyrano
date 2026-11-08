from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from cyrano.db.repositories import SqlContactRepository, SqlModeRepository
from cyrano.domain.enums import ContactStatus
from cyrano.domain.errors import ContactNotFound
from cyrano.services.repositories import ContactRepository
from tests.privacy import FAKE_TELEGRAM_ID
from tests.services.outbox_fakes import stored_contact

MOMENT = datetime(2026, 11, 4, 23, 45, tzinfo=UTC)


def test_the_repository_still_satisfies_its_protocol(contacts: SqlContactRepository) -> None:
    repository: ContactRepository = contacts

    assert repository is not None


async def test_an_empty_reply_after_is_set(
    contacts: SqlContactRepository, modes: SqlModeRepository
) -> None:
    contact = await stored_contact(contacts, modes, ContactStatus.ACTIVE, tg_id=FAKE_TELEGRAM_ID)

    moved = await contacts.set_reply_after(contact.id, MOMENT)

    assert moved.reply_after == MOMENT
    stored = await contacts.get_by_id(contact.id)
    assert stored is not None and stored.reply_after == MOMENT


async def test_reply_after_moves_forward_but_never_backwards(
    contacts: SqlContactRepository, modes: SqlModeRepository
) -> None:
    contact = await stored_contact(contacts, modes, ContactStatus.ACTIVE, tg_id=FAKE_TELEGRAM_ID)
    await contacts.set_reply_after(contact.id, MOMENT)

    earlier = await contacts.set_reply_after(contact.id, MOMENT - timedelta(seconds=1))
    same = await contacts.set_reply_after(contact.id, MOMENT)
    later = await contacts.set_reply_after(contact.id, MOMENT + timedelta(minutes=5))

    assert earlier.reply_after == MOMENT
    assert same.reply_after == MOMENT
    assert later.reply_after == MOMENT + timedelta(minutes=5)


@pytest.mark.parametrize("status", [ContactStatus.ACTIVE, ContactStatus.PAUSED])
async def test_the_status_and_the_pause_reason_are_untouched(
    contacts: SqlContactRepository, modes: SqlModeRepository, status: ContactStatus
) -> None:
    contact = await stored_contact(contacts, modes, status, tg_id=FAKE_TELEGRAM_ID)

    moved = await contacts.set_reply_after(contact.id, MOMENT)

    assert moved.status is status
    assert moved.pause_reason == contact.pause_reason
    assert moved.mode_id == contact.mode_id


async def test_an_unknown_contact_and_a_naive_time_are_refused(
    contacts: SqlContactRepository, modes: SqlModeRepository
) -> None:
    contact = await stored_contact(contacts, modes, ContactStatus.ACTIVE, tg_id=FAKE_TELEGRAM_ID)

    with pytest.raises(ContactNotFound):
        await contacts.set_reply_after(909090909090, MOMENT)
    with pytest.raises(ValueError, match="aware"):
        await contacts.set_reply_after(contact.id, datetime(2026, 11, 4, 23, 45))
