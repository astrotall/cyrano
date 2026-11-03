from __future__ import annotations

import time

import pytest

from cyrano.domain.chat_export import RedactionKind
from cyrano.services.chat_redaction import Redactor, name_words

EMAIL = RedactionKind.EMAIL
PHONE = RedactionKind.PHONE
LINK = RedactionKind.LINK
USERNAME = RedactionKind.USERNAME
NAME = RedactionKind.NAME


def _redact(text: str, names: list[str] | None = None, words: list[str] | None = None) -> str:
    return Redactor(names or [], words or []).redact(text).text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("write to anna.k+tag@mail.example.org now", "write to <email> now"),
        ("A@B.CO", "<email>"),
        ("почта: имя@пример.рф", "почта: <email>"),
        ("a@b without a dot domain", "a@b without a dot domain"),
    ],
)
def test_emails(text: str, expected: str) -> None:
    assert _redact(text) == expected


@pytest.mark.parametrize(
    "number",
    [
        "+7 (900) 123-45-67",
        "8 900 123 45 67",
        "89001234567",
        "+380-50-123-45-67",
        "(495) 123-45-67",
        "123-4567",
        "+1 202 555 0147",
        "8 900 123 45 67",
    ],
)
def test_phone_numbers(number: str) -> None:
    result = Redactor([]).redact(f"call {number} soon")

    assert result.text == "call <phone> soon"
    assert result.counts == {PHONE: 1}


@pytest.mark.parametrize("text", ["12345", "123456", "1 2 3 4 5 6", "in 2024 we met", "at 10:30"])
def test_short_digit_runs_are_not_phones(text: str) -> None:
    assert _redact(text) == text


def test_digit_groups_split_by_a_newline_are_not_one_phone() -> None:
    assert _redact("123\n456\n789") == "123\n456\n789"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("see https://example.org/a?b=1&c=2 ok", "see <link> ok"),
        ("see http://example.org ok", "see <link> ok"),
        ("see www.example.org/path ok", "see <link> ok"),
        ("t.me/someone_here", "<link>"),
        ("join t.me/+AbCdEf123", "join <link>"),
        ("insta: instagram.com/someone", "insta: <link>"),
        ("example.com/some/path", "<link>"),
        ("go to https://example.org/a.", "go to <link>."),
        ("(see https://example.org/a)", "(see <link>)"),
        ("really? https://example.org/a!", "really? <link>!"),
        ("HTTPS://EXAMPLE.ORG/X", "<link>"),
    ],
)
def test_links(text: str, expected: str) -> None:
    assert _redact(text) == expected


def test_a_plain_domain_mention_without_a_path_is_left_alone() -> None:
    assert _redact("it ended.Then we left") == "it ended.Then we left"
    assert _redact("file report.pdf") == "file report.pdf"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ping @some_user_1 please", "ping <username> please"),
        ("@ab", "<username>"),
        ("a lone @ stays", "a lone @ stays"),
        ("@@x", "@@x"),
        ("@иван_п", "<username>"),
    ],
)
def test_usernames(text: str, expected: str) -> None:
    assert _redact(text) == expected


def test_an_email_is_one_redaction_and_not_a_username() -> None:
    result = Redactor([]).redact("zed@example.com")

    assert result.text == "<email>"
    assert result.counts == {EMAIL: 1}


def test_a_phone_inside_a_link_counts_as_the_link() -> None:
    result = Redactor([]).redact("https://example.org/call/+79001234567")

    assert result.text == "<link>"
    assert result.counts == {LINK: 1}


def test_names_from_senders_are_replaced_as_whole_words_case_insensitively() -> None:
    names = ["Zed Quark", "Olga Test"]

    result = Redactor(names).redact("Zed met OLGA and quark, then olga.")

    assert result.text == "<name> met <name> and <name>, then <name>."
    assert result.counts == {NAME: 4}


