from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

from cyrano.domain.errors import DomainError
from cyrano.services.persona_service import PersonaService
from cyrano.services.repositories import StyleExampleRepository
from cyrano.services.style_examples import collect_example_stats
from cyrano.services.style_import import ExamplesImporter, PersonaImporter

PERSONA_COMMAND = "persona"
EXAMPLES_COMMAND = "examples"
IMPORT_ACTION = "import"
SHOW_ACTION = "show"
STATS_ACTION = "stats"
ACTION_DEST = "action"
EXIT_OK = 0
EXIT_ERROR = 1
REPLACE_NEEDS_CONFIRMATION = (
    "--replace deletes every stored style example before importing; add --yes to confirm"
)
NO_PERSONA_STORED = "no persona stored; import one with: python -m cyrano persona import FILE"
UNKNOWN_COMMAND = "unknown command"


@dataclass(frozen=True)
class StyleRequest:
    command: str
    action: str
    path: Path | None = None
    replace: bool = False
    confirmed: bool = False


def add_style_parsers(
    commands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> argparse._SubParsersAction[argparse.ArgumentParser]:
    persona = commands.add_parser(PERSONA_COMMAND, help="manage the owner persona text")
    persona_actions = persona.add_subparsers(dest=ACTION_DEST, required=True)
    persona_import = persona_actions.add_parser(
        IMPORT_ACTION, help="store the persona from a text or Markdown file"
    )
    persona_import.add_argument("file", type=Path)
    persona_actions.add_parser(SHOW_ACTION, help="print the length and the update time only")

    examples = commands.add_parser(EXAMPLES_COMMAND, help="manage the style examples")
    example_actions = examples.add_subparsers(dest=ACTION_DEST, required=True)
    examples_import = example_actions.add_parser(
        IMPORT_ACTION, help="store the dialogues of a JSON file"
    )
    examples_import.add_argument("file", type=Path)
    examples_import.add_argument(
        "--replace", action="store_true", help="delete all stored examples first"
    )
    examples_import.add_argument(
        "--yes", action="store_true", dest="confirmed", help="confirm --replace"
    )
    example_actions.add_parser(STATS_ACTION, help="print counts per tag, never texts")
    return example_actions


def style_request(args: argparse.Namespace) -> StyleRequest | None:
    if args.command not in {PERSONA_COMMAND, EXAMPLES_COMMAND}:
        return None
    return StyleRequest(
        command=args.command,
        action=getattr(args, ACTION_DEST),
        path=getattr(args, "file", None),
        replace=getattr(args, "replace", False),
        confirmed=getattr(args, "confirmed", False),
    )


class StyleCommands:
    def __init__(
        self,
        persona_importer: PersonaImporter,
        examples_importer: ExamplesImporter,
        persona_service: PersonaService,
        examples: StyleExampleRepository,
    ) -> None:
        self._persona_importer = persona_importer
        self._examples_importer = examples_importer
        self._persona_service = persona_service
        self._examples = examples

    async def run(self, request: StyleRequest) -> int:
        try:
            return await self._dispatch(request)
        except DomainError as exc:
            print(exc, file=sys.stderr)
            return EXIT_ERROR

    async def _dispatch(self, request: StyleRequest) -> int:
        key = (request.command, request.action)
        if key == (PERSONA_COMMAND, IMPORT_ACTION) and request.path is not None:
            return await self._import_persona(request.path)
        if key == (PERSONA_COMMAND, SHOW_ACTION):
            return await self._show_persona()
        if key == (EXAMPLES_COMMAND, IMPORT_ACTION) and request.path is not None:
            return await self._import_examples(request)
        if key == (EXAMPLES_COMMAND, STATS_ACTION):
            return await self._show_stats()
        print(UNKNOWN_COMMAND, file=sys.stderr)
        return EXIT_ERROR

    async def _import_persona(self, path: Path) -> int:
        result = await self._persona_importer.import_file(path)
        print(f"persona imported: {result.characters} characters")
        return EXIT_OK

    async def _show_persona(self) -> int:
        persona = await self._persona_service.get()
        if persona is None:
            print(NO_PERSONA_STORED, file=sys.stderr)
            return EXIT_ERROR
        print(f"persona: {len(persona.text)} characters, updated {persona.updated_at.isoformat()}")
        return EXIT_OK

    async def _import_examples(self, request: StyleRequest) -> int:
        if request.path is None:
            return EXIT_ERROR
        if request.replace and not request.confirmed:
            print(REPLACE_NEEDS_CONFIRMATION, file=sys.stderr)
            return EXIT_ERROR
        result = await self._examples_importer.import_file(request.path, replace=request.replace)
        print(
            f"examples imported: added {result.added}, skipped {result.skipped} "
            f"(already stored), total {result.total}"
        )
        return EXIT_OK

    async def _show_stats(self) -> int:
        stats = collect_example_stats(await self._examples.list_all())
        print(f"style examples: total {stats.total}")
        print(f"generic (no tags): {stats.generic}")
        for tag, count in stats.by_tag.items():
            print(f"tag {tag}: {count}")
        return EXIT_OK
