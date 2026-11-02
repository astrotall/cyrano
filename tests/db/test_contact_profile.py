from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy.engine import URL

from cyrano.db.repositories import SqlContactRepository
from cyrano.domain.errors import ContactNotFound
from cyrano.domain.models import (
    Contact,
    ContactCreate,
    ContactProfile,
    ContactUpdate,
    LeomatchProfile,
)
from tests.db.conftest import fetch_all

MISSING_ID = 999_999
EN_DASH = "\N{EN DASH}"
COLLECTED_AT = datetime(2025, 3, 4, 5, 6, 7, tzinfo=UTC)
CYRILLIC_BIO = 'Фейковая биография with "quotes", emoji \U0001f338 and a backslash \\'


async def _contact(contacts: SqlContactRepository) -> Contact:
    return await contacts.create(
        ContactCreate(tg_id=100, username="fake_old", display_name="Oldfirst Oldlast")
    )


def _profile() -> ContactProfile:
    return ContactProfile(
        username="fake_fresh",
        display_name="Freshfirst",
        bio=CYRILLIC_BIO,
        photo="profiles/1.jpg",
        leomatch=LeomatchProfile(
            raw_text=f"Фейк, 25, Город {EN_DASH} про меня",
            related_texts=["match text"],
            name="Фейк",
            age=25,
            city="Город",
            about="про меня",
        ),
        collected_at=COLLECTED_AT,
    )


async def test_update_identity_replaces_the_username_and_display_name(
    contacts: SqlContactRepository,
) -> None:
    contact = await _contact(contacts)

    updated = await contacts.update_identity(contact.id, "fake_fresh", "Freshfirst Freshlast")

    assert (updated.username, updated.display_name) == ("fake_fresh", "Freshfirst Freshlast")
    stored = await contacts.get_by_id(contact.id)
    assert stored is not None
    assert (stored.username, stored.display_name) == ("fake_fresh", "Freshfirst Freshlast")
    assert stored.tg_id == contact.tg_id
    assert stored.status is contact.status


async def test_update_identity_can_clear_both_names(contacts: SqlContactRepository) -> None:
    contact = await _contact(contacts)

    updated = await contacts.update_identity(contact.id, None, None)

    assert (updated.username, updated.display_name) == (None, None)


async def test_update_identity_with_the_same_values_does_not_move_updated_at(
    contacts: SqlContactRepository,
) -> None:
    contact = await _contact(contacts)
    await asyncio.sleep(0.02)

    unchanged = await contacts.update_identity(contact.id, "fake_old", "Oldfirst Oldlast")
    changed = await contacts.update_identity(contact.id, "fake_new", "Oldfirst Oldlast")

    assert unchanged.updated_at == contact.updated_at
    assert changed.updated_at > contact.updated_at


async def test_update_identity_does_not_touch_the_profile_or_the_summary(
    contacts: SqlContactRepository,
) -> None:
    contact = await _contact(contacts)
    await contacts.update(
        contact.id,
        ContactUpdate(profile=_profile().model_dump(mode="json"), summary={"topic": "fake"}),
    )

    updated = await contacts.update_identity(contact.id, "other", "Other")

    assert updated.profile == _profile().model_dump(mode="json")
    assert updated.summary == {"topic": "fake"}


async def test_update_identity_for_a_missing_contact_raises(
    contacts: SqlContactRepository,
) -> None:
    with pytest.raises(ContactNotFound):
        await contacts.update_identity(MISSING_ID, "x", "y")


async def test_the_profile_round_trips_through_jsonb(contacts: SqlContactRepository) -> None:
    contact = await _contact(contacts)

    await contacts.update(contact.id, ContactUpdate(profile=_profile().model_dump(mode="json")))

    stored = await contacts.get_by_id(contact.id)
    assert stored is not None
    assert ContactProfile.model_validate(stored.profile) == _profile()
    assert ContactProfile.model_validate(stored.profile).collected_at == COLLECTED_AT


async def test_the_profile_is_stored_as_a_json_object_with_a_timestamp_string(
    contacts: SqlContactRepository, migrated_url: URL
) -> None:
    contact = await _contact(contacts)
    await contacts.update(contact.id, ContactUpdate(profile=_profile().model_dump(mode="json")))

    rows = await fetch_all(
        migrated_url,
        "SELECT jsonb_typeof(profile), profile->>'photo', profile->'leomatch'->>'age', "
        "profile->>'collected_at' FROM contacts",
    )
    assert rows[0][:3] == ("object", "profiles/1.jpg", "25")
    assert datetime.fromisoformat(str(rows[0][3])) == COLLECTED_AT


async def test_an_empty_profile_of_a_new_contact_reads_as_not_collected(
    contacts: SqlContactRepository,
) -> None:
    contact = await _contact(contacts)

    assert ContactProfile.model_validate(contact.profile).collected_at is None


async def test_a_second_profile_write_replaces_the_first(contacts: SqlContactRepository) -> None:
    contact = await _contact(contacts)
    await contacts.update(contact.id, ContactUpdate(profile=_profile().model_dump(mode="json")))

    await contacts.update(
        contact.id, ContactUpdate(profile=ContactProfile().model_dump(mode="json"))
    )

    stored = await contacts.get_by_id(contact.id)
    assert stored is not None
    assert ContactProfile.model_validate(stored.profile) == ContactProfile()
