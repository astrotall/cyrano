from __future__ import annotations

import json
from pathlib import Path

import pytest

from cyrano.domain.errors import (
    MAX_REPORTED_PROBLEMS,
    ContentValidationError,
    ImportFileError,
)
from cyrano.domain.style import (
    MAX_DIALOGUES_PER_FILE,
    MAX_PERSONA_LENGTH,
    MAX_TAG_LENGTH,
    MAX_TURN_LENGTH,
    MAX_TURNS_PER_DIALOGUE,
)
from cyrano.services.style_files import (
    MAX_IMPORT_FILE_BYTES,
    parse_examples,
    parse_persona,
    read_import_file,
)

MARKER = "zq-private-marker"


def _dialogue(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "tags": ["default"],
        "turns": [{"role": "them", "text": "hello"}, {"role": "me", "text": "hi there"}],
    }
    return {**base, **overrides}


def _file(*dialogues: dict[str, object], **overrides: object) -> str:
    payload: dict[str, object] = {"version": 1, "dialogues": list(dialogues) or [_dialogue()]}
    return json.dumps({**payload, **overrides})


def _problems(raw: str) -> list[tuple[str, str]]:
    with pytest.raises(ContentValidationError) as caught:
        parse_examples(raw)
    assert MARKER not in str(caught.value)
    return [(problem.path, problem.rule) for problem in caught.value.problems]


def test_a_valid_file_is_parsed() -> None:
    parsed = parse_examples(
        _file(_dialogue(), _dialogue(tags=["Flirty"]), {"turns": _dialogue()["turns"]})
    )

    assert [example.tags for example in parsed] == [["default"], ["flirty"], []]
    assert parsed[0].turns[1].text == "hi there"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (_file(version=2), [("version", "must be 1")]),
        (_file(version="1"), [("version", "must be an integer")]),
        (json.dumps({"dialogues": [_dialogue()]}), [("version", "missing")]),
        (json.dumps({"version": 1}), [("dialogues", "missing")]),
        (_file(version=True), [("version", "must be an integer")]),
        (json.dumps({"version": 1, "dialogues": []}), [("dialogues", "empty")]),
        (json.dumps([1, 2]), [("(file)", "must be an object")]),
        (json.dumps({"version": 1, "dialogues": "x"}), [("dialogues", "must be a list")]),
        (json.dumps({"version": 1, "dialogues": [5]}), [("dialogues[0]", "must be an object")]),
    ],
)
def test_file_level_rules(raw: str, expected: list[tuple[str, str]]) -> None:
    assert _problems(raw) == expected


def test_a_dialogue_without_my_turn() -> None:
    raw = _file(_dialogue(), _dialogue(turns=[{"role": "them", "text": MARKER}]))

    assert _problems(raw) == [("dialogues[1]", "needs at least one 'me' turn")]


def test_a_dialogue_whose_last_turn_is_not_mine() -> None:
    turns = [{"role": "me", "text": MARKER}, {"role": "them", "text": MARKER}]

    assert _problems(_file(_dialogue(turns=turns))) == [
        ("dialogues[0]", "the last turn must be 'me'")
    ]


@pytest.mark.parametrize("text", ["", "   ", "\x00"])
def test_an_empty_text_is_reported_with_its_path(text: str) -> None:
    turns = [{"role": "them", "text": "ok"}, {"role": "me", "text": text}]

    assert _problems(_file(_dialogue(), _dialogue(), _dialogue(), _dialogue(turns=turns))) == [
        ("dialogues[3].turns[1].text", "empty")
    ]


def test_a_text_that_is_not_a_string_is_reported() -> None:
    turns = [{"role": "me", "text": 5}]

    assert _problems(_file(_dialogue(turns=turns))) == [
        ("dialogues[0].turns[0].text", "must be text")
    ]


def test_a_turn_that_is_too_long() -> None:
    turns = [{"role": "me", "text": MARKER * MAX_TURN_LENGTH}]

    assert _problems(_file(_dialogue(turns=turns))) == [
        ("dialogues[0].turns[0].text", f"too long (max {MAX_TURN_LENGTH})")
    ]


def test_too_many_turns() -> None:
    turns = [{"role": "me", "text": "x"}] * (MAX_TURNS_PER_DIALOGUE + 1)

    assert _problems(_file(_dialogue(turns=turns))) == [
        ("dialogues[0].turns", f"too many (max {MAX_TURNS_PER_DIALOGUE})")
    ]


def test_no_turns_at_all() -> None:
    assert _problems(_file(_dialogue(turns=[]))) == [("dialogues[0].turns", "empty")]


def test_too_many_dialogues() -> None:
    raw = _file(*[_dialogue()] * (MAX_DIALOGUES_PER_FILE + 1))

    assert ("dialogues", f"too many (max {MAX_DIALOGUES_PER_FILE})") in _problems(raw)


def test_the_maximum_number_of_dialogues_passes() -> None:
    parsed = parse_examples(_file(*[_dialogue()] * MAX_DIALOGUES_PER_FILE))

    assert len(parsed) == MAX_DIALOGUES_PER_FILE


