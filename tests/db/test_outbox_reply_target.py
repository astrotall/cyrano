from __future__ import annotations

from dataclasses import dataclass

import pytest

from cyrano.db.repositories import SqlContactRepository, SqlModeRepository, SqlOutboxRepository
from cyrano.domain.enums import ContactStatus, OutboxSource
from cyrano.domain.errors import EnqueueRefused, OutboxRowInvalid
from cyrano.domain.models import MAX_BIGINT, Contact
from cyrano.domain.outbox import REFUSED_REPLY_TARGET_INVALID, REFUSED_REPLY_TARGETS_MANY
from cyrano.services.outbox_service import OutboxService
from cyrano.services.send_guard import SendGuard
from tests.privacy import FAKE_TELEGRAM_ID
from tests.services.outbox_fakes import FAKE_NOW, stored_contact

PARTS = ("invented part one", "invented part two", "invented part three")
TRIGGER = 71
TARGET = 456000000071


@dataclass(frozen=True)
class _Switch:
    autopilot_enabled: bool = True
    proactive_enabled: bool = True


@pytest.fixture
async def contact(contacts: SqlContactRepository, modes: SqlModeRepository) -> Contact:
    return await stored_contact(contacts, modes, ContactStatus.ACTIVE, tg_id=FAKE_TELEGRAM_ID)


def _service(contacts: SqlContactRepository, outbox: SqlOutboxRepository) -> OutboxService:
    return OutboxService(contacts, outbox, SendGuard(_Switch()))


async def test_a_chain_without_targets_stores_none_on_every_part(
    outbox: SqlOutboxRepository, contact: Contact
) -> None:
    rows = await outbox.enqueue_sequence(
        contact.id, PARTS, FAKE_NOW, OutboxSource.AUTOPILOT, TRIGGER
    )

    assert [row.reply_to_tg_message_id for row in rows] == [None, None, None]


@pytest.mark.parametrize("position", [0, 1, 2])
async def test_the_target_is_stored_on_its_own_part_only(
    outbox: SqlOutboxRepository, contact: Contact, position: int
) -> None:
    targets: list[int | None] = [None, None, None]
    targets[position] = TARGET

    rows = await outbox.enqueue_sequence(
        contact.id, PARTS, FAKE_NOW, OutboxSource.AUTOPILOT, TRIGGER, targets
    )

    assert [row.reply_to_tg_message_id for row in rows] == targets
    stored = [await outbox.get(row.id) for row in rows]
    assert [row.reply_to_tg_message_id for row in stored if row is not None] == targets
    assert [row.follows_id for row in rows] == [None, rows[0].id, rows[1].id]


async def test_targets_that_do_not_align_with_the_texts_are_refused(
    outbox: SqlOutboxRepository, contact: Contact
) -> None:
    with pytest.raises(OutboxRowInvalid):
        await outbox.enqueue_sequence(
            contact.id, PARTS, FAKE_NOW, OutboxSource.AUTOPILOT, TRIGGER, [TARGET]
        )

    assert await outbox.list_rows(None, 10) == []


async def test_a_retried_enqueue_with_targets_returns_the_stored_chain(
    outbox: SqlOutboxRepository, contact: Contact
) -> None:
    first = await outbox.enqueue_sequence(
        contact.id, PARTS[:2], FAKE_NOW, OutboxSource.AUTOPILOT, TRIGGER, [TARGET, None]
    )

    again = await outbox.enqueue_sequence(
        contact.id, PARTS[:2], FAKE_NOW, OutboxSource.AUTOPILOT, TRIGGER, [None, TARGET]
    )

    assert [row.id for row in again] == [row.id for row in first]
    assert [row.reply_to_tg_message_id for row in again] == [TARGET, None]
    assert len(await outbox.list_rows(None, 10)) == 2


async def test_the_single_row_enqueue_never_has_a_target(
    outbox: SqlOutboxRepository, contact: Contact
) -> None:
    row = await outbox.enqueue(contact.id, PARTS[0], FAKE_NOW, OutboxSource.MANUAL_TEST)

    assert row.reply_to_tg_message_id is None


async def test_the_service_passes_one_target_through(
    contacts: SqlContactRepository, outbox: SqlOutboxRepository, contact: Contact
) -> None:
    rows = await _service(contacts, outbox).enqueue_sequence(
        contact.id,
        PARTS[:2],
        FAKE_NOW,
        OutboxSource.AUTOPILOT,
        TRIGGER,
        reply_to=[None, TARGET],
    )

    assert [row.reply_to_tg_message_id for row in rows] == [None, TARGET]


async def test_the_service_without_targets_behaves_as_before(
    contacts: SqlContactRepository, outbox: SqlOutboxRepository, contact: Contact
) -> None:
    rows = await _service(contacts, outbox).enqueue_sequence(
        contact.id, PARTS[:2], FAKE_NOW, OutboxSource.AUTOPILOT, TRIGGER
    )

    assert [row.reply_to_tg_message_id for row in rows] == [None, None]


@pytest.mark.parametrize(
    ("targets", "reason"),
    [
        ([0, None], REFUSED_REPLY_TARGET_INVALID),
        ([-5, None], REFUSED_REPLY_TARGET_INVALID),
        ([MAX_BIGINT + 1, None], REFUSED_REPLY_TARGET_INVALID),
        ([TARGET, TARGET + 1], REFUSED_REPLY_TARGETS_MANY),
        ([TARGET], REFUSED_REPLY_TARGET_INVALID),
        ([None, None, TARGET], REFUSED_REPLY_TARGET_INVALID),
    ],
)
async def test_the_service_refuses_a_bad_target_or_two_targets_and_stores_nothing(
    contacts: SqlContactRepository,
    outbox: SqlOutboxRepository,
    contact: Contact,
    targets: list[int | None],
    reason: str,
) -> None:
    with pytest.raises(EnqueueRefused) as raised:
        await _service(contacts, outbox).enqueue_sequence(
            contact.id, PARTS[:2], FAKE_NOW, OutboxSource.AUTOPILOT, TRIGGER, reply_to=targets
        )

    assert raised.value.reason == reason
    assert str(TARGET) not in str(raised.value)
    assert await outbox.list_rows(None, 10) == []
