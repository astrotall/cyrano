from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import update
from sqlalchemy.engine import URL

from cyrano.db.models import ContactRow
from cyrano.db.repositories import SqlContactRepository, SqlModeRepository
from cyrano.db.session import SessionFactory
from cyrano.domain.enums import ContactStatus
from cyrano.domain.errors import (
    ActivationWithoutMode,
    ContactAlreadyExists,
    ContactNotFound,
    InvalidStatusTransition,
    ModeNotFound,
)
from cyrano.domain.models import (
    DEFAULT_MODE_NAME,
    MAX_BIGINT,
    Contact,
    ContactCreate,
    ContactUpdate,
    ModeCreate,
)
from tests.db.conftest import fetch_all
from tests.domain.cases import ALLOWED, FORBIDDEN

MISSING_ID = 999_999
FAKE_PROFILE: dict[str, object] = {
    "bio": 'Fake bio with кириллица and "quotes"',
    "age": 30,
    "verified": True,
    "photo": None,
    "tags": ["one", "two"],
    "nested": {"depth": {"value": 1.5}},
}
FAKE_SUMMARY: dict[str, object] = {"topics": ["fake"], "last": {"mood": "neutral"}}


async def _new_contact(
    contacts: SqlContactRepository, tg_id: int = 100, username: str | None = "fake_user"
) -> Contact:
    return await contacts.create(
        ContactCreate(tg_id=tg_id, username=username, display_name="Fake Name")
    )


async def _default_mode_id(session_factory: SessionFactory) -> int:
    mode = await SqlModeRepository(session_factory).get_by_name(DEFAULT_MODE_NAME)
    assert mode is not None
    return mode.id


async def _contact_in(
    status: ContactStatus,
    contacts: SqlContactRepository,
    session_factory: SessionFactory,
    *,
    with_mode: bool = True,
) -> Contact:
    contact = await _new_contact(contacts)
    mode_id = await _default_mode_id(session_factory) if with_mode else None
    async with session_factory.begin() as session:
        await session.execute(
            update(ContactRow)
            .where(ContactRow.id == contact.id)
            .values(status=status, mode_id=mode_id)
        )
    refreshed = await contacts.get_by_id(contact.id)
    assert refreshed is not None
    return refreshed


async def test_create_returns_a_new_contact(contacts: SqlContactRepository) -> None:
    contact = await contacts.create(
        ContactCreate(tg_id=100, username="fake_user", display_name="Fake Name")
    )

    assert isinstance(contact, Contact)
    assert contact.id > 0
    assert contact.tg_id == 100
    assert contact.username == "fake_user"
    assert contact.display_name == "Fake Name"
    assert contact.status is ContactStatus.NEW
    assert contact.mode_id is None
    assert contact.profile == {}
    assert contact.summary == {}
    assert contact.last_message_at is None


async def test_create_without_optional_fields(contacts: SqlContactRepository) -> None:
    contact = await contacts.create(ContactCreate(tg_id=100))

    assert contact.username is None
    assert contact.display_name is None


async def test_create_stores_the_largest_telegram_id(contacts: SqlContactRepository) -> None:
    await contacts.create(ContactCreate(tg_id=MAX_BIGINT))

    assert await contacts.get_by_tg_id(MAX_BIGINT) is not None


async def test_duplicate_tg_id_raises_without_exposing_the_id(
    contacts: SqlContactRepository,
) -> None:
    await _new_contact(contacts, tg_id=424242)

    with pytest.raises(ContactAlreadyExists) as caught:
        await _new_contact(contacts, tg_id=424242)

    assert "424242" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True


async def test_get_by_id_and_tg_id(contacts: SqlContactRepository) -> None:
    created = await _new_contact(contacts)

    assert await contacts.get_by_id(created.id) == created
    assert await contacts.get_by_tg_id(created.tg_id) == created


async def test_get_missing_contact_returns_none(contacts: SqlContactRepository) -> None:
    assert await contacts.get_by_id(MISSING_ID) is None
    assert await contacts.get_by_tg_id(MISSING_ID) is None


@pytest.mark.parametrize(("current", "requested"), ALLOWED)
async def test_allowed_transition_is_stored(
    current: ContactStatus,
    requested: ContactStatus,
    contacts: SqlContactRepository,
    session_factory: SessionFactory,
) -> None:
    contact = await _contact_in(current, contacts, session_factory)

    updated = await contacts.set_status(contact.id, requested)

    assert updated.status is requested
    stored = await contacts.get_by_id(contact.id)
    assert stored is not None
    assert stored.status is requested


@pytest.mark.parametrize(("current", "requested"), FORBIDDEN)
async def test_forbidden_transition_raises_and_changes_nothing(
    current: ContactStatus,
    requested: ContactStatus,
    contacts: SqlContactRepository,
    session_factory: SessionFactory,
) -> None:
    contact = await _contact_in(current, contacts, session_factory)

    with pytest.raises(InvalidStatusTransition):
        await contacts.set_status(contact.id, requested)

    assert await contacts.get_by_id(contact.id) == contact


