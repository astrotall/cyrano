from __future__ import annotations

import ast
import typing
from collections.abc import Iterator
from pathlib import Path

from cyrano.services.repositories import SummaryRepository
from cyrano.services.summary_job_queue import SummaryJobQueue

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src" / "cyrano"
UPDATER_MODULE = "services/summary_updater.py"
COMMAND_MODULE = "summary_cli.py"
SWEEPER_MODULE = "services/summary_sweeper.py"
SANITIZER_MODULE = "services/summary_sanitizer.py"
DB_REPOSITORIES_MODULE = "db/repositories.py"
PROTOCOL_MODULE = "services/repositories.py"
SUMMARY_REPOSITORY_CLASS = "SqlSummaryRepository"
CONTACT_REPOSITORY_CLASS = "SqlContactRepository"
GENERIC_CONTACT_WRITERS = frozenset({"create", "update"})
STORE_METHOD = "store_summary"
MARK_METHOD = "mark_summary_failed"
CLEAR_METHOD = "clear_summary"
SUMMARY_WRITE_METHODS = frozenset({STORE_METHOD, MARK_METHOD, CLEAR_METHOD})
ALLOWED_WRITERS = {
    STORE_METHOD: {UPDATER_MODULE},
    MARK_METHOD: {UPDATER_MODULE},
    CLEAR_METHOD: {COMMAND_MODULE},
}
SUMMARY_COLUMNS = frozenset({"summary", "summary_failed_at"})
CONTACT_UPDATE = "ContactUpdate"
TRANSPORT_METHODS = frozenset({"send_text", "show_typing", "mark_read"})
SUMMARY_MODULES = (UPDATER_MODULE, COMMAND_MODULE, SWEEPER_MODULE, SANITIZER_MODULE)


def _modules() -> Iterator[tuple[str, ast.Module]]:
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        yield path.relative_to(SOURCE_ROOT).as_posix(), ast.parse(path.read_text())


def _called_attributes(tree: ast.AST) -> set[str]:
    return {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }


def _contact_updates_with_summary(tree: ast.AST) -> int:
    return sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == CONTACT_UPDATE
        and any(keyword.arg == "summary" for keyword in node.keywords)
    )


def _summary_assignments(tree: ast.AST) -> Iterator[tuple[str, str]]:
    for cls in (node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)):
        for function in (n for n in ast.walk(cls) if isinstance(n, ast.AsyncFunctionDef)):
            for node in ast.walk(function):
                targets: list[ast.expr] = []
                if isinstance(node, ast.Assign):
                    targets = list(node.targets)
                elif isinstance(node, ast.AugAssign | ast.AnnAssign):
                    targets = [node.target]
                for target in targets:
                    if isinstance(target, ast.Attribute) and target.attr in SUMMARY_COLUMNS:
                        yield cls.name, function.name


def test_the_summary_repository_has_exactly_its_six_methods() -> None:
    assert set(typing.get_protocol_members(SummaryRepository)) == {
        "messages_after",
        "count_after",
        STORE_METHOD,
        MARK_METHOD,
        CLEAR_METHOD,
        "contacts_due_for_summary",
    }


def test_the_summary_queue_has_exactly_one_method() -> None:
    assert set(typing.get_protocol_members(SummaryJobQueue)) == {"enqueue"}


def test_only_the_updater_and_the_summary_command_write_a_summary() -> None:
    callers: dict[str, set[str]] = {method: set() for method in SUMMARY_WRITE_METHODS}
    for name, tree in _modules():
        if name in {DB_REPOSITORIES_MODULE, PROTOCOL_MODULE}:
            continue
        for method in _called_attributes(tree) & SUMMARY_WRITE_METHODS:
            callers[method].add(name)

    assert callers == ALLOWED_WRITERS


def test_no_module_writes_a_summary_through_the_generic_contact_update() -> None:
    offenders = [name for name, tree in _modules() if _contact_updates_with_summary(tree)]

    assert offenders == []


def test_in_the_storage_only_the_summary_repository_and_the_generic_writers_set_the_columns() -> (
    None
):
    tree = ast.parse((SOURCE_ROOT / DB_REPOSITORIES_MODULE).read_text())
    writers = set(_summary_assignments(tree))

    assert {cls for cls, _ in writers} == {SUMMARY_REPOSITORY_CLASS, CONTACT_REPOSITORY_CLASS}
    assert {method for cls, method in writers if cls == CONTACT_REPOSITORY_CLASS} <= (
        GENERIC_CONTACT_WRITERS
    )
    assert {method for cls, method in writers if cls == SUMMARY_REPOSITORY_CLASS} == {
        STORE_METHOD,
        MARK_METHOD,
        CLEAR_METHOD,
    }


def test_the_summary_path_never_touches_the_transport_or_the_owner_notices() -> None:
    for name in SUMMARY_MODULES:
        tree = ast.parse((SOURCE_ROOT / name).read_text())
        assert not (_called_attributes(tree) & TRANSPORT_METHODS), name
        assert "notify" not in _called_attributes(tree), name


def test_the_summary_scans_catch_what_they_look_for() -> None:
    bad = ast.parse(
        "class SqlOther:\n"
        "    async def sneak(self, row):\n"
        "        row.summary = {}\n"
        "        row.summary_failed_at = None\n"
        "async def go(repo, contacts):\n"
        "    await repo.store_summary(1, stored, None)\n"
        "    await contacts.update(1, ContactUpdate(summary={}))\n"
    )

    assert STORE_METHOD in _called_attributes(bad)
    assert _contact_updates_with_summary(bad) == 1
    assert set(_summary_assignments(bad)) == {("SqlOther", "sneak")}
