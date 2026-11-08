from __future__ import annotations

from dataclasses import dataclass

import pytest

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
    GuardDecision,
)
from cyrano.services.outbox_service import OutboxService
from cyrano.services.send_guard import SendGuard
from tests.privacy import FAKE_INTERNAL_ID, FAKE_TELEGRAM_ID
from tests.services.outbox_fakes import FAKE_NOW, stored_contact

PARTS = ("invented first part", "invented second part")
TRIGGER = 61
EMOJI = "\N{GRINNING FACE}"


@dataclass(frozen=True)
class _Switch:
    autopilot_enabled: bool
    proactive_enabled: bool = True


class _CountingGuard(SendGuard):
    def __init__(self, enabled: bool) -> None:
        super().__init__(_Switch(enabled))
        self.checks = 0

    def check(self, contact: Contact | None, row_source: OutboxSource) -> GuardDecision:
        self.checks += 1
        return super().check(contact, row_source)


async def _active(contacts: SqlContactRepository, modes: SqlModeRepository) -> Contact:
    return await stored_contact(contacts, modes, ContactStatus.ACTIVE, tg_id=FAKE_TELEGRAM_ID)


async def test_a_sequence_is_cleaned_checked_once_and_stored_as_one_chain(
    contacts: SqlContactRepository, modes: SqlModeRepository, outbox: SqlOutboxRepository
) -> None:
    contact = await _active(contacts, modes)
    guard = _CountingGuard(enabled=True)

    rows = await OutboxService(contacts, outbox, guard).enqueue_sequence(
        contact.id,
        ["  invented\x00 first part ", "invented second part\n"],
        FAKE_NOW,
        OutboxSource.AUTOPILOT,
        TRIGGER,
    )

    assert guard.checks == 1
    assert [row.text for row in rows] == list(PARTS)
    assert rows[1].follows_id == rows[0].id
    assert rows[0].trigger_message_id == TRIGGER


@pytest.mark.parametrize(
    ("texts", "reason"),
    [
        ([PARTS[0], "   "], REFUSED_TEXT_EMPTY),
        ([PARTS[0], "\x00"], REFUSED_TEXT_EMPTY),
        (["x" * (MAX_OUTBOX_TEXT_LENGTH + 1), PARTS[1]], REFUSED_TEXT_TOO_LONG),
        ([PARTS[0], EMOJI * (MAX_OUTBOX_TEXT_LENGTH // 2 + 1)], REFUSED_TEXT_TOO_LONG),
        ([], REFUSED_TEXT_EMPTY),
    ],
)
async def test_one_bad_part_refuses_the_whole_sequence_and_stores_nothing(
    contacts: SqlContactRepository,
    modes: SqlModeRepository,
    outbox: SqlOutboxRepository,
    texts: list[str],
    reason: str,
) -> None:
    contact = await _active(contacts, modes)
    guard = _CountingGuard(enabled=True)

    with pytest.raises(EnqueueRefused) as raised:
        await OutboxService(contacts, outbox, guard).enqueue_sequence(
            contact.id, texts, FAKE_NOW, OutboxSource.AUTOPILOT, TRIGGER
        )

    assert raised.value.reason == reason
    assert guard.checks == 0
    assert await outbox.list_rows(None, 10) == []


@pytest.mark.parametrize(
    ("status", "enabled", "reason"),
    [
        (ContactStatus.PAUSED, True, REFUSED_CONTACT_NOT_ACTIVE),
        (ContactStatus.PENDING, True, REFUSED_CONTACT_NOT_ACTIVE),
        (ContactStatus.IGNORED, True, REFUSED_CONTACT_NOT_ACTIVE),
        (ContactStatus.ACTIVE, False, REFUSED_AUTOPILOT_DISABLED),
    ],
)
async def test_a_guard_refusal_stores_no_part(
    contacts: SqlContactRepository,
    modes: SqlModeRepository,
    outbox: SqlOutboxRepository,
    status: ContactStatus,
    enabled: bool,
    reason: str,
) -> None:
    contact = await stored_contact(contacts, modes, status, tg_id=FAKE_TELEGRAM_ID)
    guard = _CountingGuard(enabled=enabled)

    with pytest.raises(EnqueueRefused) as raised:
        await OutboxService(contacts, outbox, guard).enqueue_sequence(
            contact.id, list(PARTS), FAKE_NOW, OutboxSource.AUTOPILOT, TRIGGER
        )

    assert raised.value.reason == reason
    assert guard.checks == 1
    assert await outbox.list_rows(None, 10) == []


async def test_an_unknown_contact_stores_no_part(
    contacts: SqlContactRepository, outbox: SqlOutboxRepository
) -> None:
    with pytest.raises(EnqueueRefused) as raised:
        await OutboxService(contacts, outbox, _CountingGuard(enabled=True)).enqueue_sequence(
            FAKE_INTERNAL_ID, list(PARTS), FAKE_NOW, OutboxSource.AUTOPILOT, TRIGGER
        )

    assert raised.value.reason == REFUSED_UNKNOWN_CONTACT
    assert await outbox.list_rows(None, 10) == []


async def test_the_single_enqueue_keeps_working_for_the_owner_command(
    contacts: SqlContactRepository, modes: SqlModeRepository, outbox: SqlOutboxRepository
) -> None:
    contact = await _active(contacts, modes)

    row = await OutboxService(contacts, outbox, _CountingGuard(enabled=False)).enqueue(
        contact.id, "one\ntwo", FAKE_NOW, OutboxSource.MANUAL_TEST
    )

    assert row.text == "one\ntwo"
    assert row.follows_id is None
    assert len(await outbox.list_rows(None, 10)) == 1
