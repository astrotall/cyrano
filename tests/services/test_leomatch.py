from __future__ import annotations

import time

import pytest

from cyrano.domain.enums import LeomatchNameSource
from cyrano.domain.models import MAX_LEOMATCH_NAME_LENGTH, MAX_LEOMATCH_TEXT_LENGTH
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
OBSERVED_HEADER = "Кому-то понравилась твоя анкета:"
OBSERVED_CARD = f"{OBSERVED_HEADER}\n\nАня, 18, Москва"
OBSERVED_MATCH = "Отлично! Надеюсь хорошо проведете время 🙌\n\nНачинай общаться 👉 Аня"


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


def test_the_observed_card_gives_name_age_and_city_and_no_about() -> None:
    fields = parse_leomatch_text(OBSERVED_CARD)

    assert fields == LeomatchFields(name="Аня", age=18, city="Москва", about=None)
    assert fields.matched


def test_the_observed_card_with_a_description_fills_about() -> None:
    fields = parse_leomatch_text(f"{OBSERVED_HEADER}\n\nАня, 18, Москва – люблю кофе")

    assert fields == LeomatchFields(name="Аня", age=18, city="Москва", about="люблю кофе")


def test_a_card_without_a_description_has_no_about() -> None:
    assert parse_leomatch_text("Fakename, 25, Faketown").about is None
    assert parse_leomatch_text("Fakename, 25").about is None


def test_a_card_with_only_name_and_age_is_accepted() -> None:
    assert parse_leomatch_text("Fakename, 25") == LeomatchFields(
        name="Fakename", age=25, city=None, about=None
    )


def test_a_card_without_a_header_is_accepted() -> None:
    assert parse_leomatch_text("Аня, 18, Москва") == LeomatchFields(
        name="Аня", age=18, city="Москва", about=None
    )


@pytest.mark.parametrize(
    "header",
    [
        "Someone liked your profile:",
        "Ты кому-то понравился(ась):",
        "A header\nover two lines:",
        "x:",
    ],
)
def test_any_header_ending_with_a_colon_is_skipped(header: str) -> None:
    fields = parse_leomatch_text(f"{header}\n\nАня, 18, Москва")

    assert fields == LeomatchFields(name="Аня", age=18, city="Москва", about=None)


def test_extra_blank_lines_around_the_header_are_tolerated() -> None:
    fields = parse_leomatch_text(f"{OBSERVED_HEADER}\n \n\n\nАня, 18, Москва\n\n")

    assert fields.name == "Аня"


@pytest.mark.parametrize("city", ["Санкт-Петербург", "Нью-Йорк", "Ростов-на-Дону"])
def test_a_city_with_a_hyphen_is_not_split_without_a_description(city: str) -> None:
    fields = parse_leomatch_text(f"{OBSERVED_HEADER}\n\nАня, 18, {city}")

    assert fields.city == city
    assert fields.about is None


def test_a_hyphenated_city_with_a_description_is_split_only_at_the_spaced_dash() -> None:
    fields = parse_leomatch_text("Аня, 18, Санкт-Петербург — люблю кофе")

    assert (fields.city, fields.about) == ("Санкт-Петербург", "люблю кофе")


def test_a_header_with_no_card_after_it_fills_nothing() -> None:
    assert parse_leomatch_text(OBSERVED_HEADER) == NO_FIELDS
    assert parse_leomatch_text(f"{OBSERVED_HEADER}\n\n \n") == NO_FIELDS


def test_a_first_paragraph_without_a_colon_is_not_a_header() -> None:
    assert parse_leomatch_text("Кому-то понравилась твоя анкета\n\nАня, 18, Москва") == NO_FIELDS


def test_a_header_followed_by_text_that_is_no_card_fills_nothing() -> None:
    assert parse_leomatch_text(f"{OBSERVED_HEADER}\n\njust some words") == NO_FIELDS


def test_the_observed_match_text_fills_nothing() -> None:
    assert parse_leomatch_text(OBSERVED_MATCH) == NO_FIELDS


@pytest.mark.parametrize(
    "text",
    [
        "Аня, Оля, 18, Москва",
        "Аня, Оля, 18",
        "Аня, 18, Оля, 20",
        f"{OBSERVED_HEADER}\n\nАня, Оля, 18, Москва",
    ],
)
def test_two_names_separated_by_a_comma_are_not_a_card(text: str) -> None:
    assert parse_leomatch_text(text) == NO_FIELDS


def test_a_card_followed_by_more_paragraphs_keeps_only_the_raw_text() -> None:
    assert parse_leomatch_text(f"{OBSERVED_CARD}\n\nanother paragraph") == NO_FIELDS


@pytest.mark.parametrize("age", [18, 99])
def test_ages_on_the_boundary_are_accepted(age: int) -> None:
    assert parse_leomatch_text(f"Fakename, {age}, Faketown - about").age == age


@pytest.mark.parametrize("age", ["12", "100", "00", "05", "7", "123", "-25", "25.5", "xx", ""])
def test_an_age_out_of_range_or_malformed_is_not_a_card(age: str) -> None:
    assert parse_leomatch_text(f"Fakename, {age}, Faketown - about") == NO_FIELDS


