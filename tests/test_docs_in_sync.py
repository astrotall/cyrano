from __future__ import annotations

import argparse
import importlib
import re
from pathlib import Path

import pytest

from cyrano import main as main_module

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src" / "cyrano"
README = REPOSITORY_ROOT / "README.md"
CLAUDE = REPOSITORY_ROOT / "CLAUDE.md"
ARCHITECTURE = REPOSITORY_ROOT / ".claude" / "docs" / "architecture.md"
MAKEFILE = REPOSITORY_ROOT / "Makefile"
MIGRATIONS = REPOSITORY_ROOT / "migrations" / "versions"
SERVICES = SOURCE_ROOT / "services"
CLI_MODULE_GLOB = "*_cli.py"
CLI_PARSER_BUILDER = "_build_parser"
CLI_PACKAGE = "cyrano"
ACTION_GROUP = r"(?<![\w-]){command}\s*\(([^)]*)\)"
BROKEN_SENTENCE = re.compile(r"\bexist\.[ \t]*\n[ \t]*and the pause on a manual message")
MAKE_TARGET = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_-]*)\s*:(?!=)", re.MULTILINE)
REVISION = re.compile(r"""^revision\s*(?::\s*str\s*)?=\s*["']([^"']+)["']""", re.MULTILINE)
SKIPPED_MODULES = frozenset({"__init__"})


def _plain(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("`", "")


def _mentions(text: str, name: str) -> bool:
    return re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", text) is not None


def _report(document: str, what: str, missing: list[str]) -> str:
    return f"{document} is missing {what}: {', '.join(sorted(missing))}"


def _makefile_targets() -> set[str]:
    return set(MAKE_TARGET.findall(MAKEFILE.read_text(encoding="utf-8")))


def _choices(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    found: dict[str, argparse.ArgumentParser] = {}
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            found.update(action.choices)
    return found


def _top_level_parser(monkeypatch: pytest.MonkeyPatch) -> argparse.ArgumentParser:
    captured: list[argparse.ArgumentParser] = []

    def capture(
        self: argparse.ArgumentParser, *args: object, **kwargs: object
    ) -> argparse.Namespace:
        captured.append(self)
        return argparse.Namespace()

    monkeypatch.setattr(argparse.ArgumentParser, "parse_args", capture)
    main_module._parse_args([])
    assert captured, "main._parse_args did not build a parser"
    return captured[0]


def _own_action_parsers() -> dict[str, argparse.ArgumentParser]:
    found: dict[str, argparse.ArgumentParser] = {}
    for path in sorted(SOURCE_ROOT.glob(CLI_MODULE_GLOB)):
        module = importlib.import_module(f"{CLI_PACKAGE}.{path.stem}")
        builder = getattr(module, CLI_PARSER_BUILDER, None)
        if builder is None:
            continue
        parser = builder()
        found[parser.prog.split()[-1]] = parser
    return found


def _commands_and_actions(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    own = _own_action_parsers()
    commands: dict[str, list[str]] = {}
    for name, parser in _choices(_top_level_parser(monkeypatch)).items():
        actions = _choices(parser) or _choices(own.get(name, parser))
        commands[name] = list(actions)
    return commands


def _missing_commands(text: str, commands: dict[str, list[str]]) -> list[str]:
    missing: list[str] = []
    for command, actions in commands.items():
        if not actions:
            if not _mentions(text, command):
                missing.append(command)
            continue
        pattern = re.compile(ACTION_GROUP.format(command=re.escape(command)))
        listed = {item.strip() for group in pattern.findall(text) for item in group.split(",")}
        missing.extend(f"{command} ({action})" for action in actions if action not in listed)
    return missing


def test_every_makefile_target_is_in_the_readme() -> None:
    text = _plain(README)
    missing = [
        target
        for target in _makefile_targets()
        if not target.startswith(("_", ".")) and f"make {target}" not in text
    ]

    assert not missing, _report("README.md", "Makefile targets (as `make <target>`)", missing)


def test_the_makefile_targets_are_found() -> None:
    assert {"check", "test", "migrate", "stop-check", "autopilot-resume"} <= _makefile_targets()


def test_every_subcommand_and_action_of_the_parsers_is_in_the_readme(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing = _missing_commands(_plain(README), _commands_and_actions(monkeypatch))

    assert not missing, _report("README.md", "subcommands or actions", missing)


def test_every_subcommand_and_action_of_the_parsers_is_in_architecture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing = _missing_commands(_plain(ARCHITECTURE), _commands_and_actions(monkeypatch))

    assert not missing, _report("architecture.md", "subcommands or actions", missing)


def test_the_parsers_yield_the_commands_the_documents_must_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands = _commands_and_actions(monkeypatch)

    assert {"login", "persona", "examples", "outbox", "schedule", "autopilot"} <= set(commands)
    assert {"enqueue", "resume"} <= set(commands["outbox"])
    assert {"import", "stats", "convert"} <= set(commands["examples"])
    assert commands["login"] == []


def test_every_migration_revision_is_in_the_readme() -> None:
    revisions = [
        match.group(1)
        for path in sorted(MIGRATIONS.glob("*.py"))
        if (match := REVISION.search(path.read_text(encoding="utf-8")))
    ]
    text = _plain(README)

    assert revisions, "no migration revision found"
    assert not (missing := [r for r in revisions if not _mentions(text, r)]), _report(
        "README.md", "migration revisions", missing
    )


def test_every_service_module_is_in_claude_md() -> None:
    modules = [path.stem for path in SERVICES.glob("*.py") if path.stem not in SKIPPED_MODULES]
    text = _plain(CLAUDE)

    assert modules, "no service module found"
    assert not (missing := [m for m in modules if not _mentions(text, m)]), _report(
        "CLAUDE.md", "modules of src/cyrano/services", missing
    )


def test_the_broken_sentence_is_not_in_claude_md() -> None:
    assert BROKEN_SENTENCE.search(CLAUDE.read_text(encoding="utf-8")) is None, (
        "CLAUDE.md has a sentence that ends with 'exist.' followed by a line that begins "
        "with 'and the pause on a manual message'"
    )
