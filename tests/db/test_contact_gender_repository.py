from __future__ import annotations

import pytest
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError

from cyrano.db.repositories import SqlContactRepository
from cyrano.domain.enums import ContactGender, ContactStatus
from cyrano.domain.errors import ContactNotFound
from cyrano.domain.models import Contact, ContactCreate, ContactUpdate
from tests.db.conftest import execute, fetch_all

MISSING_ID = 999_999


async def _contact(contacts: SqlContactRepository, tg_id: int = 100) -> Contact:
    return await contacts.create(ContactCreate(tg_id=tg_id, display_name="Fake Name"))


async def test_a_new_contact_has_no_gender(contacts: SqlContactRepository) -> None:
    contact = await _contact(contacts)

    assert contact.gender is None
    stored = await contacts.get_by_id(contact.id)
    assert stored is not None
    assert stored.gender is None


@pytest.mark.parametrize("gender", [ContactGender.FEMALE, ContactGender.MALE])
async def test_set_gender_stores_the_choice(
    contacts: SqlContactRepository, gender: ContactGender
) -> None:
    contact = await _contact(contacts)

    returned = await contacts.set_gender(contact.id, gender)

    assert returned.gender is gender
    stored = await contacts.get_by_id(contact.id)
    assert stored is not None
    assert stored.gender is gender


async def test_set_gender_can_change_the_choice(contacts: SqlContactRepository) -> None:
    contact = await _contact(contacts)
    await contacts.set_gender(contact.id, ContactGender.FEMALE)

    returned = await contacts.set_gender(contact.id, ContactGender.MALE)

    assert returned.gender is ContactGender.MALE


async def test_set_gender_with_none_clears_the_choice(contacts: SqlContactRepository) -> None:
    contact = await _contact(contacts)
    await contacts.set_gender(contact.id, ContactGender.FEMALE)

    returned = await contacts.set_gender(contact.id, None)

    assert returned.gender is None
    stored = await contacts.get_by_id(contact.id)
    assert stored is not None
    assert stored.gender is None


async def test_clearing_an_unset_gender_changes_nothing(contacts: SqlContactRepository) -> None:
    contact = await _contact(contacts)

    returned = await contacts.set_gender(contact.id, None)

    assert returned.gender is None


async def test_set_gender_of_a_missing_contact_raises(contacts: SqlContactRepository) -> None:
    with pytest.raises(ContactNotFound):
        await contacts.set_gender(MISSING_ID, ContactGender.FEMALE)


async def test_set_gender_touches_no_other_column(contacts: SqlContactRepository) -> None:
    contact = await _contact(contacts)
    await contacts.update(contact.id, ContactUpdate(profile={"bio": "x"}, summary={"a": 1}))
    before = await contacts.get_by_id(contact.id)
    assert before is not None

    after = await contacts.set_gender(contact.id, ContactGender.MALE)

    assert after.model_dump(exclude={"gender", "updated_at"}) == before.model_dump(
        exclude={"gender", "updated_at"}
    )


@pytest.mark.parametrize(
    "status", [ContactStatus.NEW, ContactStatus.PENDING, ContactStatus.IGNORED]
)
async def test_a_choice_survives_status_changes(
    contacts: SqlContactRepository, status: ContactStatus
) -> None:
    contact = await _contact(contacts)
    await contacts.set_gender(contact.id, ContactGender.FEMALE)
    if status in {ContactStatus.PENDING, ContactStatus.IGNORED}:
        await contacts.set_status(contact.id, ContactStatus.PENDING)
    if status is ContactStatus.IGNORED:
        await contacts.set_status(contact.id, ContactStatus.IGNORED)

    stored = await contacts.get_by_id(contact.id)

    assert stored is not None
    assert stored.gender is ContactGender.FEMALE


async def test_the_database_refuses_any_other_value(
    contacts: SqlContactRepository, migrated_url: URL
) -> None:
    contact = await _contact(contacts)

    for bad in ("'other'", "'unknown'", "'FEMALE'", "''"):
        with pytest.raises(DBAPIError, match="ck_contacts_gender_allowed"):
            await execute(
                migrated_url,
                f"UPDATE contacts SET gender = {bad} WHERE id = {contact.id}",  # noqa: S608
            )

    await execute(migrated_url, f"UPDATE contacts SET gender = 'male' WHERE id = {contact.id}")  # noqa: S608
    assert await fetch_all(migrated_url, "SELECT gender FROM contacts") == [("male",)]