def test_cyrillic_names_match_in_every_letter_case() -> None:
    names = ["Лиана Вербина"]

    result = Redactor(names).redact("Привет, Лиана! ЛИАНА, лиана и вербина, ВЕРБИНА.")

    assert result.text == "Привет, <name>! <name>, <name> и <name>, <name>."
    assert result.counts == {NAME: 5}


def test_a_name_inside_a_longer_word_is_left_alone() -> None:
    text = "Лианами и Вербинаполис, Olgaville, oolga"

    assert _redact(text, ["Лиана Вербина", "Olga X"]) == text


def test_words_below_three_letters_are_not_redacted() -> None:
    names = ["Ян Ли Кор Max"]

    assert _redact("Ян и Ли с Кор и max", names) == "Ян и Ли с <name> и <name>"


def test_the_digits_and_punctuation_of_a_name_split_words() -> None:
    assert name_words(["Vasya99_the-Great"], 3) == ["Great", "Vasya", "the"]


def test_explicit_redact_words_are_replaced_whole_even_when_short() -> None:
    result = Redactor(["Zed Quark"], ["Лили", "Ян"]).redact("Лили, ЛИЛИ и ян; Зеда нет, Zed")

    assert result.text == "<name>, <name> и <name>; Зеда нет, <name>"
    assert result.counts == {NAME: 4}


def test_a_redact_phrase_is_split_into_words() -> None:
    assert _redact("Big and Bear", words=["big bear"]) == "<name> and <name>"


def test_a_name_inside_an_email_or_link_belongs_to_that_pattern() -> None:
    result = Redactor(["Zed Quark"]).redact("zed@quark.com and zed.quark.com/zed and Zed")

    assert result.text == "<email> and <link> and <name>"
    assert result.counts == {EMAIL: 1, LINK: 1, NAME: 1}


def test_a_name_that_equals_a_token_word_never_changes_a_token() -> None:
    result = Redactor(["Link Email"]).redact("see https://example.org and a@b.co, Link")

    assert result.text == "see <link> and <email>, <name>"


def test_counters_are_exact_and_the_matched_strings_are_absent() -> None:
    secrets = ["anna@example.org", "+7 900 123 45 67", "https://example.org/x", "@anna_x", "Лиана"]
    text = "; ".join(secrets)

    result = Redactor(["Лиана Вербина"]).redact(text)

    assert result.counts == {EMAIL: 1, PHONE: 1, LINK: 1, USERNAME: 1, NAME: 1}
    assert result.text == "<email>; <phone>; <link>; <username>; <name>"
    for secret in secrets:
        assert secret not in result.text


def test_text_without_anything_to_redact_is_unchanged() -> None:
    result = Redactor(["Zed Quark"]).redact("just a normal line, nothing here 👍")

    assert result.text == "just a normal line, nothing here 👍"
    assert result.counts == {}
    assert not result.only_tokens


@pytest.mark.parametrize("text", ["+7 900 123 45 67", "@someone,", "zed!!!"])
def test_a_text_made_only_of_tokens_is_detected(text: str) -> None:
    assert Redactor(["Zed Quark"]).redact(text).only_tokens


@pytest.mark.parametrize("text", ["call +7 900 123 45 67", "👍 hi @someone", "ok <3 zed"])
def test_a_text_with_words_besides_tokens_is_kept(text: str) -> None:
    result = Redactor(["Zed Quark"]).redact(text)

    assert not result.only_tokens
    assert result.text


def test_a_text_without_tokens_is_never_called_token_only() -> None:
    assert not Redactor([]).redact("?!").only_tokens


def test_redaction_never_returns_an_empty_text() -> None:
    for text in ["@a", "a@b.co", "+79001234567", "https://a.b/c", "Zed"]:
        assert Redactor(["Zed Quark"]).redact(text).text


@pytest.mark.parametrize(
    "text",
    ["a." * 20000, "9" * 20000, "1 " * 20000, "x@" * 10000, "http://" + "a" * 20000, "(" * 20000],
)
def test_pathological_input_is_processed_in_linear_time(text: str) -> None:
    started = time.monotonic()

    Redactor(["Zed Quark"]).redact(text)

    assert time.monotonic() - started < 2
