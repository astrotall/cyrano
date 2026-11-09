from __future__ import annotations

import ast
from pathlib import Path

TESTS_ROOT = Path(__file__).resolve().parents[1]
ALLOWED_MODULES = frozenset(
    {
        "db/conftest.py",
        "db/test_migrations.py",
        "db/test_proactive_migration.py",
    }
)
COMMAND_MODULE = "alembic.command"
ALEMBIC_PACKAGE = "alembic"
COMMAND_NAME = "command"
MIGRATION_CALLS = frozenset({"upgrade", "downgrade", "stamp"})
MIGRATION_HELPER = "migrate_to_head"
FRESH_FIXTURE = "fresh_migrated_url"
TEMPLATE_FIXTURES = frozenset({"migrated_url", "template_database"})
FIXTURE_MODULES = frozenset({"db/test_migrations.py", "db/test_proactive_migration.py"})


def _names_the_command_api(node: ast.AST) -> bool:
    if isinstance(node, ast.ImportFrom):
        imported = {alias.name for alias in node.names}
        return node.module == COMMAND_MODULE or (
            node.module == ALEMBIC_PACKAGE and COMMAND_NAME in imported
        )
    if isinstance(node, ast.Import):
        return any(alias.name == COMMAND_MODULE for alias in node.names)
    if isinstance(node, ast.Attribute) and node.attr in MIGRATION_CALLS:
        value = node.value
        return (isinstance(value, ast.Name) and value.id == COMMAND_NAME) or (
            isinstance(value, ast.Attribute) and value.attr == COMMAND_NAME
        )
    return False


def _names_the_helper(node: ast.AST) -> bool:
    if isinstance(node, ast.Name):
        return node.id == MIGRATION_HELPER
    if isinstance(node, ast.Attribute):
        return node.attr == MIGRATION_HELPER
    if isinstance(node, ast.alias):
        return node.name == MIGRATION_HELPER
    return False


def _requests_the_fresh_fixture(node: ast.AST) -> bool:
    if isinstance(node, ast.arg):
        return node.arg == FRESH_FIXTURE
    return False


def _requests_the_template(node: ast.AST) -> bool:
    if isinstance(node, ast.arg):
        return node.arg in TEMPLATE_FIXTURES
    return False


def template_users(root: Path, modules: frozenset[str]) -> list[str]:
    users = []
    for relative in sorted(modules):
        tree = ast.parse((root / relative).read_text(encoding="utf-8"), filename=relative)
        if any(_requests_the_template(node) for node in ast.walk(tree)):
            users.append(relative)
    return users


def migration_users(root: Path, allowed: frozenset[str]) -> list[str]:
    users = []
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        if relative in allowed:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if any(_names_the_command_api(node) or _names_the_helper(node) for node in ast.walk(tree)):
            users.append(relative)
    return users


def fresh_fixture_users(root: Path, allowed: frozenset[str]) -> list[str]:
    users = []
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        if relative in allowed:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if any(_requests_the_fresh_fixture(node) for node in ast.walk(tree)):
            users.append(relative)
    return users


def test_only_the_migration_modules_run_migrations() -> None:
    assert migration_users(TESTS_ROOT, ALLOWED_MODULES) == []


def test_only_the_migration_modules_ask_for_a_freshly_migrated_database() -> None:
    allowed = ALLOWED_MODULES | {"db/test_template_database.py"}

    assert fresh_fixture_users(TESTS_ROOT, allowed) == []


def test_the_migration_modules_never_use_the_template_database() -> None:
    assert template_users(TESTS_ROOT, FIXTURE_MODULES) == []


def test_the_scan_flags_a_migration_test_that_asks_for_the_template(tmp_path: Path) -> None:
    (tmp_path / "templated.py").write_text("async def test_one(migrated_url) -> None: ...\n")
    (tmp_path / "fresh.py").write_text(f"async def test_two({FRESH_FIXTURE}) -> None: ...\n")

    assert template_users(tmp_path, frozenset({"templated.py", "fresh.py"})) == ["templated.py"]


def test_every_allowed_module_exists() -> None:
    for relative in ALLOWED_MODULES:
        assert (TESTS_ROOT / relative).is_file()


def test_the_scan_flags_each_way_of_running_a_migration(tmp_path: Path) -> None:
    sources = {
        "from_import.py": "from alembic import command\n",
        "module_import.py": "import alembic.command\n",
        "submodule_import.py": "from alembic.command import upgrade\n",
        "call.py": "command.upgrade(config, 'head')\n",
        "downgrade.py": "alembic.command.downgrade(config, 'base')\n",
        "helper_import.py": "from tests.db.conftest import migrate_to_head\n",
        "helper_call.py": "migrate_to_head(config)\n",
        "helper_attribute.py": "module.migrate_to_head(config)\n",
    }
    for name, source in sources.items():
        (tmp_path / name).write_text(source, encoding="utf-8")
    (tmp_path / "clean.py").write_text(
        "from alembic.config import Config\nvalue = Config('x')\n", encoding="utf-8"
    )

    assert migration_users(tmp_path, frozenset()) == sorted(sources)


def test_the_scan_skips_only_the_allowed_modules(tmp_path: Path) -> None:
    (tmp_path / "allowed.py").write_text("from alembic import command\n", encoding="utf-8")
    (tmp_path / "other.py").write_text("from alembic import command\n", encoding="utf-8")

    assert migration_users(tmp_path, frozenset({"allowed.py"})) == ["other.py"]


def test_the_scan_flags_a_test_that_asks_for_a_freshly_migrated_database(tmp_path: Path) -> None:
    (tmp_path / "asks.py").write_text(
        f"async def test_one({FRESH_FIXTURE}) -> None: ...\n", encoding="utf-8"
    )
    (tmp_path / "plain.py").write_text("async def test_two(migrated_url) -> None: ...\n")

    assert fresh_fixture_users(tmp_path, frozenset()) == ["asks.py"]
