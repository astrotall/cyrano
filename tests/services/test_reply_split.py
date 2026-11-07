from __future__ import annotations

import pytest

from cyrano.domain.reply import MAX_REPLY_PARTS, REPLY_MAX_CHARS
from cyrano.services.reply_split import split_reply


def test_the_part_limit_is_three() -> None:
    assert MAX_REPLY_PARTS == 3


def test_one_line_is_one_part() -> None:
    assert split_reply("see you there") == ["see you there"]


def test_two_lines_are_two_parts_in_order() -> None:
    assert split_reply("first line\nsecond line") == ["first line", "second line"]


@pytest.mark.parametrize(
    "text",
    ["one\n\ntwo", "one\n\n\n\ntwo", "one\r\ntwo", "one\n \n\t\ntwo", "\n\none\ntwo\n\n"],
)
def test_any_run_of_line_breaks_and_blank_lines_is_one_separator(text: str) -> None:
    assert split_reply(text) == ["one", "two"]


def test_every_part_is_stripped() -> None:
    assert split_reply("   hey there  \n\t  how are you\t ") == ["hey there", "how are you"]


def test_three_lines_are_three_parts() -> None:
    assert split_reply("a\nb\nc") == ["a", "b", "c"]


def test_more_lines_than_the_limit_merge_into_the_last_part_with_one_space() -> None:
    assert split_reply("a\nb\nc\nd\n\ne") == ["a", "b", "c d e"]


def test_the_merged_remainder_keeps_every_line() -> None:
    lines = [f"line {number}" for number in range(10)]

    parts = split_reply("\n".join(lines))

    assert len(parts) == MAX_REPLY_PARTS
    assert parts[:2] == lines[:2]
    assert parts[2] == " ".join(lines[2:])
    assert " ".join(parts) == " ".join(lines)


def test_emoji_only_lines_are_parts() -> None:
    assert split_reply("😂\n🙈🙈\nok") == ["😂", "🙈🙈", "ok"]


def test_a_very_long_line_is_not_cut() -> None:
    text = "word " * (REPLY_MAX_CHARS * 4)

    assert split_reply(text) == [text.strip()]


@pytest.mark.parametrize("text", ["", "\n", "\n\n\n", "  \n \t \n"])
def test_a_text_without_any_visible_character_gives_no_part(text: str) -> None:
    assert split_reply(text) == []


@pytest.mark.parametrize("text", ["x", "a\nb", "a\nb\nc\nd", " \n y \n"])
def test_a_text_with_a_visible_character_never_gives_an_empty_list_or_an_empty_part(
    text: str,
) -> None:
    parts = split_reply(text)

    assert parts
    assert all(part and part == part.strip() for part in parts)
    assert len(parts) <= MAX_REPLY_PARTS
