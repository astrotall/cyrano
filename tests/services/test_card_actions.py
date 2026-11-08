from __future__ import annotations

import asyncio

from cyrano.db.repositories import SqlContactRepository, SqlModeRepository
from cyrano.domain.enums import CardOutcome, ContactGender, ContactStatus
from cyrano.domain.models import CardActionResult, Contact, ContactCreate, Mode, ModeCreate
from cyrano.services.card_actions import CardActions
from cyrano.services.gender import GenderResolver

MISSING_ID = 987654


class _Rig:
    def __init__(
        self,
        contacts: SqlContactRepository,
        modes: SqlModeRepository,
        flirt: Mode,
        retired: Mode,
        default: Mode,
        default_gender: ContactGender | None = ContactGender.FEMALE,
    ) -> None:
        self.contacts = contacts
        self.modes = modes
        self.flirt = flirt
        self.retired = retired
        self.default = default
        self.actions = CardActions(contacts, modes, GenderResolver(default_gender))

    async def contact(
        self, status: ContactStatus = ContactStatus.PENDING, tg_id: int = 1
    ) -> Contact:
        created = await self.contacts.create(ContactCreate(tg_id=tg_id))
        if status is ContactStatus.NEW:
            return created
        pending = await self.contacts.set_status(created.id, ContactStatus.PENDING)
        if status is ContactStatus.PENDING:
            return pending
        if status is ContactStatus.IGNORED:
            return await self.contacts.set_status(created.id, ContactStatus.IGNORED)
        active = await self.contacts.set_status(
            created.id, ContactStatus.ACTIVE, mode_id=self.default.id
        )
        if status is ContactStatus.PAUSED:
            return await self.contacts.set_status(created.id, ContactStatus.PAUSED)
        return active

    async def current(self, contact: Contact) -> Contact:
        found = await self.contacts.get_by_id(contact.id)
        assert found is not None
        return found


async def _rig(
    contacts: SqlContactRepository,
    modes: SqlModeRepository,
    default_gender: ContactGender | None = ContactGender.FEMALE,
) -> _Rig:
    default = await modes.get_by_name("default")
    assert default is not None
    flirt = await modes.create(ModeCreate(name="flirt", prompt_fragment="fake"))
    retired = await modes.create(ModeCreate(name="retired", prompt_fragment="fake", enabled=False))
    return _Rig(contacts, modes, flirt, retired, default, default_gender)


async def test_reply_activates_a_pending_contact_with_the_mode(
    contacts: SqlContactRepository, modes: SqlModeRepository
) -> None:
    rig = await _rig(contacts, modes)
    contact = await rig.contact()

    result = await rig.actions.reply(contact.id, rig.flirt.id)

    assert result == CardActionResult(
        outcome=CardOutcome.APPLIED, status=ContactStatus.ACTIVE, mode_name="flirt"
    )
    stored = await rig.current(contact)
    assert (stored.status, stored.mode_id) == (ContactStatus.ACTIVE, rig.flirt.id)


async def test_a_double_click_on_reply_is_already_applied_and_changes_nothing(
    contacts: SqlContactRepository, modes: SqlModeRepository
) -> None:
    rig = await _rig(contacts, modes)
    contact = await rig.contact()
    await rig.actions.reply(contact.id, rig.flirt.id)
    before = await rig.current(contact)

    result = await rig.actions.reply(contact.id, rig.flirt.id)

    assert result == CardActionResult(
        outcome=CardOutcome.ALREADY_APPLIED, status=ContactStatus.ACTIVE, mode_name="flirt"
    )
    assert await rig.current(contact) == before


async def test_reply_with_another_mode_on_an_active_contact_is_refused(
    contacts: SqlContactRepository, modes: SqlModeRepository
) -> None:
    rig = await _rig(contacts, modes)
    contact = await rig.contact(ContactStatus.ACTIVE)

    result = await rig.actions.reply(contact.id, rig.flirt.id)

    assert result == CardActionResult(
        outcome=CardOutcome.REFUSED, status=ContactStatus.ACTIVE, mode_name="default"
    )
    assert (await rig.current(contact)).mode_id == rig.default.id


async def test_reply_after_ignore_is_refused(
    contacts: SqlContactRepository, modes: SqlModeRepository
) -> None:
    rig = await _rig(contacts, modes)
    contact = await rig.contact()
    await rig.actions.ignore(contact.id)

    result = await rig.actions.reply(contact.id, rig.default.id)

    assert result == CardActionResult(outcome=CardOutcome.REFUSED, status=ContactStatus.IGNORED)
    assert (await rig.current(contact)).status is ContactStatus.IGNORED


