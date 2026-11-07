from __future__ import annotations

import ast
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src" / "cyrano"
RULE_MODULE = "services/humanization.py"
RULE_NAME = "is_ongoing"
BURST_RULE_NAME = "burst_is_ongoing"
PACE_METHOD = "conversation_ongoing"
WINDOW_FIELD = "ongoing_window_minutes"
SCHEDULER_MODULE = "services/autopilot.py"
DISPLAY_MODULE = "schedule_cli.py"
PLANNER_CLASS = "HumanizationPlanner"
PLAN_METHOD = "_plan_from"


def _trees() -> dict[str, ast.Module]:
    return {
        path.relative_to(SOURCE_ROOT).as_posix(): ast.parse(path.read_text(encoding="utf-8"))
        for path in SOURCE_ROOT.rglob("*.py")
    }


def _functions(tree: ast.AST) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    ]


def _called(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for call in ast.walk(node):
        if not isinstance(call, ast.Call):
            continue
        if isinstance(call.func, ast.Name):
            names.add(call.func.id)
        elif isinstance(call.func, ast.Attribute):
            names.add(call.func.attr)
    return names


def _reads_window(node: ast.AST) -> bool:
    return any(
        isinstance(child, ast.Attribute) and child.attr == WINDOW_FIELD for child in ast.walk(node)
    )


def test_the_ongoing_rule_and_its_burst_form_are_defined_once() -> None:
    defined = {
        (module, function.name)
        for module, tree in _trees().items()
        for function in _functions(tree)
        if function.name in {RULE_NAME, BURST_RULE_NAME, PACE_METHOD}
    }

    assert defined == {
        (RULE_MODULE, RULE_NAME),
        (RULE_MODULE, BURST_RULE_NAME),
        (RULE_MODULE, PACE_METHOD),
    }


def test_only_the_rule_reads_the_ongoing_window_and_only_the_planner_module_calls_it() -> None:
    readers = {
        (module, function.name)
        for module, tree in _trees().items()
        for function in _functions(tree)
        if _reads_window(function)
    }
    callers = {
        module for module, tree in _trees().items() if {RULE_NAME, BURST_RULE_NAME} & _called(tree)
    }

    assert {reader for reader in readers if reader[0] != DISPLAY_MODULE} == {
        (RULE_MODULE, RULE_NAME)
    }
    assert callers == {RULE_MODULE}


def test_the_planner_and_the_scheduler_both_decide_through_the_burst_rule() -> None:
    trees = _trees()
    planner = next(
        node
        for node in ast.walk(trees[RULE_MODULE])
        if isinstance(node, ast.ClassDef) and node.name == PLANNER_CLASS
    )
    methods = {function.name: function for function in _functions(planner)}

    assert BURST_RULE_NAME in _called(methods[PLAN_METHOD])
    assert BURST_RULE_NAME in _called(methods[PACE_METHOD])
    assert PACE_METHOD in _called(trees[SCHEDULER_MODULE])
    assert not _reads_window(trees[SCHEDULER_MODULE])


def test_the_scan_sees_a_second_copy_of_the_rule() -> None:
    copy = ast.parse(
        "def debounce(profile, gap):\n"
        "    return gap < timedelta(minutes=profile.reaction.ongoing_window_minutes)\n"
    )

    assert _reads_window(copy)
    assert [function.name for function in _functions(copy)] == ["debounce"]
