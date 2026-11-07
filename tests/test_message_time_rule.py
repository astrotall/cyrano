from __future__ import annotations

import ast
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src" / "cyrano"
RULE_NAME = "message_time"
RULE_PARAMETERS = ["sent_at", "now"]
RULE_MODULE = "services/message_time.py"
SKEW_NAME = "MAX_FUTURE_SKEW"
SKEW_MODULE = "domain/models.py"
RULE_USERS = ("services/intake.py", "services/manual_intervention.py")
RULE_IMPORT = "cyrano.services.message_time"


def _tree(relative: str) -> ast.Module:
    return ast.parse((SOURCE_ROOT / relative).read_text(encoding="utf-8"))


def _modules_where(predicate: str) -> set[str]:
    found: set[str] = set()
    for path in SOURCE_ROOT.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if predicate == "defines-rule" and (
                isinstance(node, ast.FunctionDef)
                and [arg.arg for arg in node.args.args] == RULE_PARAMETERS
            ):
                found.add(path.relative_to(SOURCE_ROOT).as_posix())
            if predicate == "uses-skew" and isinstance(node, ast.Name) and node.id == SKEW_NAME:
                found.add(path.relative_to(SOURCE_ROOT).as_posix())
            if predicate == "defines-skew" and (
                isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == SKEW_NAME for t in node.targets)
            ):
                found.add(path.relative_to(SOURCE_ROOT).as_posix())
    return found


def _calls_rule(tree: ast.Module) -> bool:
    return any(
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == RULE_NAME
        for node in ast.walk(tree)
    )


def _imports_rule(tree: ast.Module) -> bool:
    return any(
        isinstance(node, ast.ImportFrom)
        and node.module == RULE_IMPORT
        and RULE_NAME in {alias.name for alias in node.names}
        for node in ast.walk(tree)
    )


def test_the_time_rule_is_defined_in_exactly_one_place() -> None:
    assert _modules_where("defines-rule") == {RULE_MODULE}


def test_the_skew_constant_is_defined_once_and_only_the_rule_reads_it() -> None:
    assert _modules_where("defines-skew") == {SKEW_MODULE}
    assert _modules_where("uses-skew") == {SKEW_MODULE, RULE_MODULE}


def test_the_intake_and_the_manual_message_detector_both_call_the_shared_rule() -> None:
    for relative in RULE_USERS:
        tree = _tree(relative)
        assert _imports_rule(tree), relative
        assert _calls_rule(tree), relative