@pytest.mark.parametrize("status", list(ContactStatus))
async def test_same_status_is_a_no_op_and_writes_nothing(
    status: ContactStatus,
    contacts: SqlContactRepository,
    session_factory: SessionFactory,
) -> None:
    contact = await _contact_in(status, contacts, session_factory)

    assert await contacts.set_status(contact.id, status) == contact
    assert await contacts.get_by_id(contact.id) == contact


async def test_same_status_does_not_change_the_mode(
    contacts: SqlContactRepository, modes: SqlModeRepository, session_factory: SessionFactory
) -> None:
    contact = await _contact_in(ContactStatus.ACTIVE, contacts, session_factory)
    other = await modes.create(ModeCreate(name="other", prompt_fragment="fake"))

    assert await contacts.set_status(contact.id, ContactStatus.ACTIVE, mode_id=other.id) == contact


async def test_activation_without_a_mode_is_refused(
    contacts: SqlContactRepository, session_factory: SessionFactory
) -> None:
    contact = await _contact_in(ContactStatus.PENDING, contacts, session_factory, with_mode=False)

    with pytest.raises(ActivationWithoutMode):
        await contacts.set_status(contact.id, ContactStatus.ACTIVE)

    assert await contacts.get_by_id(contact.id) == contact


async def test_activation_sets_the_given_mode(
    contacts: SqlContactRepository, session_factory: SessionFactory
) -> None:
    contact = await _contact_in(ContactStatus.PENDING, contacts, session_factory, with_mode=False)
    mode_id = await _default_mode_id(session_factory)

    updated = await contacts.set_status(contact.id, ContactStatus.ACTIVE, mode_id=mode_id)

    assert updated.status is ContactStatus.ACTIVE
    assert updated.mode_id == mode_id


async def test_activation_keeps_the_mode_already_set(
    contacts: SqlContactRepository, session_factory: SessionFactory
) -> None:
    contact = await _contact_in(ContactStatus.PAUSED, contacts, session_factory)

    updated = await contacts.set_status(contact.id, ContactStatus.ACTIVE)

    assert updated.status is ContactStatus.ACTIVE
    assert updated.mode_id == contact.mode_id


async def test_activation_with_a_missing_mode_changes_nothing(
    contacts: SqlContactRepository, session_factory: SessionFactory
) -> None:
    contact = await _contact_in(ContactStatus.PENDING, contacts, session_factory, with_mode=False)

    with pytest.raises(ModeNotFound):
        await contacts.set_status(contact.id, ContactStatus.ACTIVE, mode_id=MISSING_ID)

    assert await contacts.get_by_id(contact.id) == contact


async def test_mode_id_is_ignored_when_not_activating(
    contacts: SqlContactRepository, modes: SqlModeRepository, session_factory: SessionFactory
) -> None:
    contact = await _contact_in(ContactStatus.ACTIVE, contacts, session_factory)
    other = await modes.create(ModeCreate(name="other", prompt_fragment="fake"))

    updated = await contacts.set_status(contact.id, ContactStatus.PAUSED, mode_id=other.id)

    assert updated.status is ContactStatus.PAUSED
    assert updated.mode_id == contact.mode_id


async def test_set_status_on_a_missing_contact_raises(contacts: SqlContactRepository) -> None:
    with pytest.raises(ContactNotFound):
        await contacts.set_status(MISSING_ID, ContactStatus.PENDING)


async def test_ignored_wins_over_a_concurrent_resume(
    contacts: SqlContactRepository, session_factory: SessionFactory
) -> None:
    contact = await _contact_in(ContactStatus.PAUSED, contacts, session_factory)

    await asyncio.gather(
        contacts.set_status(contact.id, ContactStatus.IGNORED),
        contacts.set_status(contact.id, ContactStatus.ACTIVE),
        return_exceptions=True,
    )

    stored = await contacts.get_by_id(contact.id)
    assert stored is not None
    assert stored.status is ContactStatus.IGNORED


async def test_set_mode_changes_only_the_mode(
    contacts: SqlContactRepository, session_factory: SessionFactory
) -> None:
    contact = await _new_contact(contacts)
    mode_id = await _default_mode_id(session_factory)

    updated = await contacts.set_mode(contact.id, mode_id)

    assert updated.mode_id == mode_id
    assert updated.status is ContactStatus.NEW


async def test_set_mode_failures(
    contacts: SqlContactRepository, session_factory: SessionFactory
) -> None:
    contact = await _new_contact(contacts)
    mode_id = await _default_mode_id(session_factory)

    with pytest.raises(ModeNotFound):
        await contacts.set_mode(contact.id, MISSING_ID)
    with pytest.raises(ContactNotFound):
        await contacts.set_mode(MISSING_ID, mode_id)


