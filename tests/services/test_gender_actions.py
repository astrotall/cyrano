from __future__ import annotations

import asyncio

import pytest

from cyrano.db.repositories import SqlContactRepository, SqlMessageRepository, SqlModeRepository
from cyrano.domain.enums import (
    ContactGender,
    ContactStatus,
    GenderOutcome,
    GenderSource,
    MessageDirection,
)
from cyrano.domain.gender import ResolvedGender
from cyrano.domain.models import Contact, ContactCreate, MessageCreate, ModeCreate
from cyrano.services.gender import GenderResolver
from cyrano.services.gender_actions import GenderActions

MISSING_ID = 987654
FEMALE = ContactGender.FEMALE
MALE = ContactGender.MALE


class _Rig:
    def __init__(
        self,
        contacts: SqlContactRepository,
        messages: SqlMessageRepository,
        modes: SqlModeRepository,
        default: ContactGender | None = None,
    ) -> None:
        self.contacts = contacts
        self.messages = messages
        self.modes = modes
        self.actions = GenderActions(contacts, messages, modes, GenderResolver(default))

    async def contact(
        self, status: ContactStatus = ContactStatus.PENDING, name: str | None = "Alex"
    ) -> Contact:
        created = await self.contacts.create(ContactCreate(tg_id=1, display_name=name))
        if status is ContactStatus.NEW:
            return created
        await self.contacts.set_status(created.id, ContactStatus.PENDING)
        if status is ContactStatus.PENDING:
            return await self._current(created.id)
        if status is ContactStatus.IGNORED:
            await self.contacts.set_status(created.id, ContactStatus.IGNORED)
            return await self._current(created.id)
        default = await self.modes.get_by_name("default")
        assert default is not None
        await self.contacts.set_status(created.id, ContactStatus.ACTIVE, mode_id=default.id)
        if status is ContactStatus.PAUSED:
            await self.contacts.set_status(created.id, ContactStatus.PAUSED)
        return await self._current(created.id)

    async def _current(self, contact_id: int) -> Contact:
        contact = await self.contacts.get_by_id(contact_id)
        assert contact is not None
        return contact

    async def stored_gender(self, contact_id: int) -> ContactGender | None:
        return (await self._current(contact_id)).gender


@pytest.fixture
def rig(
    contacts: SqlContactRepository, messages: SqlMessageRepository, modes: SqlModeRepository
) -> _Rig:
    return _Rig(contacts, messages, modes)


async def test_a_choice_is_applied_and_the_result_carries_the_owner_source(rig: _Rig) -> None:
    contact = await rig.contact()

    result = await rig.actions.set_gender(contact.id, FEMALE)

    assert result.outcome is GenderOutcome.APPLIED
    assert result.gender == ResolvedGender(value=FEMALE, source=GenderSource.OWNER)
    assert await rig.stored_gender(contact.id) is FEMALE


async def test_the_same_choice_twice_is_already_applied_and_changes_nothing(rig: _Rig) -> None:
    contact = await rig.contact()
    await rig.actions.set_gender(contact.id, MALE)
    before = await rig._current(contact.id)

    result = await rig.actions.set_gender(contact.id, MALE)

    assert result.outcome is GenderOutcome.ALREADY_APPLIED
    assert result.gender == ResolvedGender(value=MALE, source=GenderSource.OWNER)
    assert await rig._current(contact.id) == before


async def test_another_choice_replaces_the_first(rig: _Rig) -> None:
    contact = await rig.contact()
    await rig.actions.set_gender(contact.id, FEMALE)

    result = await rig.actions.set_gender(contact.id, MALE)

    assert result.outcome is GenderOutcome.APPLIED
    assert result.gender.value is MALE
    assert await rig.stored_gender(contact.id) is MALE


async def test_a_missing_contact_is_not_found_without_a_card(rig: _Rig) -> None:
    result = await rig.actions.set_gender(MISSING_ID, FEMALE)

    assert result.outcome is GenderOutcome.NOT_FOUND
    assert result.card is None
    assert not result.gender.is_known


@pytest.mark.parametrize("status", list(ContactStatus))
async def test_the_choice_is_stored_for_a_contact_in_any_status(
    rig: _Rig, status: ContactStatus
) -> None:
    contact = await rig.contact(status)

    result = await rig.actions.set_gender(contact.id, FEMALE)

    assert result.outcome is GenderOutcome.APPLIED
    assert await rig.stored_gender(contact.id) is FEMALE
    assert result.card is not None
    assert result.card.status is status
    assert result.card.gender == ResolvedGender(value=FEMALE, source=GenderSource.OWNER)
    assert (await rig._current(contact.id)).status is status


async def test_the_card_is_rebuilt_from_the_current_state(
    contacts: SqlContactRepository, messages: SqlMessageRepository, modes: SqlModeRepository
) -> None:
    rig = _Rig(contacts, messages, modes)
    contact = await rig.contact(name="Анна")
    await modes.create(ModeCreate(name="flirt", prompt_fragment="fake"))
    await modes.create(ModeCreate(name="retired", prompt_fragment="fake", enabled=False))
    await messages.append(
        MessageCreate(
            contact_id=contact.id,
            direction=MessageDirection.INCOMING,
            text="hello",
            tg_message_id=1,
        )
    )

    result = await rig.actions.set_gender(contact.id, MALE)

    assert result.card is not None
    assert result.card.contact_id == contact.id
    assert result.card.display_name == "Анна"
    assert result.card.messages == ["hello"]
    assert [mode.name for mode in result.card.modes] == ["default", "flirt"]
    assert result.card.gender.value is MALE


async def test_the_result_gender_follows_the_resolver_priority(
    contacts: SqlContactRepository, messages: SqlMessageRepository, modes: SqlModeRepository
) -> None:
    rig = _Rig(contacts, messages, modes, default=FEMALE)
    contact = await rig.contact(name="Иван")

    result = await rig.actions.set_gender(contact.id, FEMALE)

    assert result.gender == ResolvedGender(value=FEMALE, source=GenderSource.OWNER)


async def test_concurrent_choices_are_serialised(rig: _Rig) -> None:
    contact = await rig.contact()

    results = await asyncio.gather(
        rig.actions.set_gender(contact.id, FEMALE),
        rig.actions.set_gender(contact.id, MALE),
        rig.actions.set_gender(contact.id, MALE),
    )

    assert [result.outcome for result in results] == [
        GenderOutcome.APPLIED,
        GenderOutcome.APPLIED,
        GenderOutcome.ALREADY_APPLIED,
    ]
    assert await rig.stored_gender(contact.id) is MALE


async def test_a_choice_changes_neither_status_nor_mode_nor_the_rows_of_other_contacts(
    rig: _Rig, contacts: SqlContactRepository
) -> None:
    contact = await rig.contact(ContactStatus.ACTIVE)
    other = await contacts.create(ContactCreate(tg_id=2))
    before = await rig._current(contact.id)

    await rig.actions.set_gender(contact.id, FEMALE)

    after = await rig._current(contact.id)
    assert (after.status, after.mode_id) == (before.status, before.mode_id)
    assert await rig.stored_gender(other.id) is None
