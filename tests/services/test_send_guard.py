from __future__ import annotations

from dataclasses import dataclass

import pytest

from cyrano.config import Settings, load_settings
from cyrano.db.repositories import SqlContactRepository, SqlModeRepository, SqlOutboxRepository
from cyrano.domain.enums import ContactStatus, OutboxSource
from cyrano.domain.errors import EnqueueRefused
from cyrano.domain.models import Contact
from cyrano.domain.outbox import (
    MAX_OUTBOX_TEXT_LENGTH,
    REFUSED_AUTOPILOT_DISABLED,
    REFUSED_CONTACT_NOT_ACTIVE,
    REFUSED_TEXT_EMPTY,
    REFUSED_TEXT_TOO_LONG,
    REFUSED_UNKNOWN_CONTACT,
)
from cyrano.services.outbox_service import OutboxService, clean_outbox_text, utf16_units
from cyrano.services.send_guard import GuardSettings, SendGuard, check
from tests.privacy import FAKE_INTERNAL_ID, FAKE_TELEGRAM_ID
from tests.services.outbox_fakes import FAKE_NOW, stored_contact

TEXT = "fake outbox text"
EMOJI = "\N{GRINNING FACE}"
TWO_UNIT_LIMIT = MAX_OUTBOX_TEXT_LENGTH // 2
BLOCKED_STATUSES = [s for s in ContactStatus if s is not ContactStatus.ACTIVE]


@dataclass(frozen=True)
class _Switch:
    autopilot_enabled: bool


def _contact(status: ContactStatus) -> Contact:
    return Contact(
        id=1,
        tg_id=FAKE_TELEGRAM_ID,
        username=None,
        display_name=None,
        status=status,
        pause_reason=None,
        mode_id=None,
        profile={},
        summary={},
        last_message_at=None,
        created_at=FAKE_NOW,
        updated_at=FAKE_NOW,
    )


def test_the_settings_satisfy_what_the_guard_reads(full_env: dict[str, str]) -> None:
    settings: GuardSettings = load_settings(env_file=None)

    assert settings.autopilot_enabled is False


@pytest.mark.parametrize("source", list(OutboxSource))
@pytest.mark.parametrize("switch", [True, False])
@pytest.mark.parametrize("status", BLOCKED_STATUSES)
def test_every_status_but_active_is_refused(
    status: ContactStatus, switch: bool, source: OutboxSource
) -> None:
    decision = check(_contact(status), source, _Switch(switch))

    assert decision.allowed is False
    assert decision.reason == REFUSED_CONTACT_NOT_ACTIVE


@pytest.mark.parametrize("source", list(OutboxSource))
@pytest.mark.parametrize("switch", [True, False])
def test_an_unknown_contact_is_refused(switch: bool, source: OutboxSource) -> None:
    decision = check(None, source, _Switch(switch))

    assert decision.allowed is False
    assert decision.reason == REFUSED_UNKNOWN_CONTACT


@pytest.mark.parametrize(
    ("source", "switch", "allowed", "reason"),
    [
        (OutboxSource.AUTOPILOT, True, True, None),
        (OutboxSource.AUTOPILOT, False, False, REFUSED_AUTOPILOT_DISABLED),
        (OutboxSource.MANUAL_TEST, True, True, None),
        (OutboxSource.MANUAL_TEST, False, True, None),
    ],
)
def test_an_active_contact_follows_the_source_and_the_switch(
    source: OutboxSource, switch: bool, allowed: bool, reason: str | None
) -> None:
    decision = check(_contact(ContactStatus.ACTIVE), source, _Switch(switch))

    assert decision.allowed is allowed
    if reason is not None:
        assert decision.reason == reason


def test_the_thin_service_reads_the_switch_it_was_given() -> None:
    contact = _contact(ContactStatus.ACTIVE)

    assert SendGuard(_Switch(True)).check(contact, OutboxSource.AUTOPILOT).allowed is True
    assert SendGuard(_Switch(False)).check(contact, OutboxSource.AUTOPILOT).allowed is False


