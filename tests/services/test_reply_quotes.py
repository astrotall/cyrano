from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest

from cyrano.domain.enums import MessageDirection
from cyrano.domain.models import Message
from cyrano.domain.reply import MessageRef, ReplyPart
from cyrano.services.reply_quotes import (
    quote_label,
    split_reply_with_targets,
    strip_stray_markers,
    with_late_target,
)
from cyrano.services.reply_split import split_reply

INCOMING = MessageDirection.INCOMING
OUTGOING = MessageDirection.OUTGOING
REFS = {
    1: MessageRef(tg_message_id=91001, direction=INCOMING),
    2: MessageRef(tg_message_id=91002, direction=OUTGOING),
    3: MessageRef(tg_message_id=91003, direction=INCOMING),
    4: MessageRef(tg_message_id=91004, direction=INCOMING),
}
ARROW_AND_DIGIT = re.compile(r"↩️?\s*\d")
NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
LATE = 300
CONTACT_ID = 7


def _split(text: str) -> list[ReplyPart]:
    parts = split_reply_with_targets(text, REFS)
    for part in parts:
        assert ARROW_AND_DIGIT.search(part.text) is None
        assert part.text == part.text.strip()
        assert part.text
    return parts


def _targets(parts: list[ReplyPart]) -> list[int | None]:
    return [part.reply_to_tg_message_id for part in parts]


def _numbers(parts: list[ReplyPart]) -> list[int | None]:
    return [part.quote_number for part in parts]


def test_a_reply_without_markers_splits_exactly_like_split_reply() -> None:
    text = "one\n\ntwo\nthree\nfour"

    parts = _split(text)

    assert [part.text for part in parts] == split_reply(text)
    assert _targets(parts) == [None, None, None]


@pytest.mark.parametrize(
    ("text", "expected_texts", "expected_targets"),
    [
        ("↩3 first\nsecond\nthird", ["first", "second", "third"], [91003, None, None]),
        ("first\n↩3 second\nthird", ["first", "second", "third"], [None, 91003, None]),
        ("first\nsecond\n↩1 third", ["first", "second", "third"], [None, None, 91001]),
    ],
)
def test_a_marker_at_the_start_of_the_first_middle_or_last_line_targets_that_part(
    text: str, expected_texts: list[str], expected_targets: list[int | None]
) -> None:
    parts = _split(text)

    assert [part.text for part in parts] == expected_texts
    assert _targets(parts) == expected_targets


@pytest.mark.parametrize(
    "line",
    ["↩3 yes", "↩ 3 yes", "↩  3   yes", "↩3: yes", "↩3. yes", "↩3- yes", "↩3:yes", "  ↩3 yes"],
)
def test_spaces_and_a_colon_dot_or_dash_after_the_marker_are_accepted(line: str) -> None:
    [part] = _split(line)

    assert (part.text, part.reply_to_tg_message_id, part.quote_number) == ("yes", 91003, 3)


def test_the_emoji_form_of_the_arrow_is_a_marker_too() -> None:
    [part] = _split("↩️4 yes")

    assert (part.text, part.reply_to_tg_message_id) == ("yes", 91004)


@pytest.mark.parametrize("text", ["↩9 yes", "↩0 yes", "↩999 yes", "↩2 yes", "↩5 yes"])
def test_an_unknown_an_outgoing_or_an_out_of_range_number_is_dropped_silently(text: str) -> None:
    [part] = _split(text)

    assert (part.text, part.reply_to_tg_message_id, part.quote_number) == ("yes", None, None)


def test_more_than_three_digits_is_no_marker_and_is_stripped_as_noise() -> None:
    [part] = _split("↩1003 yes")

    assert (part.text, part.reply_to_tg_message_id) == ("yes", None)


def test_only_the_first_valid_marker_is_honoured() -> None:
    parts = _split("↩3 one\n↩4 two\n↩1 three")

    assert [part.text for part in parts] == ["one", "two", "three"]
    assert _targets(parts) == [91003, None, None]


def test_an_invalid_first_marker_lets_the_next_valid_one_win() -> None:
    parts = _split("↩2 one\n↩9 two\n↩4 three")

    assert _targets(parts) == [None, None, 91004]
    assert _numbers(parts) == [None, None, 4]


def test_a_marker_inside_a_line_is_stripped_as_noise() -> None:
    [part] = _split("yes ↩3 of course ↩12: really")

    assert (part.text, part.reply_to_tg_message_id) == ("yes of course really", None)


def test_a_marker_after_a_leading_one_on_the_same_line_is_stripped() -> None:
    [part] = _split("↩3 ↩4 yes")

    assert (part.text, part.reply_to_tg_message_id) == ("yes", 91003)


def test_a_marker_only_line_disappears_with_its_marker() -> None:
    parts = _split("↩3\nfine\n↩4:")

    assert [part.text for part in parts] == ["fine"]
    assert _targets(parts) == [None]


