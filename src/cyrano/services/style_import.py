"""Import of the persona and the style examples from local files into storage."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from cyrano.services.repositories import PersonaRepository, StyleExampleRepository
from cyrano.services.style_files import parse_examples, parse_persona, read_import_file

logger = logging.getLogger("cyrano.services.style_import")


@dataclass(frozen=True)
class PersonaImportResult:
    characters: int


@dataclass(frozen=True)
class ExamplesImportResult:
    added: int
    skipped: int
    total: int


class PersonaImporter:
    def __init__(self, repository: PersonaRepository) -> None:
        self._repository = repository

    async def import_file(self, path: Path) -> PersonaImportResult:
        persona = parse_persona(read_import_file(path))
        stored = await self._repository.replace(persona.text)
        logger.info("persona imported", extra={"characters": len(stored.text)})
        return PersonaImportResult(characters=len(stored.text))


class ExamplesImporter:
    def __init__(self, repository: StyleExampleRepository) -> None:
        self._repository = repository

    async def import_file(self, path: Path, *, replace: bool = False) -> ExamplesImportResult:
        examples = parse_examples(read_import_file(path))
        outcome = await self._repository.add_many(examples, replace=replace)
        total = await self._repository.count()
        logger.info(
            "style examples imported",
            extra={
                "added": outcome.added,
                "skipped": outcome.skipped,
                "total": total,
                "replace": replace,
            },
        )
        return ExamplesImportResult(added=outcome.added, skipped=outcome.skipped, total=total)
