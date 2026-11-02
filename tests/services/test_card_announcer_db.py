from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from cyrano.db.repositories import SqlContactRepository, SqlMessageRepository, SqlModeRepository
from cyrano.domain.enums import ContactStatus, MessageDirection
from cyrano.domain.models import (
    ContactCreate,
    ContactProfile,
    ContactReadyEvent,
    ContactUpdate,
    MessageCreate,
    ModeCreate,
)
from cyrano.services.card_announcer import CARD_WITHOUT_PROFILE_DELAY, CardAnnouncer
from cyrano.services.contact_events import InMemoryContactEvents
from tests.services.fakes import FakeCardSender

WAIT_ATTEMPTS = 300
WAIT_INTERVAL_SECONDS = 0.01


async def _wait_for_status(
    contacts: SqlContactRepository, contact_id: int, status: ContactStatus
) -> None:
    for _ in range(WAIT_ATTEMPTS):
        contact = await contacts.get_by_id(contact_id)
        if contact is not None and contact.status is status:
            return
        await asyncio.sleep(WAIT_INTERVAL_SECONDS)
    pytest.fail("the contact never reached the status")


async def test_a_ready_event_on_real_storage_sends_the_card_and_sets_pending(
    contacts: SqlContactRepository, messages: SqlMessageRepository, modes: SqlModeRepository
) -> None:
    await modes.create(ModeCreate(name="flirt", prompt_fragment="fake"))
    await modes.create(ModeCreate(name="retired", prompt_fragment="fake", enabled=False))
    contact = await contacts.create(ContactCreate(tg_id=1, username="fake_user"))
    profile = ContactProfile(bio="fake bio", collected_at=datetime.now(UTC))
    await contacts.update(contact.id, ContactUpdate(profile=profile.model_dump(mode="json")))
    for number in (1, 2):
        await messages.append(
            MessageCreate(
                contact_id=contact.id,
                direction=MessageDirection.INCOMING,
                text=f"fake text {number}",
                tg_message_id=number,
            )
        )
    sender = FakeCardSender()
    ready = InMemoryContactEvents[ContactReadyEvent]()
    announcer = CardAnnouncer(contacts, messages, modes, sender, ready)

    task = asyncio.create_task(announcer.run())
    try:
        await ready.publish(ContactReadyEvent(contact_id=contact.id))
        await _wait_for_status(contacts, contact.id, ContactStatus.PENDING)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    [card] = sender.cards
    assert card.messages == ["fake text 1", "fake text 2"]
    assert [m.name for m in card.modes] == ["default", "flirt"]
    assert (card.username, card.bio, card.profile_collected) == ("fake_user", "fake bio", True)


async def test_the_age_rule_uses_the_stored_creation_time(
    contacts: SqlContactRepository, messages: SqlMessageRepository, modes: SqlModeRepository
) -> None:
    contact = await contacts.create(ContactCreate(tg_id=1))
    sender = FakeCardSender()
    ready = InMemoryContactEvents[ContactReadyEvent]()
    later = datetime.now(UTC) + timedelta(seconds=CARD_WITHOUT_PROFILE_DELAY + 60)

    young = CardAnnouncer(contacts, messages, modes, sender, ready)
    task = asyncio.create_task(young.run())
    await asyncio.sleep(WAIT_INTERVAL_SECONDS * 10)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert sender.cards == []

    aged = CardAnnouncer(contacts, messages, modes, sender, ready, clock=lambda: later)
    task = asyncio.create_task(aged.run())
    try:
        await _wait_for_status(contacts, contact.id, ContactStatus.PENDING)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert [c.contact_id for c in sender.cards] == [contact.id]
