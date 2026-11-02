from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from cyrano.domain.models import (
    MAX_BIO_LENGTH,
    MAX_LEOMATCH_AGE,
    MAX_LEOMATCH_RELATED_TEXTS,
    MAX_LEOMATCH_TEXT_LENGTH,
    MAX_PHOTO_PATH_LENGTH,
    MIN_LEOMATCH_AGE,
    ContactProfile,
    ContactReadyEvent,
    LeomatchProfile,
)

COLLECTED_AT = datetime(2025, 3, 4, 5, 6, 7, tzinfo=UTC)


def test_an_empty_stored_profile_is_a_valid_profile_with_nothing_collected() -> None:
    profile = ContactProfile.model_validate({})

    assert profile.collected_at is None
    assert profile.leomatch is None
    assert profile.photo is None


def test_the_profile_round_trips_through_json() -> None:
    profile = ContactProfile(
        username="fake_user",
        display_name="Fakefirst Fakelast",
        bio="fake bio",
        photo="profiles/7.jpg",
        leomatch=LeomatchProfile(
            raw_text="Fake, 25, Town - about",
            related_texts=["match"],
            name="Fake",
            age=25,
            photo="profiles/7-card.jpg",
        ),
        collected_at=COLLECTED_AT,
    )

    restored = ContactProfile.model_validate(profile.model_dump(mode="json"))

    assert restored == profile
    assert restored.collected_at == COLLECTED_AT


def test_collected_at_is_normalized_to_utc_and_naive_values_are_rejected() -> None:
    moscow = timezone(timedelta(hours=3))

    profile = ContactProfile(collected_at=datetime(2025, 1, 1, 3, tzinfo=moscow))

    assert profile.collected_at == datetime(2025, 1, 1, tzinfo=UTC)
    assert profile.collected_at is not None
    assert profile.collected_at.utcoffset() == timedelta(0)
    with pytest.raises(ValidationError):
        ContactProfile(collected_at=datetime(2025, 1, 1))


def test_unknown_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        ContactProfile.model_validate({"unexpected": 1})


def test_text_limits_are_enforced() -> None:
    with pytest.raises(ValidationError):
        ContactProfile(bio="x" * (MAX_BIO_LENGTH + 1))
    with pytest.raises(ValidationError):
        LeomatchProfile(raw_text="x" * (MAX_LEOMATCH_TEXT_LENGTH + 1))
    with pytest.raises(ValidationError):
        LeomatchProfile(raw_text="x", related_texts=["y"] * (MAX_LEOMATCH_RELATED_TEXTS + 1))


def test_a_leomatch_profile_without_a_photo_is_valid_and_the_path_is_limited() -> None:
    assert LeomatchProfile(raw_text="x").photo is None
    assert LeomatchProfile.model_validate({"raw_text": "x"}).photo is None
    with pytest.raises(ValidationError):
        LeomatchProfile(raw_text="x", photo="p" * (MAX_PHOTO_PATH_LENGTH + 1))


@pytest.mark.parametrize("age", [MIN_LEOMATCH_AGE - 1, MAX_LEOMATCH_AGE + 1, 0, -5])
def test_the_leomatch_age_must_be_in_range(age: int) -> None:
    with pytest.raises(ValidationError):
        LeomatchProfile(raw_text="x", age=age)


def test_the_ready_event_carries_only_the_contact_id() -> None:
    assert ContactReadyEvent(contact_id=3).model_dump() == {"contact_id": 3}