def test_a_marker_only_line_lets_a_later_marker_win() -> None:
    parts = _split("↩3\n↩4 fine")

    assert [part.text for part in parts] == ["fine"]
    assert _targets(parts) == [91004]


def test_a_reply_of_markers_only_gives_no_part() -> None:
    assert _split("↩3\n↩4") == []


@pytest.mark.parametrize(
    ("text", "expected_targets"),
    [
        ("one\ntwo\n↩3 three\nfour\nfive", [None, None, 91003]),
        ("one\ntwo\nthree\nfour\n↩3 five", [None, None, 91003]),
        ("one\n↩3 two\nthree\nfour", [None, 91003, None]),
    ],
)
def test_merged_lines_pass_their_target_to_the_merged_part(
    text: str, expected_targets: list[int | None]
) -> None:
    parts = _split(text)

    assert len(parts) == 3
    assert _targets(parts) == expected_targets


def test_merged_lines_never_join_into_an_arrow_and_a_digit() -> None:
    parts = _split("one\ntwo\nthree ↩\n4 four\nfive")

    assert [part.text for part in parts] == ["one", "two", "three four five"]


def test_a_lone_arrow_without_a_number_stays_as_text() -> None:
    [part] = _split("see ↩ above")

    assert part.text == "see ↩ above"


def test_no_markers_and_no_refs_give_the_same_text() -> None:
    parts = split_reply_with_targets("↩3 hi", {})

    assert [(part.text, part.reply_to_tg_message_id) for part in parts] == [("hi", None)]


def test_strip_stray_markers_repeats_until_none_is_left() -> None:
    assert ARROW_AND_DIGIT.search(strip_stray_markers("a ↩↩3 4 b")) is None
    assert quote_label(12) == "↩12"


def _trigger(
    *, ago: float, direction: MessageDirection = INCOMING, contact_id: int = CONTACT_ID
) -> Message:
    return Message(
        id=55,
        contact_id=contact_id,
        direction=direction,
        text="invented trigger",
        tg_message_id=92055,
        created_at=NOW - timedelta(seconds=ago),
    )


def _plain(*texts: str) -> list[ReplyPart]:
    return [ReplyPart(text=text) for text in texts]


def test_the_late_rule_quotes_the_first_part_exactly_at_the_threshold() -> None:
    parts = with_late_target(_plain("one", "two"), _trigger(ago=LATE), CONTACT_ID, NOW, LATE)

    assert _targets(parts) == [92055, None]
    assert [part.text for part in parts] == ["one", "two"]


def test_the_late_rule_counts_from_the_planned_send_time_of_the_first_part() -> None:
    trigger = _trigger(ago=0)

    on_time = with_late_target(
        _plain("one"), trigger, CONTACT_ID, NOW + timedelta(seconds=LATE), LATE
    )
    early = with_late_target(
        _plain("one"), trigger, CONTACT_ID, NOW + timedelta(seconds=LATE - 1), LATE
    )

    assert _targets(on_time) == [92055]
    assert _targets(early) == [None]


def test_one_second_less_than_the_threshold_is_not_late() -> None:
    parts = with_late_target(_plain("one"), _trigger(ago=LATE - 1), CONTACT_ID, NOW, LATE)

    assert _targets(parts) == [None]


def test_a_threshold_of_zero_disables_the_late_rule() -> None:
    parts = with_late_target(_plain("one"), _trigger(ago=86400), CONTACT_ID, NOW, 0)

    assert _targets(parts) == [None]


def test_a_model_target_is_never_joined_by_a_second_one() -> None:
    model = [ReplyPart(text="one"), ReplyPart(text="two", quote_number=3, reply_to_tg_message_id=1)]

    parts = with_late_target(model, _trigger(ago=LATE * 10), CONTACT_ID, NOW, LATE)

    assert _targets(parts) == [None, 1]


@pytest.mark.parametrize(
    "trigger",
    [
        None,
        _trigger(ago=LATE * 10, direction=OUTGOING),
        _trigger(ago=LATE * 10, contact_id=CONTACT_ID + 1),
    ],
)
def test_a_missing_outgoing_or_foreign_trigger_gives_no_late_target(
    trigger: Message | None,
) -> None:
    parts = with_late_target(_plain("one"), trigger, CONTACT_ID, NOW, LATE)

    assert _targets(parts) == [None]


def test_the_late_rule_on_no_part_gives_no_part() -> None:
    assert with_late_target([], _trigger(ago=LATE * 10), CONTACT_ID, NOW, LATE) == []


def test_a_part_never_shows_its_text_or_target_in_its_repr() -> None:
    part = ReplyPart(text="invented-quote-text-zq", quote_number=3, reply_to_tg_message_id=987654)

    assert "invented-quote-text-zq" not in repr(part)
    assert "987654" not in repr(part)
