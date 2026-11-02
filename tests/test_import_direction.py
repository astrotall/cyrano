from __future__ import annotations

import ast
import sys
from collections.abc import Iterator
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src" / "cyrano"
DOMAIN_ALLOWED_THIRD_PARTY = {"pydantic"}


def _imported_modules(package: str) -> Iterator[str]:
    for path in (SOURCE_ROOT / package).rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                yield from (alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                yield node.module


def test_domain_imports_only_pydantic_the_stdlib_and_itself() -> None:
    allowed = sys.stdlib_module_names | DOMAIN_ALLOWED_THIRD_PARTY

    for module in _imported_modules("domain"):
        if module.startswith("cyrano."):
            assert module.startswith("cyrano.domain")
        else:
            assert module.split(".")[0] in allowed


def test_db_does_not_import_services_or_llm() -> None:
    for module in _imported_modules("db"):
        assert not module.startswith(("cyrano.services", "cyrano.llm"))


def test_services_do_not_import_db() -> None:
    for module in _imported_modules("services"):
        assert not module.startswith("cyrano.db")
