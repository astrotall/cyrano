from __future__ import annotations

import ast
import typing
from collections.abc import Iterator
from pathlib import Path

from cyrano.services.proactive_job_queue import ProactiveJobQueue
from cyrano.services.repositories import ProactiveRepository

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src" / "cyrano"
ELIGIBILITY_MODULE = "services/proactive_eligibility.py"
SWEEPER_MODULE = "services/proactive_sweeper.py"
JOB_MODULE = "services/proactive_job.py"
QUEUE_PROTOCOL_MODULE = "services/proactive_job_queue.py"
WORKER_QUEUE_MODULE = "worker/queue.py"
WORKER_COMPOSITION_MODULE = "worker/composition.py"
DOMAIN_MODULE = "domain/proactive.py"
ENUMS_MODULE = "domain/enums.py"
DB_REPOSITORIES_MODULE = "db/repositories.py"
DB_MODELS_MODULE = "db/models.py"
DOMAIN_MODELS_MODULE = "domain/models.py"
MAIN_MODULE = "main.py"
PROACTIVE_MODULES = (ELIGIBILITY_MODULE, SWEEPER_MODULE, JOB_MODULE, QUEUE_PROTOCOL_MODULE)
TRANSPORT_METHODS = frozenset({"send_text", "show_typing", "mark_read"})
RULE_FUNCTION = "decide_proactive"
REASON_ENUM = "ProactiveReason"
JOB_NAME = "PROACTIVE_JOB_NAME"
QUEUE_PROTOCOL = "ProactiveJobQueue"
ARQ_QUEUE = "ArqProactiveJobQueue"
RUNNER_CLASS = "ProactiveJobRunner"
NOT_BEFORE_WRITER = "set_proactive_not_before"
NOT_BEFORE_COLUMN = "proactive_not_before"
PROACTIVE_SOURCE = "PROACTIVE"
OUTBOX_SOURCE = "OutboxSource"
RULE_SETTINGS = frozenset(
    {
        "proactive_after_hours",
        "proactive_max_per_silence",
        "proactive_min_interval_hours",
        "proactive_daily_limit",
    }
)


def _modules() -> Iterator[tuple[str, ast.Module]]:
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        yield path.relative_to(SOURCE_ROOT).as_posix(), ast.parse(path.read_text())


def _names(tree: ast.AST) -> set[str]:
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names.update(alias.name for alias in node.names)
    return names


def _attributes(tree: ast.AST) -> set[str]:
    return {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}


def _called(tree: ast.AST) -> set[str]:
    called: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Attribute):
            called.add(node.func.attr)
        elif isinstance(node.func, ast.Name):
            called.add(node.func.id)
    return called


def _defined_functions(tree: ast.AST) -> set[str]:
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }


def _enum_members_used(tree: ast.AST, enum_name: str) -> set[str]:
    return {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == enum_name
    }


def _assigned_attributes(tree: ast.AST) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AugAssign | ast.AnnAssign):
            targets = [node.target]
        found.update(t.attr for t in targets if isinstance(t, ast.Attribute))
    return found


def _users(predicate: typing.Callable[[ast.Module], bool]) -> set[str]:
    return {name for name, tree in _modules() if predicate(tree)}


def test_the_eligibility_rule_is_defined_once() -> None:
    assert _users(lambda tree: RULE_FUNCTION in _defined_functions(tree)) == {ELIGIBILITY_MODULE}
    assert _users(lambda tree: bool(_enum_members_used(tree, REASON_ENUM))) == {ELIGIBILITY_MODULE}


def test_the_rule_settings_are_read_only_by_the_rule_the_scan_and_the_job() -> None:
    readers = _users(lambda tree: bool(_attributes(tree) & RULE_SETTINGS))

    assert readers == {ELIGIBILITY_MODULE, SWEEPER_MODULE, JOB_MODULE}


def test_the_sweeper_and_the_job_both_ask_the_one_rule() -> None:
    for module in (SWEEPER_MODULE, JOB_MODULE):
        tree = ast.parse((SOURCE_ROOT / module).read_text())
        assert "check" in _called(tree), module
        assert "ProactiveEligibility" in _names(tree), module


