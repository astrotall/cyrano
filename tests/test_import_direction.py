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


def _packages_importing(library: str) -> set[str]:
    importers: set[str] = set()
    for path in SOURCE_ROOT.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module]
                if isinstance(node, ast.ImportFrom) and node.module is not None
                else []
            )
            if any(name.split(".")[0] == library for name in names):
                importers.add(path.relative_to(SOURCE_ROOT).parts[0])
    return importers


def test_only_the_userbot_package_imports_telethon() -> None:
    assert _packages_importing("telethon") == {"userbot"}


def test_only_the_bot_package_imports_aiogram() -> None:
    assert _packages_importing("aiogram") == {"bot"}


def test_services_import_neither_telethon_nor_aiogram() -> None:
    for module in _imported_modules("services"):
        assert module.split(".")[0] not in {"telethon", "aiogram"}


def test_bot_does_not_import_db_userbot_or_llm() -> None:
    for module in _imported_modules("bot"):
        assert not module.startswith(("cyrano.db", "cyrano.userbot", "cyrano.llm"))


def test_services_do_not_import_the_userbot_or_the_bot() -> None:
    for module in _imported_modules("services"):
        assert not module.startswith(("cyrano.userbot", "cyrano.bot"))


def test_userbot_does_not_import_db_or_llm() -> None:
    for module in _imported_modules("userbot"):
        assert not module.startswith(("cyrano.db", "cyrano.llm", "cyrano.bot"))


def test_only_the_llm_package_imports_httpx() -> None:
    assert _packages_importing("httpx") == {"llm"}


def test_llm_does_not_import_services_db_userbot_or_bot() -> None:
    for module in _imported_modules("llm"):
        assert not module.startswith(
            ("cyrano.services", "cyrano.db", "cyrano.userbot", "cyrano.bot")
        )


def test_only_the_composition_root_imports_the_llm_package() -> None:
    importers: set[str] = set()
    for path in SOURCE_ROOT.rglob("*.py"):
        relative = path.relative_to(SOURCE_ROOT)
        if relative.parts[0] == "llm":
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            names = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module]
                if isinstance(node, ast.ImportFrom) and node.module is not None
                else []
            )
            if any(name == "cyrano.llm" or name.startswith("cyrano.llm.") for name in names):
                importers.add(relative.as_posix())
    assert importers == {"main.py"}


def test_no_module_outside_llm_and_config_mentions_the_provider() -> None:
    mentioning = {
        path.relative_to(SOURCE_ROOT).as_posix()
        for path in SOURCE_ROOT.rglob("*.py")
        if path.relative_to(SOURCE_ROOT).parts[0] != "llm"
        and "deepseek" in path.read_text().lower()
    }
    assert mentioning == {"config.py"}


def test_prompts_import_only_the_domain_and_the_standard_library() -> None:
    for module in _imported_modules("prompts"):
        if module.startswith("cyrano."):
            assert module.startswith(("cyrano.domain", "cyrano.prompts"))
        else:
            assert module.split(".")[0] in sys.stdlib_module_names | {"__future__"}


def test_services_use_the_prompts_package() -> None:
    assert any(module.startswith("cyrano.prompts") for module in _imported_modules("services"))


def test_only_services_and_the_prompts_themselves_import_the_prompts_package() -> None:
    importers: set[str] = set()
    for path in SOURCE_ROOT.rglob("*.py"):
        relative = path.relative_to(SOURCE_ROOT)
        for node in ast.walk(ast.parse(path.read_text())):
            names = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module]
                if isinstance(node, ast.ImportFrom) and node.module is not None
                else []
            )
            if any(name.startswith("cyrano.prompts") for name in names):
                importers.add(relative.parts[0])
    assert importers == {"services", "prompts"}


def test_services_do_not_import_httpx_or_the_llm_package() -> None:
    for module in _imported_modules("services"):
        assert module.split(".")[0] != "httpx"
        assert not module.startswith("cyrano.llm")


def test_the_domain_and_the_prompts_do_not_mention_the_provider() -> None:
    for package in ("domain", "prompts", "services"):
        for path in (SOURCE_ROOT / package).rglob("*.py"):
            assert "deepseek" not in path.read_text().lower()
