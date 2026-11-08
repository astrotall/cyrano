from __future__ import annotations

import ast
import typing
from collections.abc import Iterator
from pathlib import Path

from cyrano.domain.models import ContactCreate, ContactUpdate
from cyrano.services.repositories import ContactRepository

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src" / "cyrano"
ACTIONS_MODULE = "services/gender_actions.py"
COMMAND_MODULE = "contact_cli.py"
HANDLER_MODULE = "bot/handlers.py"
DB_REPOSITORIES_MODULE = "db/repositories.py"
PROTOCOL_MODULE = "services/repositories.py"
CONTACT_REPOSITORY_CLASS = "SqlContactRepository"
SET_METHOD = "set_gender"
GENDER_COLUMN = "gender"
CONTACT_WRITE_MODELS = frozenset({"ContactCreate", "ContactUpdate"})
NAME_DATA_MODULE = "gender_name_data.py"
NAME_DATA_NAMES = (
    "AMBIGUOUS_NAMES",
    "MASCULINE_NAMES_ENDING_IN_A_OR_YA",
    "MALE_NAMES",
)
GENDER_NAME_LITERAL_MINIMUM = 5


def _modules() -> Iterator[tuple[str, ast.Module]]:
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        yield path.relative_to(SOURCE_ROOT).as_posix(), ast.parse(path.read_text())


def _receivers_of(tree: ast.AST, method: str) -> set[str]:
    return {
        ast.unparse(node.func.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == method
    }


def _called_attributes(tree: ast.AST) -> set[str]:
    return {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }


def _constructor_calls_with_gender(tree: ast.AST) -> list[str]:
    return [
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in CONTACT_WRITE_MODELS
        and any(keyword.arg == GENDER_COLUMN for keyword in node.keywords)
    ]


def _gender_assignments(tree: ast.AST) -> Iterator[tuple[str, str]]:
    for cls in (node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)):
        for function in (n for n in ast.walk(cls) if isinstance(n, ast.AsyncFunctionDef)):
            for node in ast.walk(function):
                targets: list[ast.expr] = []
                if isinstance(node, ast.Assign):
                    targets = list(node.targets)
                elif isinstance(node, ast.AugAssign | ast.AnnAssign):
                    targets = [node.target]
                for target in targets:
                    if isinstance(target, ast.Attribute) and target.attr == GENDER_COLUMN:
                        yield cls.name, function.name


def test_only_the_gender_actions_and_the_contact_command_store_a_gender() -> None:
    callers: dict[str, set[str]] = {}
    for name, tree in _modules():
        if name in {DB_REPOSITORIES_MODULE, PROTOCOL_MODULE}:
            continue
        receivers = _receivers_of(tree, SET_METHOD)
        if receivers:
            callers[name] = receivers

    assert callers == {
        ACTIONS_MODULE: {"self._contacts"},
        COMMAND_MODULE: {"self._contacts"},
        HANDLER_MODULE: {"gender_actions"},
    }


def test_the_generic_contact_writers_cannot_carry_a_gender() -> None:
    for model in (ContactCreate, ContactUpdate):
        assert GENDER_COLUMN not in model.model_fields

    assert [name for name, tree in _modules() if _constructor_calls_with_gender(tree)] == []


def test_in_the_storage_only_set_gender_assigns_the_column() -> None:
    tree = ast.parse((SOURCE_ROOT / DB_REPOSITORIES_MODULE).read_text())

    assert set(_gender_assignments(tree)) == {(CONTACT_REPOSITORY_CLASS, SET_METHOD)}


def test_the_contact_repository_protocol_has_the_gender_writer() -> None:
    assert SET_METHOD in typing.get_protocol_members(ContactRepository)


def test_the_name_lists_live_in_one_module_only() -> None:
    owners: dict[str, set[str]] = {name: set() for name in NAME_DATA_NAMES}
    for module, tree in _modules():
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id in owners:
                        owners[target.id].add(module)
            elif (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.target.id in owners
            ):
                owners[node.target.id].add(module)

    assert owners == {name: {NAME_DATA_MODULE} for name in NAME_DATA_NAMES}


def test_no_other_module_holds_a_run_of_name_like_literals() -> None:
    for module, tree in _modules():
        if module in {NAME_DATA_MODULE, "prompts/texts.py", "stop_trigger_patterns.py"}:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Set | ast.Tuple | ast.List):
                names = [
                    element.value
                    for element in node.elts
                    if isinstance(element, ast.Constant)
                    and isinstance(element.value, str)
                    and element.value.isalpha()
                    and element.value.istitle()
                ]
                assert len(names) < GENDER_NAME_LITERAL_MINIMUM, module


def test_the_gender_scans_catch_what_they_look_for() -> None:
    bad = ast.parse(
        "class SqlOther:\n"
        "    async def sneak(self, row):\n"
        "        row.gender = None\n"
        "async def go(repo):\n"
        "    await repo.set_gender(1, None)\n"
        "    ContactUpdate(gender=None)\n"
    )

    assert SET_METHOD in _called_attributes(bad)
    assert _constructor_calls_with_gender(bad) == ["ContactUpdate"]
    assert set(_gender_assignments(bad)) == {("SqlOther", "sneak")}
