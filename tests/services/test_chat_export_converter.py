from __future__ import annotations

import ast
import json
import os
from pathlib import Path

import pytest

from cyrano.domain.chat_export import ConvertOptions, RedactionKind, SkipReason
from cyrano.domain.errors import ChatExportError, ContentValidationError
from cyrano.services import chat_export_converter
from cyrano.services.chat_export_converter import (
    build_draft,
    check_target,
    convert_chat_export,
    normalize_tags,
    render_draft,
    resolve_output,
    validate_options,
    write_draft,
)
from cyrano.services.chat_export_reader import ExportData, read_messages
from cyrano.services.chat_redaction import Redactor, name_words
from cyrano.services.style_files import parse_examples, read_import_file
from tests.chat_export_data import (
    BACKTICK,
    CYRILLIC_OTHER,
    OTHER,
    OTHER_ID,
    OWNER,
    OWNER_ID,
    chat,
    message,
    write_export,
)

CONVERTER_MODULES = (
    "chat_export_reader",
    "chat_redaction",
    "chat_dialogues",
    "chat_export_converter",
)
FORBIDDEN_IMPORT_PREFIXES = (
    "cyrano.config",
    "cyrano.db",
    "cyrano.userbot",
    "cyrano.bot",
    "cyrano.llm",
    "telethon",
    "aiogram",
    "httpx",
    "sqlalchemy",
)


def _data(*items: object) -> ExportData:
    return read_messages(list(items))


def _simple_chat() -> list[dict[str, object]]:
    return chat(
        (0, OTHER, "how was your week?"),
        (1, OWNER, "busy but good"),
        (2, OTHER, "what did you do?"),
        (3, OWNER, "rode a bike"),
    )


def test_the_owner_is_found_by_the_exact_sender_name() -> None:
    draft = build_draft(_data(*_simple_chat()), ConvertOptions(me=OWNER))

    assert draft.report.dialogues_written == 2
    assert draft.dialogues[0][-1].text == "busy but good"


def test_an_owner_that_matches_no_sender_reports_only_a_count() -> None:
    data = _data(*_simple_chat())

    with pytest.raises(ChatExportError) as caught:
        build_draft(data, ConvertOptions(me="Somebody Else"))

    message_text = str(caught.value)
    assert "2 sender(s)" in message_text
    for name in (OWNER, OTHER, "Zed", "Quark", "Olga", "Test"):
        assert name not in message_text


def test_a_name_that_differs_only_by_case_does_not_match() -> None:
    with pytest.raises(ChatExportError, match="matches none"):
        build_draft(_data(*_simple_chat()), ConvertOptions(me=OWNER.lower()))


def test_the_other_side_is_every_other_sender() -> None:
    items = chat(
        (0, OTHER, "question one"),
        (1, "Third Person", "question two"),
        (2, OWNER, "my answer"),
    )

    draft = build_draft(_data(*items), ConvertOptions(me=OWNER))

    assert [turn.role.value for turn in draft.dialogues[0]] == ["them", "me"]
    assert "question one" in draft.dialogues[0][0].text
    assert "question two" in draft.dialogues[0][0].text


def test_no_dialogue_is_an_error_and_not_an_empty_file() -> None:
    items = chat((0, OWNER, "just me talking"), (1, OTHER, "unanswered"))

    with pytest.raises(ChatExportError, match="no dialogue could be built"):
        build_draft(_data(*items), ConvertOptions(me=OWNER))


def test_the_report_counts_every_stage() -> None:
    items = [
        *chat(
            (0, OTHER, "hello Zed"),
            (1, OWNER, "hi"),
            (2, OWNER, "how are you"),
            (3, OTHER, "+7 900 123 45 67"),
            (4, OTHER, "mail me at olga@example.org"),
            (5, OWNER, "ok"),
            (600, OTHER, "next day"),
            (601, OWNER, "yes"),
        ),
        message(7, OWNER, "", media_type="sticker"),
        message(8, OWNER, "fwd", forwarded_from="X"),
        "junk",
    ]

    report = build_draft(_data(*items), ConvertOptions(me=OWNER)).report

    assert report.messages_read == 11
    assert report.messages_kept == 8
    assert report.skipped == {
        SkipReason.EMPTY_TEXT: 1,
        SkipReason.FORWARDED: 1,
        SkipReason.UNREADABLE: 1,
    }
    assert report.dropped_after_redaction == 1
    assert report.conversations == 2
    assert report.turns == 6
    assert report.candidates == 3
    assert report.duplicates == 0
    assert report.dialogues_written == 3
    assert report.redactions == {
        RedactionKind.NAME: 1,
        RedactionKind.PHONE: 1,
        RedactionKind.EMAIL: 1,
    }