async def test_reply_is_refused_for_a_contact_that_is_new_or_paused(
    contacts: SqlContactRepository, modes: SqlModeRepository
) -> None:
    rig = await _rig(contacts, modes)
    fresh = await rig.contact(ContactStatus.NEW, tg_id=1)
    paused = await rig.contact(ContactStatus.PAUSED, tg_id=2)

    for contact, status in ((fresh, ContactStatus.NEW), (paused, ContactStatus.PAUSED)):
        result = await rig.actions.reply(contact.id, rig.default.id)
        assert (result.outcome, result.status) == (CardOutcome.REFUSED, status)
        assert (await rig.current(contact)).status is status


async def test_ignore_ignores_a_pending_contact(
    contacts: SqlContactRepository, modes: SqlModeRepository
) -> None:
    rig = await _rig(contacts, modes)
    contact = await rig.contact()

    result = await rig.actions.ignore(contact.id)

    assert result == CardActionResult(outcome=CardOutcome.APPLIED, status=ContactStatus.IGNORED)
    assert (await rig.current(contact)).status is ContactStatus.IGNORED


async def test_ignore_twice_is_already_applied(
    contacts: SqlContactRepository, modes: SqlModeRepository
) -> None:
    rig = await _rig(contacts, modes)
    contact = await rig.contact()
    await rig.actions.ignore(contact.id)

    result = await rig.actions.ignore(contact.id)

    assert result == CardActionResult(
        outcome=CardOutcome.ALREADY_APPLIED, status=ContactStatus.IGNORED
    )


async def test_ignore_on_an_active_contact_is_refused_and_keeps_it_active(
    contacts: SqlContactRepository, modes: SqlModeRepository
) -> None:
    rig = await _rig(contacts, modes)
    contact = await rig.contact(ContactStatus.ACTIVE)

    result = await rig.actions.ignore(contact.id)

    assert (result.outcome, result.status) == (CardOutcome.REFUSED, ContactStatus.ACTIVE)
    stored = await rig.current(contact)
    assert (stored.status, stored.mode_id) == (ContactStatus.ACTIVE, rig.default.id)


async def test_ignore_is_refused_for_a_new_or_paused_contact(
    contacts: SqlContactRepository, modes: SqlModeRepository
) -> None:
    rig = await _rig(contacts, modes)
    fresh = await rig.contact(ContactStatus.NEW, tg_id=1)
    paused = await rig.contact(ContactStatus.PAUSED, tg_id=2)

    for contact, status in ((fresh, ContactStatus.NEW), (paused, ContactStatus.PAUSED)):
        result = await rig.actions.ignore(contact.id)
        assert (result.outcome, result.status) == (CardOutcome.REFUSED, status)
        assert (await rig.current(contact)).status is status


async def test_an_unknown_contact_is_not_found(
    contacts: SqlContactRepository, modes: SqlModeRepository
) -> None:
    rig = await _rig(contacts, modes)

    reply = await rig.actions.reply(MISSING_ID, rig.default.id)
    ignore = await rig.actions.ignore(MISSING_ID)

    assert reply == CardActionResult(outcome=CardOutcome.NOT_FOUND)
    assert ignore == CardActionResult(outcome=CardOutcome.NOT_FOUND)


async def test_a_disabled_mode_is_unavailable_and_changes_nothing(
    contacts: SqlContactRepository, modes: SqlModeRepository
) -> None:
    rig = await _rig(contacts, modes)
    contact = await rig.contact()

    result = await rig.actions.reply(contact.id, rig.retired.id)

    assert result == CardActionResult(
        outcome=CardOutcome.MODE_UNAVAILABLE, status=ContactStatus.PENDING
    )
    stored = await rig.current(contact)
    assert (stored.status, stored.mode_id) == (ContactStatus.PENDING, None)


async def test_a_missing_mode_is_unavailable_and_changes_nothing(
    contacts: SqlContactRepository, modes: SqlModeRepository
) -> None:
    rig = await _rig(contacts, modes)
    contact = await rig.contact()

    result = await rig.actions.reply(contact.id, MISSING_ID)

    assert result.outcome is CardOutcome.MODE_UNAVAILABLE
    assert (await rig.current(contact)).status is ContactStatus.PENDING


async def test_two_clicks_with_different_modes_apply_exactly_one(
    contacts: SqlContactRepository, modes: SqlModeRepository
) -> None:
    rig = await _rig(contacts, modes)
    contact = await rig.contact()

    first, second = await asyncio.gather(
        rig.actions.reply(contact.id, rig.default.id), rig.actions.reply(contact.id, rig.flirt.id)
    )

    assert sorted([first.outcome, second.outcome]) == [CardOutcome.APPLIED, CardOutcome.REFUSED]
    winner = first if first.outcome is CardOutcome.APPLIED else second
    stored = await rig.current(contact)
    assert stored.status is ContactStatus.ACTIVE
    assert winner.mode_name == (await rig.modes.get_by_id(stored.mode_id or 0) or rig.default).name