async def test_jsonb_round_trip_for_profile_and_summary(
    contacts: SqlContactRepository, migrated_url: URL
) -> None:
    contact = await contacts.create(ContactCreate(tg_id=100, profile=FAKE_PROFILE))
    assert contact.profile == FAKE_PROFILE

    await contacts.update(contact.id, ContactUpdate(summary=FAKE_SUMMARY))

    stored = await contacts.get_by_id(contact.id)
    assert stored is not None
    assert stored.profile == FAKE_PROFILE
    assert stored.summary == FAKE_SUMMARY
    rows = await fetch_all(
        migrated_url,
        "SELECT jsonb_typeof(profile), jsonb_typeof(summary), "
        "profile -> 'nested' -> 'depth' ->> 'value' FROM contacts",
    )
    assert tuple(rows[0]) == ("object", "object", "1.5")


async def test_update_leaves_unset_fields_and_clears_with_an_empty_object(
    contacts: SqlContactRepository,
) -> None:
    contact = await contacts.create(ContactCreate(tg_id=100, profile=FAKE_PROFILE))

    updated = await contacts.update(contact.id, ContactUpdate(summary=FAKE_SUMMARY))
    assert updated.profile == FAKE_PROFILE

    cleared = await contacts.update(contact.id, ContactUpdate(profile={}))
    assert cleared.profile == {}
    assert cleared.summary == FAKE_SUMMARY


async def test_empty_update_writes_nothing(contacts: SqlContactRepository) -> None:
    contact = await _new_contact(contacts)

    assert await contacts.update(contact.id, ContactUpdate()) == contact


async def test_update_on_a_missing_contact_raises(contacts: SqlContactRepository) -> None:
    with pytest.raises(ContactNotFound):
        await contacts.update(MISSING_ID, ContactUpdate(profile={}))


async def test_touch_last_message_at_moves_forward_only(contacts: SqlContactRepository) -> None:
    contact = await _new_contact(contacts)
    later = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    earlier = later - timedelta(hours=1)

    assert (await contacts.touch_last_message_at(contact.id, later)).last_message_at == later
    assert (await contacts.touch_last_message_at(contact.id, earlier)).last_message_at == later


async def test_touch_last_message_at_stores_other_offsets_as_utc(
    contacts: SqlContactRepository,
) -> None:
    contact = await _new_contact(contacts)
    moscow_time = datetime(2026, 5, 1, 15, 0, tzinfo=timezone(timedelta(hours=3)))

    updated = await contacts.touch_last_message_at(contact.id, moscow_time)

    assert updated.last_message_at == moscow_time
    assert updated.last_message_at is not None
    assert updated.last_message_at.utcoffset() == timedelta(0)


async def test_touch_last_message_at_rejects_a_naive_datetime(
    contacts: SqlContactRepository,
) -> None:
    contact = await _new_contact(contacts)

    with pytest.raises(ValueError, match="timezone-aware"):
        await contacts.touch_last_message_at(contact.id, datetime(2026, 5, 1))


async def test_touch_last_message_at_on_a_missing_contact_raises(
    contacts: SqlContactRepository,
) -> None:
    with pytest.raises(ContactNotFound):
        await contacts.touch_last_message_at(MISSING_ID, datetime.now(UTC))


async def test_list_by_status(contacts: SqlContactRepository) -> None:
    first = await _new_contact(contacts, tg_id=1)
    second = await _new_contact(contacts, tg_id=2)
    moved = await _new_contact(contacts, tg_id=3)
    await contacts.set_status(moved.id, ContactStatus.PENDING)

    assert await contacts.list_by_status(ContactStatus.NEW) == [first, second]
    assert [c.id for c in await contacts.list_by_status(ContactStatus.PENDING)] == [moved.id]
    assert await contacts.list_by_status(ContactStatus.IGNORED) == []


async def test_timestamps_are_timezone_aware_utc(contacts: SqlContactRepository) -> None:
    contact = await _new_contact(contacts)
    updated = await contacts.touch_last_message_at(contact.id, datetime.now(UTC))

    for value in (updated.created_at, updated.updated_at, updated.last_message_at):
        assert value is not None
        assert value.utcoffset() == timedelta(0)


async def test_updated_at_moves_on_a_real_change_only(
    contacts: SqlContactRepository, session_factory: SessionFactory
) -> None:
    contact = await _new_contact(contacts)

    changed = await contacts.set_status(contact.id, ContactStatus.PENDING)
    assert changed.updated_at > contact.updated_at
    assert changed.created_at == contact.created_at

    unchanged = await contacts.set_status(contact.id, ContactStatus.PENDING)
    assert unchanged.updated_at == changed.updated_at


async def test_rows_hold_the_enum_value_not_the_member_name(
    contacts: SqlContactRepository, migrated_url: URL
) -> None:
    await _new_contact(contacts)

    rows = await fetch_all(migrated_url, "SELECT status::text FROM contacts")

    assert rows == [(ContactStatus.NEW.value,)]