def test_a_message_made_only_of_tokens_is_dropped_and_the_turns_stay_valid() -> None:
    items = chat(
        (0, OTHER, "question"),
        (1, OWNER, "+7 900 123 45 67"),
        (2, OWNER, "real answer"),
    )

    draft = build_draft(_data(*items), ConvertOptions(me=OWNER))

    assert [turn.text for turn in draft.dialogues[0]] == ["question", "real answer"]
    assert draft.report.dropped_after_redaction == 1


def test_a_dialogue_whose_owner_turn_is_only_tokens_disappears() -> None:
    items = chat((0, OTHER, "question"), (1, OWNER, "https://example.org/x"))

    with pytest.raises(ChatExportError, match="no dialogue"):
        build_draft(_data(*items), ConvertOptions(me=OWNER))


def test_a_message_that_becomes_only_tokens_does_not_join_its_neighbours() -> None:
    items = chat(
        (0, OTHER, "first"),
        (1, OWNER, "reply one"),
        (2, OTHER, "@someone"),
        (3, OTHER, "second"),
        (4, OWNER, "reply two"),
    )

    draft = build_draft(_data(*items), ConvertOptions(me=OWNER, window=2))

    assert [[turn.text for turn in item] for item in draft.dialogues] == [
        ["first", "reply one"],
        ["second", "reply two"],
    ]


def test_cyrillic_senders_are_redacted_automatically() -> None:
    items = chat(
        (0, CYRILLIC_OTHER, "Привет, это Лиана"),
        (1, OWNER, "привет, ЛИАНА, как дела?"),
    )

    draft = build_draft(_data(*items), ConvertOptions(me=OWNER))

    assert [turn.text for turn in draft.dialogues[0]] == [
        "Привет, это <name>",
        "привет, <name>, как дела?",
    ]


def test_explicit_redact_words_reach_the_output() -> None:
    items = chat((0, OTHER, "hey Olgunchik"), (1, OWNER, "hello Olgunchik"))

    draft = build_draft(_data(*items), ConvertOptions(me=OWNER, redact_words=("olgunchik",)))

    assert [turn.text for turn in draft.dialogues[0]] == ["hey <name>", "hello <name>"]
    assert draft.report.redactions == {RedactionKind.NAME: 2}


def test_duplicates_are_dropped_before_sampling_and_counted() -> None:
    repeated = [(0, OTHER, "same question"), (1, OWNER, "same answer")]
    lines: list[tuple[float, str, str]] = []
    for day in range(4):
        lines.extend((minutes + day * 1000, who, text) for minutes, who, text in repeated)

    draft = build_draft(_data(*chat(*lines)), ConvertOptions(me=OWNER))

    assert draft.report.candidates == 4
    assert draft.report.duplicates == 3
    assert draft.report.dialogues_written == 1


def test_sampling_respects_max_and_is_deterministic() -> None:
    lines: list[tuple[float, str, str]] = []
    for index in range(30):
        lines.append((index * 1000, OTHER, f"question {index}"))
        lines.append((index * 1000 + 1, OWNER, f"answer {index}"))
    options = ConvertOptions(me=OWNER, max_dialogues=7)

    first = build_draft(_data(*chat(*lines)), options)
    second = build_draft(_data(*chat(*lines)), options)

    assert first.dialogues == second.dialogues
    assert len(first.dialogues) == 7
    assert first.report.candidates == 30
    answers = [int(item[-1].text.split()[1]) for item in first.dialogues]
    assert answers == sorted(answers)
    assert answers[0] < 5
    assert answers[-1] > 24