@pytest.fixture
def service(contacts: SqlContactRepository, outbox: SqlOutboxRepository) -> OutboxService:
    return OutboxService(contacts, outbox, SendGuard(_Switch(False)))


@pytest.mark.parametrize("status", BLOCKED_STATUSES)
async def test_enqueue_refused_by_the_guard_stores_nothing(
    service: OutboxService,
    contacts: SqlContactRepository,
    modes: SqlModeRepository,
    outbox: SqlOutboxRepository,
    status: ContactStatus,
) -> None:
    contact = await stored_contact(contacts, modes, status, tg_id=FAKE_TELEGRAM_ID)

    with pytest.raises(EnqueueRefused) as raised:
        await service.enqueue(contact.id, TEXT, FAKE_NOW, OutboxSource.MANUAL_TEST)

    assert raised.value.reason == REFUSED_CONTACT_NOT_ACTIVE
    assert await outbox.list_rows(None, 10) == []


async def test_enqueue_for_an_unknown_contact_stores_nothing(
    service: OutboxService, outbox: SqlOutboxRepository
) -> None:
    with pytest.raises(EnqueueRefused) as raised:
        await service.enqueue(FAKE_INTERNAL_ID, TEXT, FAKE_NOW, OutboxSource.MANUAL_TEST)

    assert raised.value.reason == REFUSED_UNKNOWN_CONTACT
    assert await outbox.list_rows(None, 10) == []


async def test_an_autopilot_row_needs_the_switch(
    service: OutboxService,
    contacts: SqlContactRepository,
    modes: SqlModeRepository,
    outbox: SqlOutboxRepository,
) -> None:
    contact = await stored_contact(contacts, modes, ContactStatus.ACTIVE, tg_id=FAKE_TELEGRAM_ID)

    with pytest.raises(EnqueueRefused) as raised:
        await service.enqueue(contact.id, TEXT, FAKE_NOW, OutboxSource.AUTOPILOT)

    assert raised.value.reason == REFUSED_AUTOPILOT_DISABLED
    assert await outbox.list_rows(None, 10) == []


async def test_a_manual_row_for_an_active_contact_is_stored_with_the_switch_off(
    service: OutboxService, contacts: SqlContactRepository, modes: SqlModeRepository
) -> None:
    contact = await stored_contact(contacts, modes, ContactStatus.ACTIVE, tg_id=FAKE_TELEGRAM_ID)

    row = await service.enqueue(contact.id, TEXT, FAKE_NOW, OutboxSource.MANUAL_TEST)

    assert row.source is OutboxSource.MANUAL_TEST


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("", REFUSED_TEXT_EMPTY),
        ("   \n\t ", REFUSED_TEXT_EMPTY),
        ("\x00", REFUSED_TEXT_EMPTY),
        ("\x00 \x00", REFUSED_TEXT_EMPTY),
        ("x" * (MAX_OUTBOX_TEXT_LENGTH + 1), REFUSED_TEXT_TOO_LONG),
    ],
)
async def test_a_bad_text_is_refused_and_stores_nothing(
    service: OutboxService,
    contacts: SqlContactRepository,
    modes: SqlModeRepository,
    outbox: SqlOutboxRepository,
    text: str,
    reason: str,
) -> None:
    contact = await stored_contact(contacts, modes, ContactStatus.ACTIVE, tg_id=FAKE_TELEGRAM_ID)

    with pytest.raises(EnqueueRefused) as raised:
        await service.enqueue(contact.id, text, FAKE_NOW, OutboxSource.MANUAL_TEST)

    assert raised.value.reason == reason
    assert text.strip() == "" or text not in str(raised.value)
    assert await outbox.list_rows(None, 10) == []