async def test_a_reply_and_an_ignore_at_the_same_time_never_ignore_an_active_contact(
    contacts: SqlContactRepository, modes: SqlModeRepository
) -> None:
    rig = await _rig(contacts, modes)
    contact = await rig.contact()

    reply, ignore = await asyncio.gather(
        rig.actions.reply(contact.id, rig.default.id), rig.actions.ignore(contact.id)
    )

    stored = await rig.current(contact)
    assert (reply.outcome is CardOutcome.APPLIED) == (stored.status is ContactStatus.ACTIVE)
    assert (ignore.outcome is CardOutcome.APPLIED) == (stored.status is ContactStatus.IGNORED)
    assert {reply.outcome, ignore.outcome} == {CardOutcome.APPLIED, CardOutcome.REFUSED}


async def test_reply_is_refused_with_gender_required_when_the_gender_is_unknown(
    contacts: SqlContactRepository, modes: SqlModeRepository
) -> None:
    rig = await _rig(contacts, modes, default_gender=None)
    contact = await rig.contact()

    result = await rig.actions.reply(contact.id, rig.flirt.id)

    assert result == CardActionResult(
        outcome=CardOutcome.GENDER_REQUIRED, status=ContactStatus.PENDING
    )
    stored = await rig.current(contact)
    assert (stored.status, stored.mode_id) == (ContactStatus.PENDING, None)


async def test_reply_works_for_every_resolved_gender_source(
    contacts: SqlContactRepository, modes: SqlModeRepository
) -> None:
    rig = await _rig(contacts, modes, default_gender=None)
    by_owner = await rig.contact(tg_id=1)
    await contacts.set_gender(by_owner.id, ContactGender.MALE)
    by_name = await contacts.create(ContactCreate(tg_id=2, display_name="Анна"))
    await contacts.set_status(by_name.id, ContactStatus.PENDING)

    first = await rig.actions.reply(by_owner.id, rig.default.id)
    second = await rig.actions.reply(by_name.id, rig.default.id)

    assert first.outcome is CardOutcome.APPLIED
    assert second.outcome is CardOutcome.APPLIED


async def test_reply_works_with_the_default_setting(
    contacts: SqlContactRepository, modes: SqlModeRepository
) -> None:
    rig = await _rig(contacts, modes, default_gender=ContactGender.MALE)
    contact = await rig.contact()

    result = await rig.actions.reply(contact.id, rig.default.id)

    assert result.outcome is CardOutcome.APPLIED


async def test_the_owner_choice_unblocks_a_contact_that_was_refused(
    contacts: SqlContactRepository, modes: SqlModeRepository
) -> None:
    rig = await _rig(contacts, modes, default_gender=None)
    contact = await rig.contact()
    refused = await rig.actions.reply(contact.id, rig.default.id)
    await contacts.set_gender(contact.id, ContactGender.FEMALE)

    allowed = await rig.actions.reply(contact.id, rig.default.id)

    assert refused.outcome is CardOutcome.GENDER_REQUIRED
    assert allowed.outcome is CardOutcome.APPLIED


async def test_the_other_outcomes_do_not_depend_on_the_gender(
    contacts: SqlContactRepository, modes: SqlModeRepository
) -> None:
    rig = await _rig(contacts, modes, default_gender=None)
    active = await rig.contact(ContactStatus.ACTIVE, tg_id=1)
    ignored = await rig.contact(ContactStatus.IGNORED, tg_id=2)
    pending = await rig.contact(tg_id=3)

    same_mode = await rig.actions.reply(active.id, rig.default.id)
    other_mode = await rig.actions.reply(active.id, rig.flirt.id)
    after_ignore = await rig.actions.reply(ignored.id, rig.default.id)
    missing = await rig.actions.reply(MISSING_ID, rig.default.id)
    retired = await rig.actions.reply(pending.id, rig.retired.id)
    ignore = await rig.actions.ignore(pending.id)

    assert same_mode.outcome is CardOutcome.ALREADY_APPLIED
    assert other_mode.outcome is CardOutcome.REFUSED
    assert after_ignore.outcome is CardOutcome.REFUSED
    assert missing.outcome is CardOutcome.NOT_FOUND
    assert retired.outcome is CardOutcome.MODE_UNAVAILABLE
    assert ignore.outcome is CardOutcome.APPLIED


async def test_ignore_needs_no_gender(
    contacts: SqlContactRepository, modes: SqlModeRepository
) -> None:
    rig = await _rig(contacts, modes, default_gender=None)
    contact = await rig.contact()

    result = await rig.actions.ignore(contact.id)

    assert result.outcome is CardOutcome.APPLIED
    assert (await rig.current(contact)).status is ContactStatus.IGNORED