@pytest.mark.parametrize("value", [0, -1, 501, 10_000])
def test_max_outside_the_file_limit_is_refused(value: int) -> None:
    with pytest.raises(ChatExportError, match="--max"):
        validate_options(ConvertOptions(me=OWNER, max_dialogues=value))


def test_max_at_the_file_limit_is_accepted() -> None:
    validate_options(ConvertOptions(me=OWNER, max_dialogues=500))


@pytest.mark.parametrize("value", [0, 1, 51, -3])
def test_window_outside_its_range_is_refused(value: int) -> None:
    with pytest.raises(ChatExportError, match="--window"):
        validate_options(ConvertOptions(me=OWNER, window=value))


@pytest.mark.parametrize("value", [0.0, -1.0, float("nan"), float("inf"), 1e9])
def test_gap_hours_outside_its_range_is_refused(value: float) -> None:
    with pytest.raises(ChatExportError, match="--gap-hours"):
        validate_options(ConvertOptions(me=OWNER, gap_hours=value))


@pytest.mark.parametrize("words", [("",), ("123",), ("ok", "---")])
def test_redact_words_without_letters_are_refused(words: tuple[str, ...]) -> None:
    with pytest.raises(ChatExportError, match="--redact"):
        validate_options(ConvertOptions(me=OWNER, redact_words=words))


def test_a_blank_owner_is_refused() -> None:
    with pytest.raises(ChatExportError, match="--me"):
        validate_options(ConvertOptions(me="  "))


def test_tags_are_validated_like_the_import_does() -> None:
    assert normalize_tags(["Flirty", "flirty", "casual_1"]) == ("casual_1", "flirty")
    assert normalize_tags([]) == ()
    for bad in (["bad tag"], ["x" * 33], ["a", "b", "c", "d", "e", "f"], ["ok", "!"]):
        with pytest.raises(ChatExportError, match="--tags is invalid") as caught:
            normalize_tags(bad)
        assert "bad tag" not in str(caught.value)


def test_the_draft_has_the_import_format_and_the_tags(tmp_path: Path) -> None:
    draft = build_draft(_data(*_simple_chat()), ConvertOptions(me=OWNER))

    content = render_draft(draft.dialogues, ("flirty",))

    document = json.loads(content)
    assert document["version"] == 1
    assert document["dialogues"][0]["tags"] == ["flirty"]
    assert document["dialogues"][0]["turns"][0] == {"role": "them", "text": "how was your week?"}
    path = tmp_path / "draft.json"
    path.write_text(content, encoding="utf-8")
    assert len(parse_examples(read_import_file(path))) == len(draft.dialogues)


def test_no_tags_means_generic() -> None:
    draft = build_draft(_data(*_simple_chat()), ConvertOptions(me=OWNER))

    parsed = json.loads(render_draft(draft.dialogues, ()))

    assert all(not item["tags"] for item in parsed["dialogues"])


def test_output_inside_data_is_accepted(tmp_path: Path) -> None:
    target = resolve_output(
        Path("data/style-examples.json"), allow_any_path=False, working_directory=tmp_path
    )

    assert target == (tmp_path / "data" / "style-examples.json").resolve()


@pytest.mark.parametrize(
    "out",
    [
        "style-examples.json",
        "data",
        "data/",
        "other/style-examples.json",
        "data/../style-examples.json",
        "../data/style-examples.json",
        "database/style-examples.json",
    ],
)
def test_output_outside_data_is_refused_without_the_flag(tmp_path: Path, out: str) -> None:
    with pytest.raises(ChatExportError, match="--out must be inside the data/ folder"):
        resolve_output(Path(out), allow_any_path=False, working_directory=tmp_path)


def test_an_absolute_output_outside_data_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ChatExportError, match="--out must be inside"):
        resolve_output(
            tmp_path / "elsewhere.json", allow_any_path=False, working_directory=tmp_path
        )


def test_a_symlink_inside_data_cannot_escape_it(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "link").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ChatExportError, match="--out must be inside"):
        resolve_output(Path("data/link/x.json"), allow_any_path=False, working_directory=tmp_path)


def test_the_flag_allows_any_path(tmp_path: Path) -> None:
    target = resolve_output(Path("elsewhere.json"), allow_any_path=True, working_directory=tmp_path)

    assert target == (tmp_path / "elsewhere.json").resolve()


