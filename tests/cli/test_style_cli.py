from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from sqlalchemy.engine import URL

from cyrano.db.repositories import SqlPersonaRepository, SqlStyleExampleRepository
from cyrano.main import main
from cyrano.services.persona_service import PersonaService
from cyrano.services.style_import import ExamplesImporter, PersonaImporter
from cyrano.style_cli import REPLACE_NEEDS_CONFIRMATION, StyleCommands, StyleRequest
from tests.style_samples import SAMPLE_EXAMPLES, SAMPLE_PERSONA, sample_texts

MARKER = "zq-private-marker"
PERSONA_TEXT = f"{MARKER} persona body that must never be printed"


@pytest.fixture
def commands(
    personas: SqlPersonaRepository, style_examples: SqlStyleExampleRepository
) -> StyleCommands:
    return StyleCommands(
        PersonaImporter(personas),
        ExamplesImporter(style_examples),
        PersonaService(personas),
        style_examples,
    )


def _examples_file(path: Path, replies: list[str]) -> Path:
    dialogues = [
        {
            "tags": ["flirty"] if index % 2 else [],
            "turns": [
                {"role": "them", "text": f"{MARKER} question {reply}"},
                {"role": "me", "text": f"{MARKER} {reply}"},
            ],
        }
        for index, reply in enumerate(replies)
    ]
    path.write_text(json.dumps({"version": 1, "dialogues": dialogues}), encoding="utf-8")
    return path


def _request(
    command: str,
    action: str,
    *,
    path: Path | None = None,
    replace: bool = False,
    confirmed: bool = False,
) -> StyleRequest:
    return StyleRequest(command, action, path, replace, confirmed)


async def _import_examples(commands: StyleCommands, path: Path) -> int:
    return await commands.run(_request("examples", "import", path=path))


