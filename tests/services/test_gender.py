from __future__ import annotations

from datetime import UTC, datetime

import pytest

from cyrano.domain.enums import ContactGender, GenderSource
from cyrano.domain.gender import UNRESOLVED_GENDER, ResolvedGender
from cyrano.domain.models import Contact
from cyrano.domain.summary import SUMMARY_VERSION, ContactSummary, StoredSummary, SummaryCursor
from cyrano.services.gender import GenderResolver, contact_name_for_guess
from tests.reply_data import make_contact

FEMALE = ContactGender.FEMALE
MALE = ContactGender.MALE
NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
FEMALE_NAME = "Анна"
MALE_NAME = "Иван"
UNKNOWN_NAME = "Alex"


def _summary(**fields: object) -> dict[str, object]:
    stored = StoredSummary(
        version=SUMMARY_VERSION,
        updated_at=NOW,
        cursor=SummaryCursor(created_at=NOW, message_id=5),
        data=ContactSummary.model_validate(fields),
    )
    return stored.model_dump(mode="json")


def _contact(
    *,
    name: str | None = UNKNOWN_NAME,
    gender: ContactGender | None = None,
    summary: dict[str, object] | None = None,
    profile: dict[str, object] | None = None,
) -> Contact:
    return make_contact(
        profile if profile is not None else {},
        display_name=name,
        gender=gender,
        summary=summary if summary is not None else {},
    )


def _resolve(contact: Contact, default: ContactGender | None = None) -> ResolvedGender:
    return GenderResolver(default).resolve(contact)


def test_the_owner_choice_is_the_first_source() -> None:
    resolved = _resolve(_contact(gender=MALE))

    assert resolved == ResolvedGender(value=MALE, source=GenderSource.OWNER)
    assert resolved.is_known
    assert resolved.label == "male"


def test_the_summary_is_the_second_source() -> None:
    resolved = _resolve(_contact(summary=_summary(gender="female")))

    assert resolved == ResolvedGender(value=FEMALE, source=GenderSource.SUMMARY)


def test_the_name_is_the_third_source() -> None:
    resolved = _resolve(_contact(name=FEMALE_NAME))

    assert resolved == ResolvedGender(value=FEMALE, source=GenderSource.NAME)


def test_the_default_setting_is_the_fourth_source() -> None:
    resolved = _resolve(_contact(), MALE)

    assert resolved == ResolvedGender(value=MALE, source=GenderSource.DEFAULT)


def test_nothing_known_is_unknown_with_the_unknown_source() -> None:
    resolved = _resolve(_contact())

    assert resolved == UNRESOLVED_GENDER
    assert resolved.value is None
    assert resolved.source is GenderSource.UNKNOWN
    assert not resolved.is_known
    assert resolved.label == "unknown"


def test_the_owner_beats_the_summary_the_name_and_the_default() -> None:
    contact = _contact(name=FEMALE_NAME, gender=MALE, summary=_summary(gender="female"))

    assert _resolve(contact, FEMALE) == ResolvedGender(value=MALE, source=GenderSource.OWNER)


def test_the_summary_beats_the_name_and_the_default() -> None:
    contact = _contact(name=FEMALE_NAME, summary=_summary(gender="male"))

    assert _resolve(contact, FEMALE) == ResolvedGender(value=MALE, source=GenderSource.SUMMARY)


def test_the_name_beats_the_default() -> None:
    contact = _contact(name=MALE_NAME)

    assert _resolve(contact, FEMALE) == ResolvedGender(value=MALE, source=GenderSource.NAME)


def test_an_ambiguous_name_falls_through_to_the_default() -> None:
    contact = _contact(name="Саша")

    assert _resolve(contact, FEMALE) == ResolvedGender(value=FEMALE, source=GenderSource.DEFAULT)
    assert _resolve(contact) == UNRESOLVED_GENDER


@pytest.mark.parametrize(
    "stored",
    [
        {"version": 2, "data": {"gender": "female"}},
        {"garbage": True},
        {"version": SUMMARY_VERSION, "updated_at": "not a time", "cursor": {}, "data": {}},
        {"version": SUMMARY_VERSION},
        {},
    ],
)
def test_an_unreadable_stored_summary_is_ignored(stored: dict[str, object]) -> None:
    resolved = _resolve(_contact(name=FEMALE_NAME, summary=stored))

    assert resolved == ResolvedGender(value=FEMALE, source=GenderSource.NAME)


def test_a_stored_summary_without_the_gender_field_is_read_as_none() -> None:
    stored = _summary(name="Оля")
    del stored["data"]["gender"]  # type: ignore[attr-defined]

    assert _resolve(_contact(name=MALE_NAME, summary=stored)) == ResolvedGender(
        value=MALE, source=GenderSource.NAME
    )


def test_a_summary_with_an_invalid_gender_value_does_not_decide() -> None:
    stored = _summary(gender="other")

    assert _resolve(_contact(name=FEMALE_NAME, summary=stored)).source is GenderSource.NAME


def test_the_card_name_comes_first_then_the_match_name_then_the_display_name() -> None:
    card: dict[str, object] = {"leomatch": {"name": MALE_NAME, "name_source": "card"}}
    match: dict[str, object] = {"leomatch": {"name": MALE_NAME, "name_source": "match"}}
    plain: dict[str, object] = {"display_name": MALE_NAME}

    assert contact_name_for_guess(_contact(name=FEMALE_NAME, profile=card)) == MALE_NAME
    assert contact_name_for_guess(_contact(name=FEMALE_NAME, profile=match)) == MALE_NAME
    assert contact_name_for_guess(_contact(name=FEMALE_NAME, profile=plain)) == FEMALE_NAME
    assert contact_name_for_guess(_contact(name=None, profile=plain)) == MALE_NAME
    assert contact_name_for_guess(_contact(name=None)) is None


def test_the_card_name_decides_before_the_display_name() -> None:
    contact = _contact(
        name=FEMALE_NAME, profile={"leomatch": {"name": MALE_NAME, "name_source": "card"}}
    )

    assert _resolve(contact) == ResolvedGender(value=MALE, source=GenderSource.NAME)


def test_the_match_fallback_name_decides_before_the_display_name() -> None:
    contact = _contact(
        name=MALE_NAME, profile={"leomatch": {"name": "Мария", "name_source": "match"}}
    )

    assert _resolve(contact) == ResolvedGender(value=FEMALE, source=GenderSource.NAME)


def test_only_the_first_word_of_the_display_name_counts() -> None:
    assert _resolve(_contact(name="Анна Иванов")).value is FEMALE
    assert _resolve(_contact(name="Иван Анна")).value is MALE


def test_an_unreadable_profile_does_not_break_the_resolution() -> None:
    contact = _contact(name=FEMALE_NAME, profile={"leomatch": "not an object", "age": [1]})

    assert _resolve(contact) == ResolvedGender(value=FEMALE, source=GenderSource.NAME)


def test_resolution_needs_no_database_and_has_no_side_effects() -> None:
    contact = _contact(name=FEMALE_NAME)

    first = _resolve(contact)
    second = _resolve(contact)

    assert first == second
    assert contact == _contact(name=FEMALE_NAME)


def test_the_resolved_gender_model_is_frozen_and_strict() -> None:
    resolved = ResolvedGender(value=FEMALE, source=GenderSource.NAME)

    with pytest.raises(ValueError, match="frozen"):
        resolved.value = MALE  # type: ignore[misc]
    with pytest.raises(ValueError, match="Extra"):
        ResolvedGender.model_validate({"value": "female", "source": "owner", "extra": 1})
