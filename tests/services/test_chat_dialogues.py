from __future__ import annotations

from datetime import timedelta

import pytest

from cyrano.domain.chat_export import ChatTurn, Dialogue, ExportMessage
from cyrano.domain.enums import TurnRole
from cyrano.domain.errors import ChatExportError
from cyrano.domain.style import MAX_TURN_LENGTH, MAX_TURNS_PER_DIALOGUE
from cyrano.services.chat_dialogues import (
    build_turns,
    dialogue_candidates,
    drop_duplicates,
    sample_evenly,
    split_conversations,
    truncate_at_word,
    truncate_turns,
)
from tests.chat_export_data import OTHER, OWNER, export_message

ME = TurnRole.ME
THEM = TurnRole.THEM
SIX_HOURS = timedelta(hours=6)


def _is_owner(message: ExportMessage) -> bool:
    return message.sender == OWNER


def _turns(*roles: TurnRole) -> list[ChatTurn]:
    return [ChatTurn(role, f"turn {index}") for index, role in enumerate(roles)]


def _roles(dialogue: Dialogue) -> list[TurnRole]:
    return [turn.role for turn in dialogue]


def _dialogue(label: str) -> Dialogue:
    return (ChatTurn(THEM, f"q {label}"), ChatTurn(ME, f"a {label}"))


def test_a_gap_longer_than_the_limit_starts_a_new_conversation() -> None:
    messages = [
        export_message(0, OTHER, "a"),
        export_message(60, OWNER, "b"),
        export_message(60 + 6 * 60 + 1, OTHER, "c"),
        export_message(60 + 6 * 60 + 2, OWNER, "d"),
    ]

    conversations = split_conversations(messages, SIX_HOURS)

    assert [[item.text for item in part] for part in conversations] == [["a", "b"], ["c", "d"]]


def test_a_gap_exactly_at_the_limit_stays_in_one_conversation() -> None:
    messages = [export_message(0, OTHER, "a"), export_message(6 * 60, OWNER, "b")]

    assert len(split_conversations(messages, SIX_HOURS)) == 1


def test_the_gap_is_measured_between_consecutive_messages_not_from_the_start() -> None:
    messages = [export_message(index * 300, OTHER, str(index)) for index in range(5)]

    assert len(split_conversations(messages, SIX_HOURS)) == 1


def test_no_messages_give_no_conversations() -> None:
    assert split_conversations([], SIX_HOURS) == []


def test_consecutive_messages_of_one_side_merge_into_one_turn() -> None:
    conversation = [
        export_message(0, OTHER, "one"),
        export_message(1, OTHER, "two"),
        export_message(2, OWNER, "three"),
        export_message(3, OWNER, "four"),
        export_message(4, OWNER, "five"),
        export_message(5, OTHER, "six"),
    ]

    assert build_turns(conversation, _is_owner) == [
        ChatTurn(THEM, "one\ntwo"),
        ChatTurn(ME, "three\nfour\nfive"),
        ChatTurn(THEM, "six"),
    ]


def test_every_other_sender_counts_as_them() -> None:
    conversation = [
        export_message(0, "Third Person", "a"),
        export_message(1, OTHER, "b"),
        export_message(2, OWNER, "c"),
    ]

    assert build_turns(conversation, _is_owner) == [ChatTurn(THEM, "a\nb"), ChatTurn(ME, "c")]


def test_the_window_ends_with_the_owner_turn_and_starts_with_them() -> None:
    turns = _turns(THEM, ME, THEM, ME, THEM, ME)

    candidates = dialogue_candidates(turns, window=3)

    assert [_roles(item) for item in candidates] == [
        [THEM, ME],
        [THEM, ME],
        [THEM, ME],
    ]
    assert candidates[-1] == tuple(turns[-2:])


def test_leading_owner_turns_are_removed_from_the_window() -> None:
    turns = _turns(THEM, ME, THEM, ME, THEM, ME)

    candidates = dialogue_candidates(turns, window=4)

    assert candidates[-1] == tuple(turns[2:])
    assert candidates[0] == tuple(turns[:2])


def test_a_window_without_a_them_turn_is_dropped() -> None:
    turns = _turns(ME, THEM, ME)

    candidates = dialogue_candidates(turns, window=2)

    assert [_roles(item) for item in candidates] == [[THEM, ME]]