async def test_persona_import_reports_the_length_only(
    commands: StyleCommands, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "persona.md"
    path.write_text(PERSONA_TEXT, encoding="utf-8")

    code = await commands.run(_request("persona", "import", path=path))

    captured = capsys.readouterr()
    assert code == 0
    assert captured.out.strip() == f"persona imported: {len(PERSONA_TEXT)} characters"
    assert MARKER not in captured.out + captured.err


async def test_persona_show_prints_length_and_time_but_never_the_text(
    commands: StyleCommands,
    personas: SqlPersonaRepository,
    capsys: pytest.CaptureFixture[str],
) -> None:
    stored = await personas.replace(PERSONA_TEXT)

    code = await commands.run(_request("persona", "show"))

    captured = capsys.readouterr()
    assert code == 0
    assert captured.out.strip() == (
        f"persona: {len(PERSONA_TEXT)} characters, updated {stored.updated_at.isoformat()}"
    )
    assert MARKER not in captured.out + captured.err


async def test_persona_show_without_a_persona_is_an_error(
    commands: StyleCommands, capsys: pytest.CaptureFixture[str]
) -> None:
    code = await commands.run(_request("persona", "show"))

    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert "no persona stored" in captured.err


async def test_a_persona_problem_exits_with_one_and_a_readable_message(
    commands: StyleCommands, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "persona.md"
    path.write_text("   ", encoding="utf-8")

    code = await commands.run(_request("persona", "import", path=path))

    captured = capsys.readouterr()
    assert code == 1
    assert "persona file is invalid" in captured.err
    assert "(file): empty" in captured.err


async def test_examples_import_prints_counts_and_a_second_run_skips(
    commands: StyleCommands, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _examples_file(tmp_path / "e.json", ["a", "b", "c"])

    first = await commands.run(_request("examples", "import", path=path))
    second = await commands.run(_request("examples", "import", path=path))

    lines = capsys.readouterr().out.strip().splitlines()
    assert (first, second) == (0, 0)
    assert lines == [
        "examples imported: added 3, skipped 0 (already stored), total 3",
        "examples imported: added 0, skipped 3 (already stored), total 3",
    ]


async def test_replace_without_confirmation_refuses_and_changes_nothing(
    commands: StyleCommands,
    style_examples: SqlStyleExampleRepository,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    await commands.run(
        _request("examples", "import", path=_examples_file(tmp_path / "a.json", ["old"]))
    )
    capsys.readouterr()

    code = await commands.run(
        _request(
            "examples", "import", path=_examples_file(tmp_path / "b.json", ["new"]), replace=True
        )
    )

    captured = capsys.readouterr()
    assert code == 1
    assert REPLACE_NEEDS_CONFIRMATION in captured.err
    assert captured.out == ""
    replies = {example.turns[-1].text for example in await style_examples.list_all()}
    assert replies == {f"{MARKER} old"}


async def test_replace_with_confirmation_swaps_the_examples(
    commands: StyleCommands,
    style_examples: SqlStyleExampleRepository,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    await commands.run(
        _request("examples", "import", path=_examples_file(tmp_path / "a.json", ["old"]))
    )

    code = await commands.run(
        _request(
            "examples",
            "import",
            path=_examples_file(tmp_path / "b.json", ["new"]),
            replace=True,
            confirmed=True,
        )
    )

    assert code == 0
    assert "added 1, skipped 0" in capsys.readouterr().out
    replies = {example.turns[-1].text for example in await style_examples.list_all()}
    assert replies == {f"{MARKER} new"}


async def test_an_invalid_examples_file_exits_with_one_and_lists_paths_only(
    commands: StyleCommands,
    style_examples: SqlStyleExampleRepository,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "bad.json"
    path.write_text(
        json.dumps({"version": 1, "dialogues": [{"turns": [{"role": "them", "text": MARKER}]}]}),
        encoding="utf-8",
    )

    code = await commands.run(_request("examples", "import", path=path))

    captured = capsys.readouterr()
    assert code == 1
    assert "dialogues[0]: needs at least one 'me' turn" in captured.err
    assert MARKER not in captured.out + captured.err
    assert await style_examples.count() == 0


async def test_a_missing_examples_file_exits_with_one(
    commands: StyleCommands, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = await commands.run(_request("examples", "import", path=tmp_path / "nope.json"))

    assert code == 1
    assert "not a readable file" in capsys.readouterr().err


async def test_stats_print_counts_per_tag_and_no_texts(
    commands: StyleCommands, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    await commands.run(
        _request(
            "examples", "import", path=_examples_file(tmp_path / "e.json", ["a", "b", "c", "d"])
        )
    )
    capsys.readouterr()

    code = await commands.run(_request("examples", "stats"))

    captured = capsys.readouterr()
    assert code == 0
    assert captured.out.splitlines() == [
        "style examples: total 4",
        "generic (no tags): 2",
        "tag flirty: 2",
    ]
    assert MARKER not in captured.out + captured.err


async def test_stats_of_an_empty_store(
    commands: StyleCommands, capsys: pytest.CaptureFixture[str]
) -> None:
    code = await commands.run(_request("examples", "stats"))

    assert code == 0
    assert capsys.readouterr().out.splitlines() == [
        "style examples: total 0",
        "generic (no tags): 0",
    ]


async def test_the_sample_files_import_through_the_commands_without_printing_their_text(
    commands: StyleCommands, capsys: pytest.CaptureFixture[str]
) -> None:
    await commands.run(_request("persona", "import", path=SAMPLE_PERSONA))
    await commands.run(_request("examples", "import", path=SAMPLE_EXAMPLES))
    await commands.run(_request("persona", "show"))
    await commands.run(_request("examples", "stats"))

    captured = capsys.readouterr()
    for text in sample_texts():
        assert text not in captured.out + captured.err


async def test_an_unknown_request_is_an_error(
    commands: StyleCommands, capsys: pytest.CaptureFixture[str]
) -> None:
    code = await commands.run(_request("examples", "explode"))

    assert code == 1
    assert "unknown command" in capsys.readouterr().err


async def test_main_runs_the_whole_flow_against_the_database(
    migrated_url: URL,
    full_env: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    persona = tmp_path / "persona.md"
    persona.write_text(PERSONA_TEXT, encoding="utf-8")
    examples = _examples_file(tmp_path / "e.json", ["a", "b"])

    codes = [
        await asyncio.to_thread(main, argv)
        for argv in (
            ["persona", "import", str(persona)],
            ["persona", "show"],
            ["examples", "import", str(examples)],
            ["examples", "import", str(examples), "--replace"],
            ["examples", "import", str(examples), "--replace", "--yes"],
            ["examples", "stats"],
        )
    ]

    captured = capsys.readouterr()
    assert codes == [0, 0, 0, 1, 0, 0]
    assert MARKER not in captured.out + captured.err


def test_main_reports_an_unreachable_database_and_exits_with_one(
    full_env: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://nobody:pw-secret@127.0.0.1:1/none")

    code = main(["examples", "stats"])

    captured = capsys.readouterr()
    assert code == 1
    assert "storage failed" in captured.err
    assert "pw-secret" not in captured.out + captured.err


def test_main_with_an_invalid_configuration_still_exits_with_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)

    code = main(["persona", "show"])

    assert code == 2
    assert "Invalid configuration" in capsys.readouterr().err


def test_main_requires_a_file_argument(
    full_env: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as caught:
        main(["persona", "import"])

    assert caught.value.code == 2
