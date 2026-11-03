from __future__ import annotations

import pytest

from cyrano.domain.errors import ReplyInvalid
from cyrano.domain.reply import (
    REPLY_BOUNDARY_REASON,
    REPLY_EMPTY_REASON,
    REPLY_HEADER_REASON,
    REPLY_MAX_CHARS,
)
from cyrano.prompts.texts import DIALOGUE_BOUNDARY, DIALOGUE_END, PERSONA_HEADER
from cyrano.services.reply_postprocess import cap_length, clean_reply


def test_the_cap_is_six_hundred_characters() -> None:
    assert REPLY_MAX_CHARS == 600


def test_surrounding_whitespace_is_trimmed() -> None:
    assert clean_reply("  \n hello there \n\n") == "hello there"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('"hello there"', "hello there"),
        ("“hello there”", "hello there"),
        ("«hello there»", "hello there"),
        ('"  padded  "', "padded"),
        ("«привет»", "привет"),
    ],
)
def test_one_pair_of_outer_quotation_marks_is_removed(raw: str, expected: str) -> None:
    assert clean_reply(raw) == expected


def test_only_one_pair_is_removed() -> None:
    assert clean_reply("\u00ab\u201cnested\u201d\u00bb") == "\u201cnested\u201d"


def test_identical_nested_quotes_are_ambiguous_and_stay() -> None:
    assert clean_reply('""double""') == '""double""'


@pytest.mark.parametrize(
    "raw",
    [
        'she said "yes" and "no"',
        '"yes" she said',
        'he is "so" cool',
        "«one» and «two»",
    ],
)
def test_quotes_that_do_not_wrap_the_whole_reply_stay(raw: str) -> None:
    assert clean_reply(raw) == raw


def test_a_lone_quote_stays() -> None:
    assert clean_reply('"') == '"'


@pytest.mark.parametrize(
    "raw",
    [
        "Me: see you soon",
        "me: see you soon",
        "Я: see you soon",
        "Assistant: see you soon",
        "Ответ: see you soon",
        "**Me:** see you soon",
        "**Me**: see you soon",
        "__Me:__ see you soon",
        "Me :   see you soon",
        "> Me: see you soon",
    ],
)
def test_a_leading_speaker_prefix_is_removed(raw: str) -> None:
    assert clean_reply(raw) == "see you soon"


def test_only_a_leading_prefix_is_removed() -> None:
    assert clean_reply("call me: see you soon") == "call me: see you soon"
    assert clean_reply("fine\nMe: see you soon") == "fine\nMe: see you soon"


def test_a_word_that_merely_starts_like_a_prefix_stays() -> None:
    assert clean_reply("Mery: hello") == "Mery: hello"
    assert clean_reply("Meet: later") == "Meet: later"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("**bold** text", "bold text"),
        ("__bold__ text", "bold text"),
        ("*italic* text", "italic text"),
        ("_italic_ text", "italic text"),
        ("use `code` here", "use code here"),
        ("# Heading\nbody", "Heading\nbody"),
        ("### Deep heading", "Deep heading"),
        ("```\nblock\n```", "block"),
        ("```python\nprint(1)\n```", "print(1)"),
        ("snake_case_name stays", "snake_case_name stays"),
        ("2 * 3 * 4", "2 * 3 * 4"),
        ("a#b and #1 stay", "a#b and #1 stay"),
    ],
)
def test_markdown_artifacts_are_removed_and_plain_text_is_kept(raw: str, expected: str) -> None:
    assert clean_reply(raw) == expected


def test_runs_of_blank_lines_collapse_to_one_blank_line() -> None:
    assert clean_reply("one\n\n\n\n\ntwo\n\nthree") == "one\n\ntwo\n\nthree"


def test_windows_line_breaks_and_trailing_spaces_are_normalised() -> None:
    assert clean_reply("one  \r\n\r\n\r\n\r\ntwo") == "one\n\ntwo"


def test_a_short_reply_is_not_cut() -> None:
    text = "x" * REPLY_MAX_CHARS

    assert clean_reply(text) == text


