from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from cyrano.domain.summary import (
    SUMMARY_AGREEMENTS_MAX,
    SUMMARY_INTERESTS_MAX,
    SUMMARY_ITEM_MAX_CHARS,
    SUMMARY_NOTES_MAX,
    SUMMARY_OPEN_QUESTIONS_MAX,
    SUMMARY_TEXT_MAX_CHARS,
    SUMMARY_TOLD_ABOUT_OWNER_MAX,
    SUMMARY_VERSION,
    ContactSummary,
    StoredSummary,
    SummaryCursor,
    read_stored_summary,
    summary_job_id,
)

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
LIST_CAPS = {
    "interests": SUMMARY_INTERESTS_MAX,
    "agreements_and_plans": SUMMARY_AGREEMENTS_MAX,
    "open_questions": SUMMARY_OPEN_QUESTIONS_MAX,
    "told_about_owner": SUMMARY_TOLD_ABOUT_OWNER_MAX,
    "notes": SUMMARY_NOTES_MAX,
}


def test_the_caps_have_their_documented_values() -> None:
    assert SUMMARY_TEXT_MAX_CHARS == 200
    assert SUMMARY_ITEM_MAX_CHARS == 120
    assert LIST_CAPS == {
        "interests": 10,
        "agreements_and_plans": 8,
        "open_questions": 5,
        "told_about_owner": 10,
        "notes": 8,
    }


def test_an_empty_object_is_an_empty_summary() -> None:
    summary = ContactSummary.model_validate({})

    assert summary.is_empty()
    assert summary.interests == []
    assert summary.name is None


def test_texts_are_trimmed_collapsed_cut_and_blank_becomes_none() -> None:
    summary = ContactSummary.model_validate(
        {
            "name": "  Аня \n  ",
            "city": "\t  ",
            "occupation": "а" * (SUMMARY_TEXT_MAX_CHARS + 50),
            "mood_and_tone": "весёлое\x00 и   лёгкое",
        }
    )

    assert summary.name == "Аня"
    assert summary.city is None
    assert summary.occupation == "а" * SUMMARY_TEXT_MAX_CHARS
    assert summary.mood_and_tone == "весёлое и лёгкое"


@pytest.mark.parametrize(("field", "cap"), list(LIST_CAPS.items()))
def test_every_list_drops_empty_and_repeated_items_cuts_them_and_keeps_its_cap(
    field: str, cap: int
) -> None:
    items = ["", "   ", "один", "один", "x" * (SUMMARY_ITEM_MAX_CHARS + 9)]
    items += [f"пункт {number}" for number in range(cap + 5)]

    values: list[str] = getattr(ContactSummary.model_validate({field: items}), field)

    assert values[0] == "один"
    assert values[1] == "x" * SUMMARY_ITEM_MAX_CHARS
    assert len(values) == cap
    assert all(value.strip() and len(value) <= SUMMARY_ITEM_MAX_CHARS for value in values)


def test_a_single_string_becomes_a_list_and_other_types_are_dropped() -> None:
    summary = ContactSummary.model_validate(
        {"interests": "велосипед", "notes": {"a": 1}, "open_questions": [3, None, "вопрос"]}
    )

    assert summary.interests == ["велосипед"]
    assert summary.notes == []
    assert summary.open_questions == ["вопрос"]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (10, 10),
        (99, 99),
        (9, None),
        (100, None),
        ("27", 27),
        (" 31 ", 31),
        (True, None),
        (25.5, None),
        ("двадцать", None),
        (None, None),
    ],
)
def test_the_age_is_kept_only_between_10_and_99(value: object, expected: int | None) -> None:
    assert ContactSummary.model_validate({"age": value}).age == expected


def test_extra_keys_are_ignored() -> None:
    summary = ContactSummary.model_validate({"name": "Оля", "favourite_colour": "синий"})

    assert summary.name == "Оля"
    assert "favourite_colour" not in summary.model_dump()


def test_the_json_schema_names_the_caps_for_the_model() -> None:
    schema = ContactSummary.model_json_schema()
    properties = schema["properties"]

    assert properties["name"]["maxLength"] == SUMMARY_TEXT_MAX_CHARS
    assert properties["told_about_owner"]["maxItems"] == SUMMARY_TOLD_ABOUT_OWNER_MAX
    assert properties["interests"]["items"]["maxLength"] == SUMMARY_ITEM_MAX_CHARS
    assert properties["age"]["minimum"] == 10
    assert "description" not in schema


def _stored() -> StoredSummary:
    return StoredSummary(
        version=SUMMARY_VERSION,
        updated_at=NOW,
        cursor=SummaryCursor(created_at=NOW, message_id=41),
        data=ContactSummary(name="Оля", told_about_owner=["люблю походы"]),
    )


def test_the_stored_shape_round_trips_through_json() -> None:
    stored = _stored()

    assert read_stored_summary(stored.model_dump(mode="json")) == stored


@pytest.mark.parametrize(
    "raw",
    [
        None,
        {},
        {"topic": "old shape"},
        {"version": 2, "updated_at": NOW.isoformat(), "cursor": {}, "data": {}},
        {"version": 1, "updated_at": "not a time", "cursor": {}, "data": {}},
        {
            "version": 1,
            "updated_at": "2026-11-08T12:00:00",
            "cursor": {"created_at": NOW.isoformat(), "message_id": 1},
            "data": {},
        },
        {
            "version": 1,
            "updated_at": NOW.isoformat(),
            "cursor": {"created_at": NOW.isoformat(), "message_id": 0},
            "data": {},
        },
        {
            "version": 1,
            "updated_at": NOW.isoformat(),
            "cursor": {"created_at": NOW.isoformat(), "message_id": 3},
            "data": "garbage",
        },
    ],
)
def test_an_unreadable_or_old_stored_shape_is_no_summary(raw: dict[str, object] | None) -> None:
    assert read_stored_summary(raw) is None


def test_extra_keys_in_the_stored_shape_are_ignored() -> None:
    raw = {**_stored().model_dump(mode="json"), "later_key": [1, 2]}

    assert read_stored_summary(raw) == _stored()


def test_the_cursor_refuses_a_naive_time_and_a_non_positive_id() -> None:
    with pytest.raises(ValidationError):
        SummaryCursor(created_at=NOW.replace(tzinfo=None), message_id=3)
    with pytest.raises(ValidationError):
        SummaryCursor(created_at=NOW, message_id=0)


def test_the_job_id_is_deterministic() -> None:
    assert summary_job_id(7, 41) == "summary-7-41"
