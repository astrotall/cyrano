from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from cyrano.domain.enums import LeomatchNameSource
from cyrano.domain.models import (
    MAX_BIO_LENGTH,
    MAX_CARD_PHOTOS,
    MAX_LEOMATCH_AGE,
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
            match_text="match",
            name="Fake",
            name_source=LeomatchNameSource.CARD,
            age=25,
            photos=["profiles/7-card-1.jpg", "profiles/7-card-2.jpg"],
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
        LeomatchProfile(match_text="x" * (MAX_LEOMATCH_TEXT_LENGTH + 1))


def test_a_leomatch_profile_needs_no_text_and_no_photos() -> None:
    profile = LeomatchProfile.model_validate({})

    assert (profile.raw_text, profile.match_text, profile.name, profile.name_source) == (
        None,
        None,
        None,
        None,
    )
    assert profile.photos == []


def test_the_match_text_is_kept_apart_from_the_card_text() -> None:
    profile = LeomatchProfile(raw_text=None, match_text="match only")

    assert profile.raw_text is None
    assert profile.match_text == "match only"


def test_the_photo_list_and_its_paths_are_limited() -> None:
    assert len(LeomatchProfile(photos=["p"] * MAX_CARD_PHOTOS).photos) == MAX_CARD_PHOTOS
    with pytest.raises(ValidationError):
        LeomatchProfile(photos=["p"] * (MAX_CARD_PHOTOS + 1))
    with pytest.raises(ValidationError):
        LeomatchProfile(photos=["p" * (MAX_PHOTO_PATH_LENGTH + 1)])


def test_the_legacy_single_photo_key_becomes_a_one_item_list() -> None:
    profile = LeomatchProfile.model_validate({"raw_text": "x", "photo": "profiles/7-card.jpg"})

    assert profile.photos == ["profiles/7-card.jpg"]
    assert "photo" not in profile.model_dump()


def test_a_legacy_photo_key_without_a_path_gives_no_photos() -> None:
    assert LeomatchProfile.model_validate({"raw_text": "x", "photo": None}).photos == []
    assert LeomatchProfile.model_validate({"raw_text": "x", "photo": 5}).photos == []


def test_the_new_photos_key_wins_over_the_legacy_one() -> None:
    profile = LeomatchProfile.model_validate({"photo": "old.jpg", "photos": ["new.jpg"]})

    assert profile.photos == ["new.jpg"]


def test_a_legacy_row_with_related_texts_still_validates() -> None:
    legacy = {
        "raw_text": "Fake, 25, Town - about",
        "related_texts": ["match"],
        "name": "Fake",
        "age": 25,
        "city": "Town",
        "about": "about",
        "photo": "profiles/7-card.jpg",
    }

    profile = ContactProfile.model_validate({"photo": "profiles/7.jpg", "leomatch": legacy})

    assert profile.leomatch is not None
    assert profile.leomatch.raw_text == "Fake, 25, Town - about"
    assert profile.leomatch.match_text is None
    assert profile.leomatch.photos == ["profiles/7-card.jpg"]
    assert profile.photo == "profiles/7.jpg"


def test_a_legacy_parsed_name_counts_as_a_card_name() -> None:
    profile = LeomatchProfile.model_validate({"raw_text": "x", "name": "Fake"})

    assert profile.name_source is LeomatchNameSource.CARD


def test_a_legacy_row_without_a_name_has_no_name_source() -> None:
    assert LeomatchProfile.model_validate({"raw_text": "x"}).name_source is None


def test_an_explicit_name_source_is_kept() -> None:
    profile = LeomatchProfile.model_validate({"name": "Fake", "name_source": "match"})

    assert profile.name_source is LeomatchNameSource.MATCH


def test_unknown_leomatch_keys_are_still_rejected() -> None:
    with pytest.raises(ValidationError):
        LeomatchProfile.model_validate({"unexpected": 1})


@pytest.mark.parametrize("age", [MIN_LEOMATCH_AGE - 1, MAX_LEOMATCH_AGE + 1, 0, -5])
def test_the_leomatch_age_must_be_in_range(age: int) -> None:
    with pytest.raises(ValidationError):
        LeomatchProfile(raw_text="x", age=age)


def test_the_ready_event_carries_only_the_contact_id() -> None:
    assert ContactReadyEvent(contact_id=3).model_dump() == {"contact_id": 3}
