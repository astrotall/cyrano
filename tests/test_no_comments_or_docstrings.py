from __future__ import annotations

import ast
import io
import tokenize
from collections.abc import Callable
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CHECKED_DIRECTORIES = ("src", "tests", "migrations")
PRAGMA_PREFIXES = ("noqa", "type: ignore", "pragma", "fmt:", "ruff:", "mypy:")
SHEBANG_PREFIX = "#!"
SHEBANG_LINE = 1
COMMENT_MARK = "#"
DocstringOwner = ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef


def _lines(*parts: str) -> str:
    return "\n".join(parts) + "\n"


def python_files() -> list[Path]:
    files: list[Path] = []
    for directory in CHECKED_DIRECTORIES:
        files.extend(sorted((REPOSITORY_ROOT / directory).rglob("*.py")))
    return files


def _is_allowed_comment(comment: str, line: int) -> bool:
    if line == SHEBANG_LINE and comment.startswith(SHEBANG_PREFIX):
        return True
    body = comment.removeprefix(COMMENT_MARK).strip()
    return body.startswith(PRAGMA_PREFIXES)


def comment_lines(source: str) -> list[int]:
    found: list[int] = []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type != tokenize.COMMENT:
            continue
        line = token.start[0]
        if not _is_allowed_comment(token.string, line):
            found.append(line)
    return found


def _docstring_line(owner: DocstringOwner) -> int:
    return owner.body[0].lineno


def docstring_lines(source: str) -> list[int]:
    found: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, DocstringOwner) and ast.get_docstring(node, clean=False) is not None:
            found.append(_docstring_line(node))
    return sorted(found)


def _violations(finder: Callable[[str], list[int]]) -> list[str]:
    report: list[str] = []
    for path in python_files():
        relative = path.relative_to(REPOSITORY_ROOT).as_posix()
        report.extend(f"{relative}:{line}" for line in finder(path.read_text(encoding="utf-8")))
    return report


def _failure_message(kind: str, report: list[str]) -> str:
    return f"{len(report)} {kind} found:\n" + "\n".join(f"  {entry}" for entry in report)


def test_the_scan_covers_the_source_the_tests_and_the_migrations() -> None:
    top_level = {path.relative_to(REPOSITORY_ROOT).parts[0] for path in python_files()}

    assert top_level == set(CHECKED_DIRECTORIES)


def test_no_python_file_has_a_comment_that_is_not_a_pragma() -> None:
    report = _violations(comment_lines)

    assert not report, _failure_message("comments", report)


def test_no_python_file_has_a_docstring() -> None:
    report = _violations(docstring_lines)

    assert not report, _failure_message("docstrings", report)


def test_a_planted_own_line_comment_is_found() -> None:
    source = _lines("x = 1", "# explains x", "y = 2")

    assert comment_lines(source) == [2]


def test_a_planted_trailing_comment_is_found() -> None:
    source = _lines("x = 1  # explains x", "y = 2")

    assert comment_lines(source) == [1]


def test_a_comment_inside_a_function_body_is_found() -> None:
    source = _lines("def run() -> None:", "    value = 1", "    # why", "    print(value)")

    assert comment_lines(source) == [3]


def test_an_empty_comment_is_found() -> None:
    assert comment_lines(_lines("x = 1  #")) == [1]


@pytest.mark.parametrize(
    "comment",
    [
        "# noqa",
        "# noqa: S101",
        "#noqa: E501",
        "# type: ignore",
        "# type: ignore[arg-type]",
        "# pragma: no cover",
        "# fmt: off",
        "# fmt: skip",
        "# ruff: noqa: S101",
        "# mypy: ignore-errors",
    ],
)
def test_tool_pragmas_pass(comment: str) -> None:
    source = _lines(f"value = compute()  {comment}", f"{comment}", "other = 1")

    assert comment_lines(source) == []


def test_a_shebang_on_the_first_line_passes() -> None:
    source = _lines("#!/usr/bin/env python", "x = 1")

    assert comment_lines(source) == []


def test_a_shebang_like_comment_on_a_later_line_is_found() -> None:
    source = _lines("x = 1", "#!/usr/bin/env python")

    assert comment_lines(source) == [2]


def test_a_hash_inside_a_string_is_not_a_comment() -> None:
    source = _lines(
        'text = "# not a comment"',
        "other = '''",
        "# still not a comment",
        "'''",
        'formatted = f"{text}# nor this"',
    )

    assert comment_lines(source) == []


def test_a_planted_module_docstring_is_found() -> None:
    source = _lines('"""About the module."""', "", "x = 1")

    assert docstring_lines(source) == [1]


def test_a_planted_class_docstring_is_found() -> None:
    source = _lines("class Thing:", '    """About the class."""', "", "    value = 1")

    assert docstring_lines(source) == [2]


def test_a_planted_function_docstring_is_found() -> None:
    source = _lines("def run() -> None:", '    """About the function."""', "    return None")

    assert docstring_lines(source) == [2]


def test_a_planted_async_function_docstring_is_found() -> None:
    source = _lines("async def run() -> None:", '    """About it."""', "    return None")

    assert docstring_lines(source) == [2]


def test_a_planted_method_docstring_is_found() -> None:
    source = _lines(
        "class Thing:",
        "    def run(self) -> None:",
        "        '''About the method.'''",
        "        return None",
    )

    assert docstring_lines(source) == [3]


def test_a_docstring_only_body_is_found() -> None:
    source = _lines("class Failure(Exception):", '    """Raised on failure."""')

    assert docstring_lines(source) == [2]


def test_every_kind_of_planted_docstring_is_reported_together() -> None:
    source = _lines(
        '"""module"""',
        "class A:",
        '    """class"""',
        "    def f(self) -> None:",
        '        """method"""',
        "async def g() -> None:",
        '    """async"""',
    )

    assert docstring_lines(source) == [1, 3, 5, 7]


def test_a_string_statement_in_the_middle_of_a_body_is_not_a_docstring() -> None:
    source = _lines(
        "def run() -> None:",
        "    value = 1",
        '    "just a string statement"',
        "    print(value)",
    )

    assert docstring_lines(source) == []


def test_string_values_prompt_texts_and_test_data_are_not_docstrings() -> None:
    source = _lines(
        'PROMPT = """Answer briefly."""',
        "class Thing:",
        '    label = "text"',
        '    other = """more text"""',
        "def run() -> str:",
        "    data = ['''sample''']",
        '    return f"{data}"',
        'print("""a call argument""")',
    )

    assert docstring_lines(source) == []


def test_a_string_after_the_first_statement_of_a_class_is_not_a_docstring() -> None:
    source = _lines("class Thing:", "    value = 1", '    "attribute note"')

    assert docstring_lines(source) == []


def test_a_clean_sample_passes_both_checks() -> None:
    source = _lines(
        "from __future__ import annotations",
        "",
        "def run() -> int:",
        "    return 1  # noqa: RUF100",
    )

    assert docstring_lines(source) == []
    assert comment_lines(source) == []


def test_the_failure_message_lists_file_and_line() -> None:
    message = _failure_message("comments", ["src/a.py:3", "tests/b.py:9"])

    assert message == "2 comments found:\n  src/a.py:3\n  tests/b.py:9"