def test_a_long_reply_is_cut_at_a_sentence_boundary() -> None:
    sentence = "This is a sentence of moderate length. "
    cleaned = clean_reply(sentence * 40)

    assert len(cleaned) <= REPLY_MAX_CHARS
    assert cleaned.endswith("length.")
    assert cleaned == (sentence * 40)[: len(cleaned)]


def test_a_long_reply_with_question_and_exclamation_cuts_after_them() -> None:
    cleaned = clean_reply("Really? " * 200)

    assert cleaned.endswith("Really?")
    assert len(cleaned) <= REPLY_MAX_CHARS


def test_a_decimal_point_is_not_a_sentence_boundary() -> None:
    text = "a" * 596 + "3.14159 and more words after that"

    cleaned = cap_length(text, 600)

    assert len(cleaned) <= 600
    assert not cleaned.endswith("3.")


def test_a_long_reply_without_a_sentence_end_is_cut_at_a_space() -> None:
    cleaned = clean_reply("word " * 200)

    assert len(cleaned) <= REPLY_MAX_CHARS
    assert cleaned.endswith("word")


def test_a_long_reply_without_any_break_is_cut_hard() -> None:
    assert clean_reply("x" * 1000) == "x" * REPLY_MAX_CHARS


def test_a_paragraph_break_is_a_boundary_when_there_is_no_sentence_end() -> None:
    text = "first paragraph without dots " + "a" * 100 + "\n" + "b" * 600

    cleaned = cap_length(text, 600)

    assert cleaned == "first paragraph without dots " + "a" * 100


def test_the_cap_is_a_parameter() -> None:
    assert clean_reply("One. Two. Three.", limit=10) == "One. Two."


def test_wrappers_markdown_and_the_cap_combine() -> None:
    raw = '"**Me:** ' + "Sentence number one. " * 50 + '"'

    cleaned = clean_reply(raw)

    assert cleaned.startswith("Sentence number one.")
    assert len(cleaned) <= REPLY_MAX_CHARS
    assert "*" not in cleaned
    assert not cleaned.startswith('"')


def test_a_prefix_inside_quotes_and_quotes_after_a_prefix_are_both_handled() -> None:
    assert clean_reply('"Me: hello"') == "hello"
    assert clean_reply('Me: "hello"') == "hello"
    assert clean_reply("**Me:** «hello»") == "hello"


@pytest.mark.parametrize("raw", ["", "   ", "\n\n", '""', "```\n```", "Me:", "`", "**Me:**"])
def test_a_reply_that_is_empty_after_cleaning_is_invalid(raw: str) -> None:
    with pytest.raises(ReplyInvalid) as caught:
        clean_reply(raw)

    assert caught.value.reason == REPLY_EMPTY_REASON


def test_a_reply_with_the_boundary_token_is_invalid() -> None:
    with pytest.raises(ReplyInvalid) as caught:
        clean_reply(f"sure\n{DIALOGUE_END}")

    assert caught.value.reason == REPLY_BOUNDARY_REASON


def test_a_reply_with_only_the_boundary_token_is_invalid() -> None:
    with pytest.raises(ReplyInvalid) as caught:
        clean_reply(DIALOGUE_BOUNDARY)

    assert caught.value.reason == REPLY_BOUNDARY_REASON


def test_a_reply_with_a_layer_header_is_invalid() -> None:
    with pytest.raises(ReplyInvalid) as caught:
        clean_reply(f"here you go\n{PERSONA_HEADER}\nsecret persona")

    assert caught.value.reason == REPLY_HEADER_REASON


def test_a_header_hidden_by_markdown_is_still_caught() -> None:
    with pytest.raises(ReplyInvalid):
        clean_reply("=== PERSONA **===**")


def test_the_error_carries_no_reply_text() -> None:
    with pytest.raises(ReplyInvalid) as caught:
        clean_reply(f"private-words {DIALOGUE_BOUNDARY}")

    assert "private-words" not in str(caught.value)


def test_a_normal_reply_passes_unchanged() -> None:
    assert clean_reply("haha, nice one :) see you around") == "haha, nice one :) see you around"