def test_an_existing_output_is_refused_without_force(tmp_path: Path) -> None:
    target = tmp_path / "out.json"
    target.write_text("old", encoding="utf-8")

    with pytest.raises(ChatExportError, match="already exists"):
        write_draft(target, "new", force=False)

    assert target.read_text(encoding="utf-8") == "old"


def test_an_output_directory_is_refused_even_with_force(tmp_path: Path) -> None:
    with pytest.raises(ChatExportError, match="is a directory"):
        check_target(tmp_path, force=True)


def _valid_content() -> str:
    return render_draft(build_draft(_data(*_simple_chat()), ConvertOptions(me=OWNER)).dialogues, ())


def test_force_replaces_an_existing_output(tmp_path: Path) -> None:
    target = tmp_path / "out.json"
    target.write_text("old", encoding="utf-8")

    write_draft(target, _valid_content(), force=True)

    assert json.loads(target.read_text(encoding="utf-8"))["version"] == 1
    assert [path.name for path in tmp_path.iterdir()] == ["out.json"]


def test_the_output_is_created_with_private_permissions(tmp_path: Path) -> None:
    target = tmp_path / "data" / "out.json"

    write_draft(target, _valid_content(), force=False)

    assert target.stat().st_mode & 0o777 == 0o600


def test_a_failing_rename_leaves_no_file_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(source: object, destination: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", refuse)

    with pytest.raises(ChatExportError, match="cannot write the draft: OSError"):
        write_draft(tmp_path / "out.json", _valid_content(), force=False)

    assert list(tmp_path.iterdir()) == []


def test_a_draft_the_validators_refuse_is_never_published(tmp_path: Path) -> None:
    broken = json.dumps({"version": 1, "dialogues": [{"turns": [{"role": "them", "text": "x"}]}]})

    with pytest.raises(ContentValidationError):
        write_draft(tmp_path / "out.json", broken, force=False)

    assert list(tmp_path.iterdir()) == []


def test_a_validator_failure_after_a_partial_write_leaves_the_old_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "out.json"
    target.write_text("old", encoding="utf-8")

    def reject(raw: str) -> object:
        raise ChatExportError("rejected")

    monkeypatch.setattr(chat_export_converter, "parse_examples", reject)

    with pytest.raises(ChatExportError, match="rejected"):
        write_draft(target, _valid_content(), force=True)

    assert target.read_text(encoding="utf-8") == "old"
    assert [path.name for path in tmp_path.iterdir()] == ["out.json"]


def test_a_full_conversion_writes_a_file_the_import_validators_accept(tmp_path: Path) -> None:
    source = write_export(tmp_path / "result.json", *_simple_chat())

    report = convert_chat_export(
        source,
        Path("data/draft.json"),
        ConvertOptions(me=OWNER, tags=("Flirty",)),
        working_directory=tmp_path,
    )

    written = tmp_path / "data" / "draft.json"
    parsed = parse_examples(read_import_file(written))
    assert report.dialogues_written == len(parsed) == 2
    assert all(example.tags == ["flirty"] for example in parsed)


def test_a_failed_conversion_writes_nothing(tmp_path: Path) -> None:
    source = write_export(tmp_path / "result.json", *_simple_chat())

    with pytest.raises(ChatExportError):
        convert_chat_export(
            source, Path("data/draft.json"), ConvertOptions(me="Nobody"), working_directory=tmp_path
        )

    assert not (tmp_path / "data").exists()


def test_the_output_guard_runs_before_the_input_is_read(tmp_path: Path) -> None:
    with pytest.raises(ChatExportError, match="--out must be inside"):
        convert_chat_export(
            tmp_path / "missing.json",
            Path("draft.json"),
            ConvertOptions(me=OWNER),
            working_directory=tmp_path,
        )


@pytest.mark.parametrize("module", CONVERTER_MODULES)
def test_the_converter_needs_no_settings_database_or_telegram(module: str) -> None:
    path = Path(chat_export_converter.__file__).with_name(f"{module}.py")
    imported: list[str] = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.append(node.module)

    assert not [name for name in imported if name.startswith(FORBIDDEN_IMPORT_PREFIXES)]


def _id_chat() -> list[dict[str, object]]:
    return [
        message(0, None, "question one", from_id=OTHER_ID),
        message(1, BACKTICK, "answer one", from_id=OWNER_ID),
        message(2, OTHER, "question two", from_id=OTHER_ID),
        message(3, None, "answer two", from_id=OWNER_ID),
    ]


def test_the_owner_is_found_by_from_id_whatever_the_name() -> None:
    data = read_messages(_id_chat(), by_id=True)

    draft = build_draft(data, ConvertOptions(me_id=OWNER_ID))

    assert [[turn.role.value for turn in item] for item in draft.dialogues] == [
        ["them", "me"],
        ["them", "me", "them", "me"],
    ]
    assert draft.dialogues[1][-1].text == "answer two"


def test_a_from_id_that_matches_nothing_reports_only_a_count() -> None:
    data = read_messages(_id_chat(), by_id=True)

    with pytest.raises(ChatExportError) as caught:
        build_draft(data, ConvertOptions(me_id="user42"))

    text = str(caught.value)
    assert "2 sender id(s)" in text
    for identifier in (OWNER_ID, OTHER_ID, "user42", "1000000001"):
        assert identifier not in text


def test_a_from_id_is_matched_exactly() -> None:
    data = read_messages(_id_chat(), by_id=True)

    for wrong in (OWNER_ID.upper(), OWNER_ID + "0", OWNER_ID[:-1], OWNER):
        with pytest.raises(ChatExportError, match="matches none"):
            build_draft(data, ConvertOptions(me_id=wrong))


def test_a_name_is_not_used_to_find_the_owner_in_id_mode() -> None:
    data = read_messages(_id_chat(), by_id=True)

    with pytest.raises(ChatExportError, match="--me matches none"):
        build_draft(data, ConvertOptions(me=OWNER))


@pytest.mark.parametrize(
    "options",
    [
        ConvertOptions(),
        ConvertOptions(me=OWNER, me_id=OWNER_ID),
        ConvertOptions(me="", me_id=""),
    ],
)
def test_exactly_one_of_me_and_me_id_is_required(options: ConvertOptions) -> None:
    with pytest.raises(ChatExportError, match="give exactly one of --me and --me-id"):
        validate_options(options)


def test_one_of_me_and_me_id_is_enough() -> None:
    validate_options(ConvertOptions(me=OWNER))
    validate_options(ConvertOptions(me_id=OWNER_ID))


@pytest.mark.parametrize("options", [ConvertOptions(me=" "), ConvertOptions(me_id="")])
def test_a_blank_owner_option_is_refused(options: ConvertOptions) -> None:
    with pytest.raises(ChatExportError, match="must not be empty"):
        validate_options(options)


def test_a_backtick_name_works_as_the_owner_and_in_the_redaction_step() -> None:
    items = [
        message(0, OTHER, "see you ` there, call me`", from_id=OTHER_ID),
        message(1, BACKTICK, "ok ` fine `` .", from_id=OWNER_ID),
    ]

    draft = build_draft(_data(*items), ConvertOptions(me=BACKTICK))

    assert [turn.text for turn in draft.dialogues[0]] == [
        "see you ` there, call me`",
        "ok ` fine `` .",
    ]
    assert draft.report.redactions == {}


@pytest.mark.parametrize("name", ["`", "-", "…", "!?", "12", "ab", "_", "😀"])
def test_a_sender_name_without_a_long_word_makes_no_redaction_pattern(name: str) -> None:
    redactor = Redactor([name, "Zed Quark"])
    text = "a`b - … !? 12 ab _ 😀 and Zed"

    result = redactor.redact(text)

    assert result.text == "a`b - … !? 12 ab _ 😀 and <name>"
    assert result.counts == {RedactionKind.NAME: 1}


def test_only_punctuation_names_give_no_name_pattern_at_all() -> None:
    result = Redactor(["`", "-", ""]).redact("text with ` and - and nothing else")

    assert result.text == "text with ` and - and nothing else"
    assert result.counts == {}
    assert name_words(["`", "-", ""], 3) == []