def test_too_many_tags() -> None:
    tags = [f"t{index}" for index in range(6)]

    assert _problems(_file(_dialogue(tags=tags))) == [("dialogues[0].tags", "too many (max 5)")]


@pytest.mark.parametrize("tag", [f"{MARKER} spaced", "x" * (MAX_TAG_LENGTH + 1), "", "a/b"])
def test_a_bad_tag_is_reported_by_position(tag: str) -> None:
    problems = _problems(_file(_dialogue(tags=["ok", tag])))

    assert [path for path, _ in problems] == ["dialogues[0].tags[1]"]
    assert problems[0][1] == "empty" or str(MAX_TAG_LENGTH) in problems[0][1]


def test_tags_that_are_not_a_list() -> None:
    assert _problems(_file(_dialogue(tags="default"))) == [("dialogues[0].tags", "must be a list")]


def test_an_unknown_role() -> None:
    turns = [{"role": MARKER, "text": "x"}, {"role": "me", "text": "y"}]

    assert _problems(_file(_dialogue(turns=turns))) == [
        ("dialogues[0].turns[0].role", "must be 'me' or 'them'")
    ]


def test_unknown_keys_are_reported_without_naming_the_key() -> None:
    turns = [{"role": "me", "text": "x", MARKER: 1}]
    raw = json.dumps(
        {"version": 1, MARKER: 1, "dialogues": [{**_dialogue(), MARKER: 1, "turns": turns}]}
    )

    assert sorted(_problems(raw)) == [
        ("(file)", "unknown key"),
        ("dialogues[0]", "unknown key"),
        ("dialogues[0].turns[0]", "unknown key"),
    ]


def test_every_problem_is_collected_but_at_most_twenty_are_printed() -> None:
    bad = _dialogue(turns=[{"role": "them", "text": ""}, {"role": "me", "text": MARKER}])
    raw = _file(*[bad] * 30)

    with pytest.raises(ContentValidationError) as caught:
        parse_examples(raw)

    assert len(caught.value.problems) == 30
    lines = str(caught.value).splitlines()
    assert lines[0].startswith("style examples file is invalid: 30 problem(s)")
    assert len(lines) - 1 == MAX_REPORTED_PROBLEMS
    assert "dialogues[19].turns[0].text: empty" in lines[-1]
    assert MARKER not in str(caught.value)


def test_invalid_json_is_a_readable_error_without_the_content() -> None:
    with pytest.raises(ContentValidationError) as caught:
        parse_examples('{"version": 1, "dialogues": [' + MARKER)

    message = str(caught.value)
    assert "invalid JSON" in message
    assert "line 1 column" in message
    assert MARKER not in message


def test_deeply_nested_json_is_a_readable_error() -> None:
    depth = MAX_IMPORT_FILE_BYTES // 2
    with pytest.raises(ContentValidationError, match="nested too deeply"):
        parse_examples("[" * depth + "]" * depth)


def test_persona_text_is_parsed_cleaned_and_limited() -> None:
    assert parse_persona("  # Who I am\x00\n\nText \n").text == "# Who I am\n\nText"
    assert parse_persona("x" * MAX_PERSONA_LENGTH).text


@pytest.mark.parametrize(
    ("raw", "rule"),
    [
        ("", "empty"),
        (" \n\t \x00", "empty"),
        (MARKER * MAX_PERSONA_LENGTH, f"too long (max {MAX_PERSONA_LENGTH})"),
    ],
)
def test_invalid_persona_text(raw: str, rule: str) -> None:
    with pytest.raises(ContentValidationError) as caught:
        parse_persona(raw)

    assert [(p.path, p.rule) for p in caught.value.problems] == [("(file)", rule)]
    assert MARKER not in str(caught.value)


def test_reading_a_file_returns_its_text_and_drops_a_bom(tmp_path: Path) -> None:
    path = tmp_path / "a.json"
    path.write_bytes("﻿привет".encode())

    assert read_import_file(path) == "привет"


def test_a_missing_file_is_a_readable_error(tmp_path: Path) -> None:
    with pytest.raises(ImportFileError, match="not a readable file"):
        read_import_file(tmp_path / "absent.json")


def test_a_directory_is_not_a_file(tmp_path: Path) -> None:
    with pytest.raises(ImportFileError, match="not a readable file"):
        read_import_file(tmp_path)


def test_an_oversized_file_is_refused_without_being_parsed(tmp_path: Path) -> None:
    path = tmp_path / "big.json"
    path.write_bytes(b" " * (MAX_IMPORT_FILE_BYTES + 1))

    with pytest.raises(ImportFileError, match="larger than"):
        read_import_file(path)


def test_a_file_exactly_at_the_size_limit_is_read(tmp_path: Path) -> None:
    path = tmp_path / "edge.json"
    path.write_bytes(b" " * MAX_IMPORT_FILE_BYTES)

    assert len(read_import_file(path)) == MAX_IMPORT_FILE_BYTES


def test_a_file_that_is_not_utf8_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "latin.md"
    path.write_bytes(b"caf\xe9 " + MARKER.encode())

    with pytest.raises(ImportFileError, match="not valid UTF-8") as caught:
        read_import_file(path)

    assert MARKER not in str(caught.value)
