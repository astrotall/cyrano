from __future__ import annotations

import pytest

from cyrano.domain.enums import ContactGender
from cyrano.gender_name_data import AMBIGUOUS_NAMES, MALE_NAMES, MASCULINE_NAMES_ENDING_IN_A_OR_YA
from cyrano.services.gender_guess import guess_gender

FEMALE = ContactGender.FEMALE
MALE = ContactGender.MALE


@pytest.mark.parametrize(
    "name", ["Анна", "Мария", "Ольга", "Екатерина", "Юлия", "Ксения", "Дарья", "Наталья", "Ася"]
)
def test_clear_feminine_names_are_female(name: str) -> None:
    assert guess_gender(name) is FEMALE


@pytest.mark.parametrize(
    "name",
    [
        "Никита",
        "Илья",
        "Данила",
        "Миша",
        "Дима",
        "Паша",
        "Гоша",
        "Коля",
        "Ваня",
        "Петя",
        "Витя",
        "Федя",
        "Костя",
        "Леша",
        "Леня",
        "Юра",
        "Жора",
        "Гриша",
        "Сережа",
        "Андрюша",
        "Кузьма",
        "Фома",
        "Савва",
    ],
)
def test_masculine_names_ending_in_a_or_ya_are_never_female(name: str) -> None:
    assert guess_gender(name) is not FEMALE
    assert guess_gender(name) is MALE


@pytest.mark.parametrize(
    "name", ["Саша", "Женя", "Валя", "Шура", "Слава", "Сашка", "Женька", "Жека"]
)
def test_ambiguous_unisex_diminutives_are_unknown(name: str) -> None:
    assert guess_gender(name) is None


@pytest.mark.parametrize(
    "name", ["Александр", "Алексей", "Дмитрий", "Максим", "Пётр", "Фёдор", "Артём", "Игорь", "Ян"]
)
def test_names_of_the_male_set_are_male(name: str) -> None:
    assert guess_gender(name) is MALE


@pytest.mark.parametrize("name", ["Любовь", "Айгуль", "Ингрид", "Нинель", "Ким", "Жасмин"])
def test_female_names_without_a_feminine_ending_stay_unknown(name: str) -> None:
    assert guess_gender(name) is None


@pytest.mark.parametrize("name", ["Жданбек", "Мерген", "Тансылу", "Zzz", "Петрушк"])
def test_an_unlisted_name_is_never_male_by_its_ending(name: str) -> None:
    assert guess_gender(name) is not MALE


def test_an_unlisted_name_ending_like_a_male_name_is_unknown() -> None:
    assert guess_gender("Жданбек") is None
    assert guess_gender("Игорьков") is None


@pytest.mark.parametrize(
    "name",
    ["Anna", "Alex", "Maria", "Аnna", "Нick", "Lola", "ANNA"],
)
def test_latin_script_gives_unknown(name: str) -> None:
    assert guess_gender(name) is None


@pytest.mark.parametrize("name", ["🌸", "🌸 Аня", "12345", "42", "!!!", "...", "", "   ", None])
def test_emoji_digits_punctuation_and_empty_input_give_unknown(name: str | None) -> None:
    assert guess_gender(name) is None


def test_only_the_first_word_counts() -> None:
    assert guess_gender("Анна Иванов") is FEMALE
    assert guess_gender("Иван Анна") is MALE
    assert guess_gender("Саша Мария") is None
    assert guess_gender("Alex Анна") is None
    assert guess_gender("Анна 🌸 Мария") is FEMALE


def test_a_decorated_first_word_keeps_its_letters() -> None:
    assert guess_gender("Аня🌸") is FEMALE
    assert guess_gender("Аня,") is FEMALE
    assert guess_gender("«Иван»") is MALE


def test_case_and_yo_variants_give_the_same_answer() -> None:
    assert guess_gender("АННА") is FEMALE
    assert guess_gender("анна") is FEMALE
    assert guess_gender("ПЁТР") is MALE
    assert guess_gender("петр") is MALE
    assert guess_gender("Фёдор") is MALE
    assert guess_gender("федор") is MALE
    assert guess_gender("САША") is None


def test_a_single_letter_is_unknown() -> None:
    assert guess_gender("А") is None
    assert guess_gender("Я") is None


def test_a_decomposed_letter_is_composed_before_the_check() -> None:
    assert guess_gender("Андреи\u0306") is MALE


def test_every_listed_name_gives_its_own_answer() -> None:
    for name in MALE_NAMES | MASCULINE_NAMES_ENDING_IN_A_OR_YA:
        assert guess_gender(name) is MALE, name
    for name in AMBIGUOUS_NAMES:
        assert guess_gender(name) is None, name


def test_the_guess_never_raises_on_odd_input() -> None:
    odd = ["\x00", "\ud800"[:0], "а" * 10_000, "​", "я" * 3, "\n\t", "я́", "ё"]
    for name in odd:
        assert guess_gender(name) in {FEMALE, MALE, None}
