from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src" / "cyrano"
HELPER = "unavailable_soon"
HELPER_MODULE = "services/humanization.py"
WRITER = "set_reply_after"
WRITER_MODULE = "services/reply_job.py"
TEXT_NAMES = frozenset(
    {"WRAPUP_LINE", "WRAPUP_SOFT_INSTRUCTIONS", "WRAPUP_HARD_INSTRUCTIONS", "WRAPUP_SKIP_RULE"}
)
PROMPT_MODULES = frozenset({"prompts/texts.py", "prompts/render.py"})


def _modules() -> Iterator[tuple[str, ast.Module]]:
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        yield path.relative_to(SOURCE_ROOT).as_posix(), ast.parse(path.read_text())


def _defines(tree: ast.AST, name: str) -> bool:
    return any(
        isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name
        for node in ast.walk(tree)
    )


def _calls(tree: ast.AST, attribute: str) -> bool:
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == attribute
        for node in ast.walk(tree)
    )


def _names(tree: ast.AST) -> set[str]:
    found = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            found.update(alias.name for alias in node.names)
    return found


def test_the_wrapup_rule_is_defined_in_one_module() -> None:
    assert {name for name, tree in _modules() if _defines(tree, HELPER)} == {HELPER_MODULE}


def test_only_the_reply_job_marks_a_skipped_message_handled() -> None:
    callers = {name for name, tree in _modules() if _calls(tree, WRITER)}

    assert callers == {WRITER_MODULE}


def test_the_wrapup_texts_live_in_the_prompts_package() -> None:
    users = {name for name, tree in _modules() if _names(tree) & TEXT_NAMES}

    assert users == PROMPT_MODULES


def test_the_scans_catch_what_they_look_for() -> None:
    bad = ast.parse(
        "def unavailable_soon(now, profile):\n"
        "    return WRAPUP_LINE\n"
        "async def sneak(contacts):\n"
        "    await contacts.set_reply_after(1, now)\n"
    )

    assert _defines(bad, HELPER)
    assert _calls(bad, WRITER)
    assert "WRAPUP_LINE" in _names(bad)
