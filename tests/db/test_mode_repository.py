from __future__ import annotations

import pytest
from sqlalchemy import update

from cyrano.db.models import ModeRow
from cyrano.db.repositories import SqlContactRepository, SqlModeRepository
from cyrano.db.session import SessionFactory
from cyrano.domain.enums import ContactStatus
from cyrano.domain.errors import DefaultModeUnavailable, ModeAlreadyExists
from cyrano.domain.models import DEFAULT_MODE_NAME, ContactCreate, Mode, ModeCreate

MISSING_ID = 999_999
FAKE_EXAMPLES: list[dict[str, object]] = [{"in": "fake question", "out": "fake answer"}]


async def _default(modes: SqlModeRepository) -> Mode:
    mode = await modes.get_by_name(DEFAULT_MODE_NAME)
    assert mode is not None
    return mode


async def _alter_default(session_factory: SessionFactory, *, enabled: bool, name: str) -> None:
    async with session_factory.begin() as session:
        await session.execute(
            update(ModeRow)
            .where(ModeRow.name == DEFAULT_MODE_NAME)
            .values(enabled=enabled, name=name)
        )


async def test_default_mode_is_seeded_once(modes: SqlModeRepository) -> None:
    default = await _default(modes)

    assert await modes.list_enabled() == [default]
    assert default.few_shot_examples == []
    assert default.enabled is True


async def test_create_and_get(modes: SqlModeRepository) -> None:
    created = await modes.create(
        ModeCreate(name="fake", prompt_fragment="fake fragment", few_shot_examples=FAKE_EXAMPLES)
    )

    assert created.few_shot_examples == FAKE_EXAMPLES
    assert created.enabled is True
    assert await modes.get_by_id(created.id) == created
    assert await modes.get_by_name("fake") == created


async def test_get_missing_mode_returns_none(modes: SqlModeRepository) -> None:
    assert await modes.get_by_id(MISSING_ID) is None
    assert await modes.get_by_name("missing") is None


async def test_get_returns_a_disabled_mode_as_stored(modes: SqlModeRepository) -> None:
    disabled = await modes.create(ModeCreate(name="off", prompt_fragment="fake", enabled=False))

    assert await modes.get_by_id(disabled.id) == disabled
    assert await modes.get_by_name("off") == disabled


async def test_duplicate_name_raises(modes: SqlModeRepository) -> None:
    with pytest.raises(ModeAlreadyExists):
        await modes.create(ModeCreate(name=DEFAULT_MODE_NAME, prompt_fragment="fake"))


async def test_list_enabled_skips_disabled_modes(modes: SqlModeRepository) -> None:
    default = await _default(modes)
    enabled = await modes.create(ModeCreate(name="on", prompt_fragment="fake"))
    await modes.create(ModeCreate(name="off", prompt_fragment="fake", enabled=False))

    assert await modes.list_enabled() == [default, enabled]


async def test_resolve_returns_an_enabled_mode(modes: SqlModeRepository) -> None:
    enabled = await modes.create(ModeCreate(name="on", prompt_fragment="fake"))

    assert await modes.resolve_by_id(enabled.id) == enabled
    assert await modes.resolve_by_name("on") == enabled


async def test_disabled_mode_falls_back_to_default(modes: SqlModeRepository) -> None:
    default = await _default(modes)
    disabled = await modes.create(ModeCreate(name="off", prompt_fragment="fake", enabled=False))

    assert await modes.resolve_by_id(disabled.id) == default
    assert await modes.resolve_by_name("off") == default


async def test_missing_mode_falls_back_to_default(modes: SqlModeRepository) -> None:
    default = await _default(modes)

    assert await modes.resolve_by_id(MISSING_ID) == default
    assert await modes.resolve_by_id(None) == default
    assert await modes.resolve_by_name("missing") == default


async def test_contact_on_a_disabled_mode_resolves_to_default(
    modes: SqlModeRepository, contacts: SqlContactRepository
) -> None:
    default = await _default(modes)
    disabled = await modes.create(ModeCreate(name="off", prompt_fragment="fake", enabled=False))
    contact = await contacts.create(ContactCreate(tg_id=100))
    await contacts.set_status(contact.id, ContactStatus.PENDING)

    active = await contacts.set_status(contact.id, ContactStatus.ACTIVE, mode_id=disabled.id)

    assert active.mode_id == disabled.id
    assert await modes.resolve_by_id(active.mode_id) == default


async def test_disabled_default_refuses_to_resolve(
    modes: SqlModeRepository, session_factory: SessionFactory
) -> None:
    await _alter_default(session_factory, enabled=False, name=DEFAULT_MODE_NAME)

    with pytest.raises(DefaultModeUnavailable):
        await modes.resolve_by_name(DEFAULT_MODE_NAME)


async def test_missing_default_refuses_to_resolve(
    modes: SqlModeRepository, session_factory: SessionFactory
) -> None:
    await _alter_default(session_factory, enabled=True, name="renamed")

    with pytest.raises(DefaultModeUnavailable):
        await modes.resolve_by_id(None)