def test_a_conversation_of_only_owner_turns_gives_nothing() -> None:
    assert dialogue_candidates(_turns(ME), window=6) == []


def test_a_window_larger_than_the_turn_limit_keeps_the_last_turns() -> None:
    count = MAX_TURNS_PER_DIALOGUE + 10
    turns = [ChatTurn(THEM if index % 2 == 0 else ME, f"t{index}") for index in range(count)]

    candidates = dialogue_candidates(turns, window=count)

    assert len(candidates[-1]) == MAX_TURNS_PER_DIALOGUE
    assert candidates[-1] == tuple(turns[-MAX_TURNS_PER_DIALOGUE:])
    assert all(len(item) <= MAX_TURNS_PER_DIALOGUE for item in candidates)
    assert all(item[0].role is THEM and item[-1].role is ME for item in candidates)


def test_a_long_turn_is_truncated_at_a_word_boundary_and_counted() -> None:
    long_text = " ".join(["word"] * 400)
    turns = [ChatTurn(THEM, "short"), ChatTurn(ME, long_text)]

    result, count = truncate_turns(turns)

    assert count == 1
    assert len(result[1].text) <= MAX_TURN_LENGTH
    assert result[1].text.endswith("word")
    assert long_text.startswith(result[1].text)
    assert result[0] == turns[0]


def test_a_turn_at_the_limit_is_not_truncated() -> None:
    text = "x" * MAX_TURN_LENGTH

    result, count = truncate_turns([ChatTurn(ME, text)])

    assert (result[0].text, count) == (text, 0)


def test_truncation_cuts_a_single_long_word_at_the_limit() -> None:
    assert truncate_at_word("x" * 1500) == "x" * MAX_TURN_LENGTH


def test_truncation_keeps_a_word_that_ends_exactly_at_the_limit() -> None:
    text = "x" * (MAX_TURN_LENGTH - 4) + " abc def"

    assert truncate_at_word(text) == "x" * (MAX_TURN_LENGTH - 4) + " abc"


def test_truncation_drops_a_word_that_crosses_the_limit() -> None:
    text = "x" * (MAX_TURN_LENGTH - 3) + " abcdef"

    assert truncate_at_word(text) == "x" * (MAX_TURN_LENGTH - 3)


def test_a_cut_after_a_space_does_not_drop_the_last_word() -> None:
    text = "x" * (MAX_TURN_LENGTH - 1) + " " + "d" * 10

    assert truncate_at_word(text) == "x" * (MAX_TURN_LENGTH - 1)


def test_exact_duplicate_dialogues_are_dropped_keeping_the_first() -> None:
    first, second = _dialogue("one"), _dialogue("two")

    assert drop_duplicates([first, second, first, second, first]) == [first, second]


def test_sampling_returns_everything_when_under_the_limit() -> None:
    dialogues = [_dialogue(str(index)) for index in range(5)]

    assert sample_evenly(dialogues, 5) == dialogues
    assert sample_evenly(dialogues, 100) == dialogues


def test_sampling_is_deterministic_and_evenly_spread() -> None:
    dialogues = [_dialogue(str(index)) for index in range(10)]

    picked = sample_evenly(dialogues, 5)

    assert picked == sample_evenly(dialogues, 5)
    assert [dialogues.index(item) for item in picked] == [1, 3, 5, 7, 9]


@pytest.mark.parametrize(("total", "limit"), [(501, 500), (1000, 7), (11, 10), (100, 1), (3, 2)])
def test_sampling_respects_the_limit_keeps_order_and_picks_distinct_items(
    total: int, limit: int
) -> None:
    dialogues = [_dialogue(str(index)) for index in range(total)]

    picked = sample_evenly(dialogues, limit)
    positions = [dialogues.index(item) for item in picked]

    assert len(picked) == limit
    assert positions == sorted(set(positions))
    assert positions[0] < total / limit
    assert positions[-1] >= total - total / limit


def test_a_max_above_the_file_limit_is_refused() -> None:
    from cyrano.domain.chat_export import ConvertOptions
    from cyrano.services.chat_export_converter import validate_options

    with pytest.raises(ChatExportError, match=r"--max must be from 1 to 500"):
        validate_options(ConvertOptions(me=OWNER, max_dialogues=501))
