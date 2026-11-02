from __future__ import annotations

import time

import pytest

from cyrano.domain.enums import LeomatchTextKind
from cyrano.domain.models import (
    MAX_LEOMATCH_NAME_LENGTH,
    MAX_LEOMATCH_RELATED_TEXTS,
    MAX_LEOMATCH_TEXT_LENGTH,
    LeomatchSourceText,
)
from cyrano.services.leomatch import (
    NO_FIELDS,
    LeomatchFields,
    build_leomatch_profile,
    parse_leomatch_text,
)

HYPHEN = "-"
EN_DASH = "\N{EN DASH}"
EM_DASH = "\N{EM DASH}"
CARD = "Fakename, 25, Faketown - fake description"
MATCH = "You have a match with Fakename! t.me/fake_user"


def test_a_typical_card_fills_every_field() -> None:
    fields = parse_leomatch_text("Fakename, 25, Faketown - loves fake things")

    assert fields == LeomatchFields(
        name="Fakename", age=25, city="Faketown", about="loves fake things"
    )
    assert fields.matched


def test_a_card_without_a_city_fills_the_rest() -> None:
    fields = parse_leomatch_text("Fakename, 31 - loves fake things")

    assert fields == LeomatchFields(name="Fakename", age=31, city=None, about="loves fake things")


@pytest.mark.parametrize("dash", [HYPHEN, EN_DASH, EM_DASH])
def test_hyphen_and_dash_variants_are_accepted(dash: str) -> None:
    fields = parse_leomatch_text(f"Fakename, 25, Faketown {dash} about me")

    assert fields.name == "Fakename"
    assert fields.about == "about me"


@pytest.mark.parametrize(
    "name", ["Fakename \U0001f338", "\U0001f525 Fakename", "Fake \U0001f468‍\U0001f469"]
)
def test_an_emoji_in_the_name_is_kept(name: str) -> None:
    fields = parse_leomatch_text(f"{name}, 25, Faketown - about")

    assert fields.name == name


def test_a_city_with_a_hyphen_inside_is_not_split() -> None:
    fields = parse_leomatch_text("Fakename, 25, Fake-Town - about")

    assert fields.city == "Fake-Town"


def test_a_multiline_description_is_kept() -> None:
    fields = parse_leomatch_text("Fakename, 25, Faketown - first line\nsecond line")

    assert fields.about == "first line\nsecond line"


@pytest.mark.parametrize("age", [18, 99])
def test_ages_on_the_boundary_are_accepted(age: int) -> None:
    assert parse_leomatch_text(f"Fakename, {age}, Faketown - about").age == age


@pytest.mark.parametrize("age", ["17", "100", "00", "05", "7", "123", "-25", "25.5", "xx", ""])
def test_an_age_out_of_range_or_malformed_is_not_a_card(age: str) -> None:
    assert parse_leomatch_text(f"Fakename, {age}, Faketown - about") == NO_FIELDS


@pytest.mark.parametrize(
    "text",
    [
        "just a plain sentence",
        "Fakename, 25, Faketown",
        "Fakename, 25, Faketown -",
        "Fakename, 25, Faketown - ",
        "Fakename, 25-Faketown about",
        "Fakename 25, Faketown - about",
        "Fakename, 25, Fake, Town - about",
        ", 25, Faketown - about",
        "Fakename, 25, - about",
        "first line\nFakename, 25, Faketown - about",
        "Match - Fakename, 25, Faketown - about",
        "Fakename,25,Faketown-about",
    ],
)
def test_text_that_is_not_a_card_fills_nothing(text: str) -> None:
    fields = parse_leomatch_text(text)

    assert fields == NO_FIELDS
    assert not fields.matched


@pytest.mark.parametrize("text", ["", "   ", "\n\n", "\x00", " \x00 \x00"])
def test_empty_text_fills_nothing(text: str) -> None:
    assert parse_leomatch_text(text) == NO_FIELDS


def test_nul_bytes_are_removed_before_parsing() -> None:
    fields = parse_leomatch_text("Fake\x00name, 2\x005, Fake\x00town - ab\x00out")

    assert fields == LeomatchFields(name="Fakename", age=25, city="Faketown", about="about")