@pytest.mark.parametrize(
    ("text", "stored"),
    [
        ("  fake\x00 text \n", "fake text"),
        ("x" * MAX_OUTBOX_TEXT_LENGTH, "x" * MAX_OUTBOX_TEXT_LENGTH),
        (" " + "x" * MAX_OUTBOX_TEXT_LENGTH + " ", "x" * MAX_OUTBOX_TEXT_LENGTH),
    ],
)
async def test_the_text_is_stored_cleaned_up_to_the_limit(
    service: OutboxService,
    contacts: SqlContactRepository,
    modes: SqlModeRepository,
    text: str,
    stored: str,
) -> None:
    contact = await stored_contact(contacts, modes, ContactStatus.ACTIVE, tg_id=FAKE_TELEGRAM_ID)

    row = await service.enqueue(contact.id, text, FAKE_NOW, OutboxSource.MANUAL_TEST)

    assert row.text == stored


@pytest.mark.parametrize(
    ("text", "units"),
    [
        ("x", 1),
        ("\N{CYRILLIC SMALL LETTER YA}", 1),
        (EMOJI, 2),
        (f"x{EMOJI}x", 4),
        ("\N{REGIONAL INDICATOR SYMBOL LETTER R}\N{REGIONAL INDICATOR SYMBOL LETTER U}", 4),
        ("\ud83d", 1),
    ],
)
def test_the_length_is_counted_in_utf16_code_units(text: str, units: int) -> None:
    assert utf16_units(text) == units


@pytest.mark.parametrize(
    "text",
    [
        EMOJI * TWO_UNIT_LIMIT,
        "x" * (MAX_OUTBOX_TEXT_LENGTH - 2) + EMOJI,
        "   " + EMOJI * TWO_UNIT_LIMIT + "\n",
        "\x00" + EMOJI * TWO_UNIT_LIMIT + "\x00",
    ],
)
async def test_a_text_at_the_utf16_limit_is_stored(
    service: OutboxService, contacts: SqlContactRepository, modes: SqlModeRepository, text: str
) -> None:
    contact = await stored_contact(contacts, modes, ContactStatus.ACTIVE, tg_id=FAKE_TELEGRAM_ID)

    row = await service.enqueue(contact.id, text, FAKE_NOW, OutboxSource.MANUAL_TEST)

    assert row.text == text.replace("\x00", "").strip()
    assert utf16_units(row.text) == MAX_OUTBOX_TEXT_LENGTH


@pytest.mark.parametrize(
    "text",
    [
        EMOJI * (TWO_UNIT_LIMIT + 1),
        "x" * (MAX_OUTBOX_TEXT_LENGTH - 1) + EMOJI,
        EMOJI * TWO_UNIT_LIMIT + "x",
    ],
)
async def test_a_text_over_the_utf16_limit_is_refused_and_stores_nothing(
    service: OutboxService,
    contacts: SqlContactRepository,
    modes: SqlModeRepository,
    outbox: SqlOutboxRepository,
    text: str,
) -> None:
    contact = await stored_contact(contacts, modes, ContactStatus.ACTIVE, tg_id=FAKE_TELEGRAM_ID)
    assert len(text) <= MAX_OUTBOX_TEXT_LENGTH

    with pytest.raises(EnqueueRefused) as raised:
        await service.enqueue(contact.id, text, FAKE_NOW, OutboxSource.MANUAL_TEST)

    assert raised.value.reason == REFUSED_TEXT_TOO_LONG
    assert await outbox.list_rows(None, 10) == []


def test_the_limit_is_checked_after_the_cleaning() -> None:
    assert clean_outbox_text(" \x00" + EMOJI * TWO_UNIT_LIMIT + " ") == EMOJI * TWO_UNIT_LIMIT


def test_autopilot_enabled_defaults_to_false(full_env: dict[str, str]) -> None:
    assert Settings(_env_file=None).autopilot_enabled is False
