from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path

import pytest

from cyrano.db.repositories import SqlStyleExampleRepository
from cyrano.main import main
from cyrano.services import chat_export_reader
from cyrano.services.style_import import ExamplesImporter
from tests.chat_export_data import (
    BACKTICK,
    CYRILLIC_OTHER,
    OTHER,
    OTHER_ID,
    OWNER,
    OWNER_ID,
    chat,
    message,
    sample_shaped_message,
    write_export,
)

SECRET_TEXTS = (
    "zq-secret-question",
    "zq-secret-answer",
    "zq-follow-up",
    "7 900 123 45 67",
    "79001234567",
    "zq.person@example.org",
    "@zq_handle",
    "zq-nickname",
)
PERSONAL_NAMES = ("Zed", "Quark", "Olga", "Test", "Лиана", "Вербина")


def _conversation() -> list[dict[str, object]]:
    return chat(
        (0, OTHER, "zq-secret-question for Zed, call +7 900 123 45 67"),
        (1, OWNER, "zq-secret-answer, write zq.person@example.org or @zq_handle, Olga"),
        (2, CYRILLIC_OTHER, "zq-follow-up Лиана zq-nickname"),
        (3, OWNER, "reply to ВЕРБИНА at 79001234567"),
    )


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _run(*arguments: str) -> int:
    return main(["examples", "convert", *arguments])