def test_a_name_over_the_limit_is_not_a_card() -> None:
    name = "n" * (MAX_LEOMATCH_NAME_LENGTH + 1)

    assert parse_leomatch_text(f"{name}, 25, Faketown - about") == NO_FIELDS


def test_a_huge_text_is_truncated_and_parsed_quickly() -> None:
    text = "Fakename, 25, Faketown - " + "x" * (MAX_LEOMATCH_TEXT_LENGTH * 20)

    fields = parse_leomatch_text(text)

    assert fields.name == "Fakename"
    assert fields.about is not None
    assert len(fields.about) <= MAX_LEOMATCH_TEXT_LENGTH


@pytest.mark.parametrize(
    "text",
    [
        "," * 200_000,
        " - " * 100_000,
        "a, " * 100_000,
        "(" * 100_000 + " - " + ")" * 100_000,
        "\\" * 100_000,
        " " * 100_000 + "-" + " " * 100_000,
        "Fakename, " + "9" * 100_000 + " - about",
    ],
)
def test_text_that_would_break_a_naive_regex_is_handled_in_linear_time(text: str) -> None:
    started = time.perf_counter()

    fields = parse_leomatch_text(text)

    assert time.perf_counter() - started < 1.0
    assert fields == NO_FIELDS


def _texts(card: str | None, match: str | None) -> list[LeomatchSourceText]:
    texts: list[LeomatchSourceText] = []
    if match is not None:
        texts.append(LeomatchSourceText(kind=LeomatchTextKind.MATCH, text=match))
    if card is not None:
        texts.append(LeomatchSourceText(kind=LeomatchTextKind.CARD, text=card))
    return texts


def test_no_texts_build_no_profile() -> None:
    assert build_leomatch_profile([]) is None
    assert build_leomatch_profile(_texts("  ", "\x00")) is None


def test_the_card_is_the_raw_text_and_the_match_is_related() -> None:
    profile = build_leomatch_profile(_texts(CARD, MATCH))

    assert profile is not None
    assert profile.raw_text == CARD
    assert profile.related_texts == [MATCH]
    assert (profile.name, profile.age, profile.city) == ("Fakename", 25, "Faketown")
    assert profile.about == "fake description"


def test_without_a_card_the_match_is_the_raw_text() -> None:
    profile = build_leomatch_profile(_texts(None, MATCH))

    assert profile is not None
    assert profile.raw_text == MATCH
    assert profile.related_texts == []
    assert profile.name is None


def test_fields_come_from_the_match_text_when_the_card_does_not_parse() -> None:
    parseable_match = "Matchname, 40, Matchtown - from the match"

    profile = build_leomatch_profile(_texts("a card that is no card", parseable_match))

    assert profile is not None
    assert profile.raw_text == "a card that is no card"
    assert profile.related_texts == [parseable_match]
    assert (profile.name, profile.age, profile.city) == ("Matchname", 40, "Matchtown")


def test_fields_come_from_the_first_text_that_parses() -> None:
    profile = build_leomatch_profile(_texts("Cardname, 22 - card", "Matchname, 40 - match"))

    assert profile is not None
    assert profile.name == "Cardname"
    assert profile.age == 22


def test_the_raw_text_is_kept_when_nothing_parses() -> None:
    profile = build_leomatch_profile(_texts("free text card", "free text match"))

    assert profile is not None
    assert profile.raw_text == "free text card"
    assert (profile.name, profile.age, profile.city, profile.about) == (None, None, None, None)


def test_stored_texts_are_cleaned_and_truncated() -> None:
    long_match = "m" * (MAX_LEOMATCH_TEXT_LENGTH * 2)

    profile = build_leomatch_profile(_texts("  ca\x00rd  ", long_match))

    assert profile is not None
    assert profile.raw_text == "card"
    assert profile.related_texts == ["m" * MAX_LEOMATCH_TEXT_LENGTH]


def test_the_related_texts_never_exceed_the_limit() -> None:
    profile = build_leomatch_profile(_texts(CARD, MATCH))

    assert profile is not None
    assert len(profile.related_texts) <= MAX_LEOMATCH_RELATED_TEXTS