@pytest.mark.parametrize(
    "text",
    [
        "just a plain sentence",
        "Fakename, 25, Faketown -",
        "Fakename, 25, Faketown - ",
        "Fakename, 25-Faketown about",
        "Fakename 25, Faketown - about",
        "Fakename, 25, Fake, Town - about",
        ", 25, Faketown - about",
        "Fakename, 25, - about",
        "first line\nFakename, 25, Faketown - about",
        "Match - Fakename, 25, Faketown - about",
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
        "x:\n\n" * 100_000,
        ":\n" * 100_000,
        "\n \n" * 100_000 + "x",
        OBSERVED_HEADER + "\n\n" + "," * 100_000,
        "a:" + "\n\n" * 100_000 + "-",
    ],
)
def test_text_that_would_break_a_naive_regex_is_handled_in_linear_time(text: str) -> None:
    started = time.perf_counter()

    fields = parse_leomatch_text(text)

    assert time.perf_counter() - started < 1.0
    assert fields == NO_FIELDS


def test_no_texts_build_no_profile() -> None:
    assert build_leomatch_profile(None, None, None) is None
    assert build_leomatch_profile("  ", "\x00", "Linkname") is None


def test_the_observed_chain_stores_the_card_as_raw_text_and_the_match_apart() -> None:
    profile = build_leomatch_profile(OBSERVED_CARD, OBSERVED_MATCH, "Аня")

    assert profile is not None
    assert profile.raw_text == OBSERVED_CARD
    assert profile.match_text == OBSERVED_MATCH
    assert (profile.name, profile.age, profile.city, profile.about) == ("Аня", 18, "Москва", None)
    assert profile.name_source is LeomatchNameSource.CARD
    assert profile.photos == []


def test_the_card_is_the_raw_text_and_the_match_is_the_match_text() -> None:
    profile = build_leomatch_profile(CARD, MATCH, None)

    assert profile is not None
    assert profile.raw_text == CARD
    assert profile.match_text == MATCH
    assert (profile.name, profile.age, profile.city) == ("Fakename", 25, "Faketown")
    assert profile.about == "fake description"


def test_without_a_card_the_match_text_is_never_the_raw_text() -> None:
    profile = build_leomatch_profile(None, MATCH, None)

    assert profile is not None
    assert profile.raw_text is None
    assert profile.match_text == MATCH
    assert (profile.name, profile.name_source) == (None, None)


def test_a_match_text_that_looks_like_a_card_is_never_parsed() -> None:
    profile = build_leomatch_profile(None, "Matchname, 40, Matchtown - from the match", None)

    assert profile is not None
    assert (profile.name, profile.age, profile.city, profile.about) == (None, None, None, None)


def test_without_a_card_the_name_comes_from_the_link_text() -> None:
    profile = build_leomatch_profile(None, OBSERVED_MATCH, "Аня")

    assert profile is not None
    assert (profile.name, profile.name_source) == ("Аня", LeomatchNameSource.MATCH)
    assert (profile.age, profile.city, profile.about) == (None, None, None)


def test_a_card_that_does_not_parse_falls_back_to_the_link_text() -> None:
    profile = build_leomatch_profile("a card that is no card", MATCH, "Linkname")

    assert profile is not None
    assert profile.raw_text == "a card that is no card"
    assert (profile.name, profile.name_source) == ("Linkname", LeomatchNameSource.MATCH)


def test_a_parsed_card_name_wins_over_the_link_text() -> None:
    profile = build_leomatch_profile("Cardname, 22 - card", MATCH, "Linkname")

    assert profile is not None
    assert (profile.name, profile.age, profile.name_source) == (
        "Cardname",
        22,
        LeomatchNameSource.CARD,
    )


def test_the_raw_text_is_kept_when_nothing_parses() -> None:
    profile = build_leomatch_profile("free text card", "free text match", None)

    assert profile is not None
    assert profile.raw_text == "free text card"
    assert (profile.name, profile.age, profile.city, profile.about) == (None, None, None, None)


def test_stored_texts_are_cleaned_and_truncated() -> None:
    long_match = "m" * (MAX_LEOMATCH_TEXT_LENGTH * 2)

    profile = build_leomatch_profile("  ca\x00rd  ", long_match, None)

    assert profile is not None
    assert profile.raw_text == "card"
    assert profile.match_text == "m" * MAX_LEOMATCH_TEXT_LENGTH


def test_the_link_name_is_cleaned_and_truncated() -> None:
    profile = build_leomatch_profile(None, MATCH, " Li\x00nk" + "k" * 500)

    assert profile is not None
    assert profile.name is not None
    assert profile.name.startswith("Link")
    assert len(profile.name) == MAX_LEOMATCH_NAME_LENGTH


def test_a_blank_link_name_gives_no_name() -> None:
    profile = build_leomatch_profile(None, MATCH, "  \x00 ")

    assert profile is not None
    assert (profile.name, profile.name_source) == (None, None)