def test_a_full_run_writes_the_draft_and_prints_counts_only(
    workdir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_export(workdir / "result.json", *_conversation())

    code = _run("result.json", "--me", OWNER, "--tags", "flirty,casual", "--redact", "zq-nickname")

    captured = capsys.readouterr()
    assert code == 0
    lines = captured.out.strip().splitlines()
    assert "messages read: 4" in lines
    assert "messages kept: 4" in lines
    assert "dialogues written: 2" in lines
    assert "redacted, email: 1" in lines
    assert "redacted, phone: 2" in lines
    assert "redacted, username: 1" in lines
    assert "output: data/style-examples.json" in lines
    assert "review the draft before importing" in lines[-1]
    assert (workdir / "data" / "style-examples.json").is_file()
    document = json.loads((workdir / "data" / "style-examples.json").read_text(encoding="utf-8"))
    assert document["dialogues"][0]["tags"] == ["casual", "flirty"]


def test_the_output_never_contains_the_secrets_names_or_numbers(
    workdir: Path, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    logging.getLogger().setLevel(logging.DEBUG)
    write_export(workdir / "result.json", *_conversation())

    code = _run("result.json", "--me", OWNER, "--redact", "zq-nickname")

    captured = capsys.readouterr()
    everything = captured.out + captured.err + caplog.text
    draft = (workdir / "data" / "style-examples.json").read_text(encoding="utf-8")
    assert code == 0
    for secret in (*SECRET_TEXTS[2:], *PERSONAL_NAMES):
        assert secret not in draft
    for secret in (*SECRET_TEXTS, *PERSONAL_NAMES):
        assert secret.lower() not in everything.lower()
    assert any(record.name.endswith("chat_export_converter") for record in caplog.records)


def test_errors_never_echo_names_or_texts(
    workdir: Path, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    write_export(workdir / "result.json", *_conversation())

    code = _run("result.json", "--me", "Nobody At All")

    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert "3 sender(s)" in captured.err
    for secret in (*SECRET_TEXTS, *PERSONAL_NAMES):
        assert secret.lower() not in (captured.err + caplog.text).lower()


def test_a_missing_input_exits_with_one_and_no_traceback(
    workdir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = _run("missing.json", "--me", OWNER)

    captured = capsys.readouterr()
    assert code == 1
    assert "not found" in captured.err
    assert "Traceback" not in captured.err


def test_a_directory_input_exits_with_one(
    workdir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (workdir / "folder").mkdir()

    assert _run("folder", "--me", OWNER) == 1
    assert "is a directory" in capsys.readouterr().err


def test_an_html_input_exits_with_one_and_names_the_fix(
    workdir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (workdir / "messages.html").write_text("<html></html>", encoding="utf-8")

    assert _run("messages.html", "--me", OWNER) == 1
    assert "HTML exports are not supported" in capsys.readouterr().err


def test_invalid_json_exits_with_one(workdir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (workdir / "result.json").write_text("{not json", encoding="utf-8")

    assert _run("result.json", "--me", OWNER) == 1
    assert "not valid JSON" in capsys.readouterr().err


def test_an_account_wide_export_exits_with_one(
    workdir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (workdir / "result.json").write_text(json.dumps({"chats": {"list": []}}), encoding="utf-8")

    assert _run("result.json", "--me", OWNER) == 1
    assert "export one chat" in capsys.readouterr().err


def test_a_json_without_messages_exits_with_one(
    workdir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (workdir / "result.json").write_text(json.dumps({"name": "x"}), encoding="utf-8")

    assert _run("result.json", "--me", OWNER) == 1
    assert "no messages" in capsys.readouterr().err


def test_an_oversized_input_exits_with_one(
    workdir: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    write_export(workdir / "result.json", *_conversation())
    monkeypatch.setattr(chat_export_reader, "MAX_EXPORT_BYTES", 10)

    assert _run("result.json", "--me", OWNER) == 1
    assert "larger than 10 bytes" in capsys.readouterr().err


def test_max_above_five_hundred_is_refused(
    workdir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_export(workdir / "result.json", *_conversation())

    assert _run("result.json", "--me", OWNER, "--max", "501") == 1
    assert "--max must be from 1 to 500" in capsys.readouterr().err
    assert not (workdir / "data").exists()


def test_an_output_outside_data_is_refused_unless_allowed(
    workdir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_export(workdir / "result.json", *_conversation())

    refused = _run("result.json", "--me", OWNER, "--out", "draft.json")
    err = capsys.readouterr().err
    allowed = _run("result.json", "--me", OWNER, "--out", "draft.json", "--allow-any-path")

    assert refused == 1
    assert "--out must be inside the data/ folder" in err
    assert allowed == 0
    assert (workdir / "draft.json").is_file()


def test_an_existing_output_needs_force(workdir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_export(workdir / "result.json", *_conversation())
    assert _run("result.json", "--me", OWNER) == 0
    capsys.readouterr()

    again = _run("result.json", "--me", OWNER)
    err = capsys.readouterr().err
    forced = _run("result.json", "--me", OWNER, "--force")

    assert again == 1
    assert "already exists" in err
    assert forced == 0


def test_redact_takes_several_words(workdir: Path) -> None:
    write_export(
        workdir / "result.json",
        message(0, OTHER, "hey Lulu and Bobo"),
        message(1, OWNER, "hi Lulu"),
    )

    assert _run("result.json", "--me", OWNER, "--redact", "lulu", "bobo") == 0
    text = (workdir / "data" / "style-examples.json").read_text(encoding="utf-8")
    assert "Lulu" not in text
    assert "Bobo" not in text


def test_names_with_spaces_and_a_cyrillic_owner_work(workdir: Path) -> None:
    write_export(
        workdir / "chat export.json",
        message(0, "Анна Иванова", "привет, как дела?"),
        message(1, "Пётр Сидоров", "всё хорошо, Анна"),
    )

    assert _run("chat export.json", "--me", "Пётр Сидоров", "--out", "data/my draft.json") == 0
    text = (workdir / "data" / "my draft.json").read_text(encoding="utf-8")
    assert "Анна" not in text
    assert "<name>" in text


def test_a_conversion_runs_with_no_settings_env_file_or_database(workdir: Path) -> None:
    write_export(workdir / "result.json", *_conversation())

    finished = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "cyrano", "examples", "convert", "result.json", "--me", OWNER],
        cwd=workdir,
        env={},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert finished.returncode == 0, finished.stderr
    assert "dialogues written: 2" in finished.stdout
    assert (workdir / "data" / "style-examples.json").is_file()
    assert finished.stderr == ""


def test_the_other_examples_commands_still_need_the_settings(
    workdir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["examples", "stats"])

    assert code == 2
    assert "DATABASE_URL" in capsys.readouterr().err


async def test_the_written_draft_imports_through_the_real_importer(
    workdir: Path, style_examples: SqlStyleExampleRepository
) -> None:
    write_export(workdir / "result.json", *_conversation())
    assert _run("result.json", "--me", OWNER, "--tags", "flirty") == 0

    result = await ExamplesImporter(style_examples).import_file(
        workdir / "data" / "style-examples.json", replace=False
    )

    stored = await style_examples.list_all()
    assert (result.added, result.skipped) == (2, 0)
    assert all(example.tags == ["flirty"] for example in stored)
    assert {turn.role.value for example in stored for turn in example.turns} == {"me", "them"}


def _id_export(workdir: Path) -> Path:
    return write_export(
        workdir / "result.json",
        message(0, None, "zq-secret-question", from_id=OTHER_ID),
        message(1, BACKTICK, "zq-secret-answer", from_id=OWNER_ID),
    )


def test_me_id_finds_the_owner_when_the_name_is_awkward_or_null(
    workdir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _id_export(workdir)

    code = _run("result.json", "--me-id", OWNER_ID)

    assert code == 0
    assert "dialogues written: 1" in capsys.readouterr().out
    document = json.loads((workdir / "data" / "style-examples.json").read_text(encoding="utf-8"))
    assert [turn["role"] for turn in document["dialogues"][0]["turns"]] == ["them", "me"]


def test_a_backtick_name_can_be_passed_as_me(workdir: Path) -> None:
    write_export(
        workdir / "result.json",
        message(0, OTHER, "zq-secret-question", from_id=OTHER_ID),
        message(1, BACKTICK, "zq-secret-answer", from_id=OWNER_ID),
    )

    assert _run("result.json", "--me", BACKTICK) == 0


def test_me_id_that_matches_nothing_reports_a_count_and_no_ids(
    workdir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _id_export(workdir)

    code = _run("result.json", "--me-id", "user42")

    captured = capsys.readouterr()
    assert code == 1
    assert "2 sender id(s)" in captured.err
    for identifier in (OWNER_ID, OTHER_ID, "user42"):
        assert identifier not in captured.out + captured.err
    assert not (workdir / "data").exists()


@pytest.mark.parametrize(
    "owner_arguments", [[], ["--me", OWNER, "--me-id", OWNER_ID]], ids=["neither", "both"]
)
def test_exactly_one_of_me_and_me_id_is_required(
    workdir: Path, capsys: pytest.CaptureFixture[str], owner_arguments: list[str]
) -> None:
    _id_export(workdir)

    code = _run("result.json", *owner_arguments)

    captured = capsys.readouterr()
    assert code == 1
    assert "give exactly one of --me and --me-id" in captured.err
    assert "Traceback" not in captured.err
    assert not (workdir / "data").exists()


def test_a_message_shaped_like_a_real_export_converts(workdir: Path) -> None:
    reply = sample_shaped_message()
    question = {**reply, "id": 2, "from": OTHER, "from_id": OTHER_ID, "date_unixtime": "1789159301"}
    question["text"] = "hi there"
    (workdir / "result.json").write_text(
        json.dumps({"name": "x", "type": "personal_chat", "id": 1, "messages": [reply, question]}),
        encoding="utf-8",
    )

    assert _run("result.json", "--me-id", OWNER_ID) == 0
    document = json.loads((workdir / "data" / "style-examples.json").read_text(encoding="utf-8"))
    assert [turn["text"] for turn in document["dialogues"][0]["turns"]] == ["hi there", "hello"]
