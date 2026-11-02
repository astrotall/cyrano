from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from cyrano.db.repositories import SqlPersonaRepository, SqlStyleExampleRepository
from cyrano.domain.errors import ContentValidationError, ImportFileError
from cyrano.services.style_import import ExamplesImporter, PersonaImporter
from tests.style_samples import SAMPLE_EXAMPLES, SAMPLE_PERSONA, sample_texts

MARKER = "zq-private-marker"


def _write_examples(path: Path, replies: list[str], **dialogue: object) -> Path:
    dialogues = [
        {
            "turns": [
                {"role": "them", "text": f"question for {reply}"},
                {"role": "me", "text": reply},
            ],
            **dialogue,
        }
        for reply in replies
    ]
    path.write_text(json.dumps({"version": 1, "dialogues": dialogues}), encoding="utf-8")
    return path


async def _replies(repository: SqlStyleExampleRepository) -> set[str]:
    return {example.turns[-1].text for example in await repository.list_all()}


async def test_the_first_import_adds_everything(
    style_examples: SqlStyleExampleRepository, tmp_path: Path
) -> None:
    importer = ExamplesImporter(style_examples)

    result = await importer.import_file(_write_examples(tmp_path / "e.json", ["a", "b", "c"]))

    assert (result.added, result.skipped, result.total) == (3, 0, 3)


async def test_importing_the_same_file_again_skips_every_dialogue(
    style_examples: SqlStyleExampleRepository, tmp_path: Path
) -> None:
    importer = ExamplesImporter(style_examples)
    path = _write_examples(tmp_path / "e.json", ["a", "b"])
    await importer.import_file(path)

    again = await importer.import_file(path)

    assert (again.added, again.skipped, again.total) == (0, 2, 2)


async def test_a_partly_new_file_adds_only_the_new_dialogues(
    style_examples: SqlStyleExampleRepository, tmp_path: Path
) -> None:
    importer = ExamplesImporter(style_examples)
    await importer.import_file(_write_examples(tmp_path / "one.json", ["a", "b"]))

    result = await importer.import_file(_write_examples(tmp_path / "two.json", ["b", "c"]))

    assert (result.added, result.skipped, result.total) == (1, 1, 3)


async def test_an_invalid_file_stores_nothing(
    style_examples: SqlStyleExampleRepository, tmp_path: Path
) -> None:
    path = _write_examples(tmp_path / "e.json", ["fine"])
    document = json.loads(path.read_text(encoding="utf-8"))
    document["dialogues"].append({"turns": [{"role": "them", "text": MARKER}]})
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ContentValidationError) as caught:
        await ExamplesImporter(style_examples).import_file(path)

    assert MARKER not in str(caught.value)
    assert await style_examples.count() == 0


async def test_a_missing_file_stores_nothing(
    style_examples: SqlStyleExampleRepository, tmp_path: Path
) -> None:
    with pytest.raises(ImportFileError):
        await ExamplesImporter(style_examples).import_file(tmp_path / "absent.json")

    assert await style_examples.count() == 0


async def test_replace_swaps_all_stored_examples(
    style_examples: SqlStyleExampleRepository, tmp_path: Path
) -> None:
    importer = ExamplesImporter(style_examples)
    await importer.import_file(_write_examples(tmp_path / "old.json", ["old-1", "old-2"]))

    result = await importer.import_file(
        _write_examples(tmp_path / "new.json", ["old-2", "new"]), replace=True
    )

    assert (result.added, result.skipped, result.total) == (2, 0, 2)
    assert await _replies(style_examples) == {"old-2", "new"}


async def test_an_invalid_file_with_replace_keeps_the_old_examples(
    style_examples: SqlStyleExampleRepository, tmp_path: Path
) -> None:
    importer = ExamplesImporter(style_examples)
    await importer.import_file(_write_examples(tmp_path / "old.json", ["keep"]))
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")

    with pytest.raises(ContentValidationError):
        await importer.import_file(broken, replace=True)

    assert await _replies(style_examples) == {"keep"}


async def test_nul_bytes_in_a_file_never_reach_the_database(
    style_examples: SqlStyleExampleRepository, tmp_path: Path
) -> None:
    path = _write_examples(tmp_path / "e.json", ["cle\x00an"])

    await ExamplesImporter(style_examples).import_file(path)

    assert await _replies(style_examples) == {"clean"}


async def test_the_first_persona_import_stores_the_cleaned_text(
    personas: SqlPersonaRepository, tmp_path: Path
) -> None:
    path = tmp_path / "persona.md"
    path.write_text("  I am\x00 here \n", encoding="utf-8")

    result = await PersonaImporter(personas).import_file(path)

    stored = await personas.get()
    assert stored is not None
    assert stored.text == "I am here"
    assert result.characters == len("I am here")


async def test_importing_a_persona_again_updates_text_and_time(
    personas: SqlPersonaRepository, tmp_path: Path
) -> None:
    importer = PersonaImporter(personas)
    path = tmp_path / "persona.md"
    path.write_text("first", encoding="utf-8")
    await importer.import_file(path)
    first = await personas.get()
    path.write_text("second version", encoding="utf-8")

    await importer.import_file(path)

    second = await personas.get()
    assert first is not None
    assert second is not None
    assert second.text == "second version"
    assert second.updated_at > first.updated_at


async def test_an_invalid_persona_keeps_the_old_one(
    personas: SqlPersonaRepository, tmp_path: Path
) -> None:
    importer = PersonaImporter(personas)
    good = tmp_path / "good.md"
    good.write_text("keep me", encoding="utf-8")
    await importer.import_file(good)
    empty = tmp_path / "empty.md"
    empty.write_text(" \n ", encoding="utf-8")

    with pytest.raises(ContentValidationError):
        await importer.import_file(empty)

    stored = await personas.get()
    assert stored is not None
    assert stored.text == "keep me"


async def test_a_full_import_leaves_no_persona_or_example_text_in_logs_or_errors(
    personas: SqlPersonaRepository,
    style_examples: SqlStyleExampleRepository,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    persona_importer = PersonaImporter(personas)
    examples_importer = ExamplesImporter(style_examples)
    broken = tmp_path / "broken.json"
    broken.write_text(
        json.dumps({"version": 2, "dialogues": [{"turns": [{"role": "them", "text": "x"}]}]}),
        encoding="utf-8",
    )
    messages: list[str] = []

    await persona_importer.import_file(SAMPLE_PERSONA)
    await examples_importer.import_file(SAMPLE_EXAMPLES)
    await examples_importer.import_file(SAMPLE_EXAMPLES)
    await examples_importer.import_file(SAMPLE_EXAMPLES, replace=True)
    for failing in (broken, tmp_path / "absent.json"):
        with pytest.raises((ContentValidationError, ImportFileError)) as caught:
            await examples_importer.import_file(failing)
        messages.append(str(caught.value))

    captured = (
        caplog.text
        + "\n".join(messages)
        + "\n".join(record.getMessage() for record in caplog.records)
    )
    assert caplog.records
    texts = sample_texts()
    assert len(texts) > 10
    for text in texts:
        assert text not in captured
