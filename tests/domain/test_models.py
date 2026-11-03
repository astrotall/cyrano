from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from cyrano.domain.enums import ContactStatus, MessageDirection
from cyrano.domain.errors import ContactAlreadyExists, ContactNotFound
from cyrano.domain.models import MAX_BIGINT, Contact, ContactCreate, MessageCreate, ModeCreate
from tests.privacy import FAKE_TELEGRAM_ID, assert_absent

NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def _contact(created_at: datetime) -> Contact:
    return Contact(
        id=1,
        tg_id=100,
        username=None,
        display_name=None,
        status=ContactStatus.NEW,
        mode_id=None,
        profile={},
        summary={},
        last_message_at=None,
        created_at=created_at,
        updated_at=NOW,
    )


def test_enum_values_are_the_stored_strings() -> None:
    assert [s.value for s in ContactStatus] == ["new", "pending", "active", "paused", "ignored"]
    assert [d.value for d in MessageDirection] == ["incoming", "outgoing"]


def test_stored_timestamps_are_normalized_to_utc() -> None:
    moscow = timezone(timedelta(hours=3))

    created_at = _contact(NOW.astimezone(moscow)).created_at

    assert created_at == NOW
    assert created_at.utcoffset() == timedelta(0)


def test_naive_timestamp_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _contact(datetime(2026, 1, 2))


def test_stored_models_are_frozen() -> None:
    contact = _contact(NOW)
    field = "status"

    with pytest.raises(ValidationError):
        setattr(contact, field, ContactStatus.ACTIVE)


@pytest.mark.parametrize("tg_id", [0, -1, MAX_BIGINT + 1])
def test_contact_create_rejects_ids_telegram_cannot_produce(tg_id: int) -> None:
    with pytest.raises(ValidationError):
        ContactCreate(tg_id=tg_id)


@pytest.mark.parametrize("tg_message_id", [0, -1, MAX_BIGINT + 1])
def test_message_create_rejects_invalid_telegram_ids(tg_message_id: int) -> None:
    with pytest.raises(ValidationError):
        MessageCreate(
            contact_id=1,
            direction=MessageDirection.INCOMING,
            text="fake",
            tg_message_id=tg_message_id,
        )


def test_message_create_rejects_unknown_direction() -> None:
    with pytest.raises(ValidationError):
        MessageCreate.model_validate(
            {"contact_id": 1, "direction": "sideways", "text": "fake", "tg_message_id": 1}
        )


def test_message_create_accepts_empty_text() -> None:
    message = MessageCreate(
        contact_id=1, direction=MessageDirection.INCOMING, text="", tg_message_id=1
    )

    assert message.text == ""


def test_mode_create_requires_a_name() -> None:
    with pytest.raises(ValidationError):
        ModeCreate(name="", prompt_fragment="fake")


def test_inputs_reject_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        ContactCreate.model_validate({"tg_id": 1, "status": "active"})


def test_error_messages_carry_no_telegram_id() -> None:
    assert_absent(FAKE_TELEGRAM_ID, str(ContactAlreadyExists()))
    assert str(ContactNotFound(7)) == "contact 7 does not exist"
