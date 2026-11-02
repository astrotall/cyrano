from __future__ import annotations

import pytest
from pydantic import ValidationError

from cyrano.domain.enums import CardOutcome, ContactStatus
from cyrano.domain.models import (
    CARD_BIO_LENGTH,
    CARD_MESSAGE_LENGTH,
    CARD_MESSAGES_LIMIT,
    CARD_MODE_NAME_LENGTH,
    CARD_NAME_LENGTH,
    MAX_CARD_PHOTOS,
    CardActionResult,
    CardLeomatch,
    ContactCard,
    ModeChoice,
)


def test_a_minimal_card_needs_only_the_contact_id() -> None:
    card = ContactCard(contact_id=1)

    assert card.messages == []
    assert card.modes == []
    assert card.profile_collected is False
    assert (card.display_name, card.bio, card.leomatch) == (None, None, None)


@pytest.mark.parametrize(
    "fields",
    [
        {"display_name": "x" * (CARD_NAME_LENGTH + 1)},
        {"bio": "x" * (CARD_BIO_LENGTH + 1)},
        {"messages": ["x" * (CARD_MESSAGE_LENGTH + 1)]},
        {"messages": ["x"] * (CARD_MESSAGES_LIMIT + 1)},
        {"modes": [{"id": 1, "name": "x" * (CARD_MODE_NAME_LENGTH + 1)}]},
        {"unknown": 1},
    ],
)
def test_a_card_rejects_texts_beyond_the_named_limits_and_unknown_fields(
    fields: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        ContactCard.model_validate({"contact_id": 1, **fields})


def test_a_leomatch_part_may_have_no_card_text_and_needs_a_sane_age() -> None:
    assert CardLeomatch.model_validate({}).raw_text is None
    with pytest.raises(ValidationError):
        CardLeomatch(raw_text="r", age=100)


def test_a_card_holds_at_most_the_photo_limit_of_card_photos() -> None:
    assert ContactCard(contact_id=1).card_photos == []
    assert len(ContactCard(contact_id=1, card_photos=["p"] * MAX_CARD_PHOTOS).card_photos) == (
        MAX_CARD_PHOTOS
    )
    with pytest.raises(ValidationError):
        ContactCard(contact_id=1, card_photos=["p"] * (MAX_CARD_PHOTOS + 1))


def test_the_card_models_are_frozen() -> None:
    choice = ModeChoice(id=1, name="default")

    with pytest.raises(ValidationError):
        choice.name = "other"  # type: ignore[misc]


def test_the_outcomes_are_the_five_agreed_values() -> None:
    assert {outcome.value for outcome in CardOutcome} == {
        "applied",
        "already_applied",
        "refused",
        "not_found",
        "mode_unavailable",
    }


def test_an_action_result_may_carry_nothing_but_the_outcome() -> None:
    result = CardActionResult(outcome=CardOutcome.NOT_FOUND)

    assert (result.status, result.mode_name) == (None, None)
    assert CardActionResult(outcome=CardOutcome.APPLIED, status=ContactStatus.IGNORED).status