def test_only_the_sweeper_enqueues_proactive_jobs_and_only_the_worker_queue_names_the_job() -> None:
    protocol_users = _users(lambda tree: QUEUE_PROTOCOL in _names(tree))
    arq_users = _users(lambda tree: ARQ_QUEUE in _names(tree))
    job_name_users = _users(lambda tree: JOB_NAME in _names(tree))

    assert protocol_users == {SWEEPER_MODULE}
    assert arq_users == {MAIN_MODULE}
    assert job_name_users == {DOMAIN_MODULE, WORKER_QUEUE_MODULE}


def test_only_the_worker_composition_builds_the_proactive_runner() -> None:
    builders = _users(lambda tree: RUNNER_CLASS in _called(tree))

    assert builders == {WORKER_COMPOSITION_MODULE}


def test_only_the_proactive_job_writes_proactive_rows() -> None:
    writers = _users(lambda tree: PROACTIVE_SOURCE in _enum_members_used(tree, OUTBOX_SOURCE))

    assert writers == {
        JOB_MODULE,
        "services/send_guard.py",
        "services/outbox_sender.py",
        "domain/outbox.py",
        DB_REPOSITORIES_MODULE,
        "autopilot_cli.py",
    }
    for reader in writers - {JOB_MODULE}:
        tree = ast.parse((SOURCE_ROOT / reader).read_text())
        assert not {"enqueue", "enqueue_sequence"} & _called(tree), reader


def test_only_the_proactive_job_postpones_a_contact() -> None:
    callers = _users(lambda tree: NOT_BEFORE_WRITER in _called(tree))
    assigners = _users(lambda tree: NOT_BEFORE_COLUMN in _assigned_attributes(tree))

    assert callers == {JOB_MODULE}
    assert assigners == {DB_REPOSITORIES_MODULE}


def test_the_proactive_path_never_touches_the_transport_or_the_owner_notices() -> None:
    for module in PROACTIVE_MODULES:
        tree = ast.parse((SOURCE_ROOT / module).read_text())
        assert not (_attributes(tree) & TRANSPORT_METHODS), module
        assert "notify" not in _called(tree), module
        assert not any(
            isinstance(node, ast.Import | ast.ImportFrom)
            and any(
                (alias.name if isinstance(node, ast.Import) else node.module or "").split(".")[0]
                in {"arq", "telethon", "aiogram"}
                for alias in node.names
            )
            for node in ast.walk(tree)
        ), module


def test_the_proactive_repository_has_exactly_its_six_methods() -> None:
    assert set(typing.get_protocol_members(ProactiveRepository)) == {
        "dialogue_tail",
        "count_proactive_after",
        "newest_proactive_due_at",
        "count_proactive_due_since",
        "contacts_due_for_proactive",
        NOT_BEFORE_WRITER,
    }


def test_the_proactive_queue_has_exactly_one_method() -> None:
    assert set(typing.get_protocol_members(ProactiveJobQueue)) == {"enqueue"}


def test_the_scans_catch_what_they_look_for() -> None:
    bad = ast.parse(
        "def decide_proactive(snapshot, rules):\n"
        "    return ProactiveReason.ELIGIBLE\n"
        "async def sneak(outbox, contact, repo, row):\n"
        "    await outbox.enqueue(1, 'x', now, OutboxSource.PROACTIVE)\n"
        "    await repo.set_proactive_not_before(1, now)\n"
        "    row.proactive_not_before = now\n"
        "    await transport.send_text(1, 'x')\n"
        "    ProactiveJobRunner()\n"
    )

    assert RULE_FUNCTION in _defined_functions(bad)
    assert _enum_members_used(bad, REASON_ENUM) == {"ELIGIBLE"}
    assert _enum_members_used(bad, OUTBOX_SOURCE) == {PROACTIVE_SOURCE}
    assert NOT_BEFORE_WRITER in _called(bad)
    assert RUNNER_CLASS in _called(bad)
    assert NOT_BEFORE_COLUMN in _assigned_attributes(bad)
    assert "send_text" in _attributes(bad)
