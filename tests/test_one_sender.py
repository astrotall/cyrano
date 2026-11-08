from __future__ import annotations

import ast
import re
import typing
from collections.abc import Iterator
from pathlib import Path

import pytest

from cyrano.services.hand_over_notifier import HandOverNotifier
from cyrano.services.manual_pause_notifier import ManualPauseNotifier
from cyrano.services.message_transport import MessageTransport
from cyrano.services.owner_notifier import OwnerNotifier
from cyrano.services.repositories import AutopilotStateRepository, SenderStateRepository
from cyrano.userbot.protocols import UserbotClient

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src" / "cyrano"
SENDER_MODULE = "services/outbox_sender.py"
ADAPTER_MODULE = "userbot/adapter.py"
USERBOT_PACKAGE = "userbot"
BOT_PACKAGE = "bot"
OWNER_CHAT_MODULES = {"bot/sender.py", "bot/notifier.py"}
DETECTOR_MODULE = "services/manual_intervention.py"
MANUAL_PAUSE_METHOD = "notify_manual_pause"
MANUAL_PAUSE_PROTOCOL_MODULE = "services/manual_pause_notifier.py"
MANUAL_PAUSE_NOTIFIER_MODULE = "bot/notifier.py"
RESUME_PAUSE_METHOD = "resume"
IGNORE_PAUSE_METHOD = "ignore"
HAND_OVER_METHOD = "notify_hand_over"
HAND_OVER_PROTOCOL_MODULE = "services/hand_over_notifier.py"
HAND_OVER_NOTIFIER_MODULE = "bot/notifier.py"
HAND_OVER_MODULE = "services/hand_over.py"
INTAKE_MODULE = "services/intake.py"
EARLY_READ_MODULE = "services/early_read.py"
EARLY_READ_DOMAIN_MODULE = "domain/early_read.py"
EARLY_READ_EXECUTION = "_execute_early_read"
EARLY_READ_FUNCTIONS = frozenset({"run_early_reads", "execute_early_reads", EARLY_READ_EXECUTION})
EARLY_READ_FORBIDDEN_RECEIVERS = frozenset({"_outbox", "_limiter"})
INTAKE_HANDLE = "handle"
INTAKE_STOP_CHECK = "_check_stop_triggers"
ON_INCOMING = "on_incoming"
STOP_TRIGGER_DETECTOR_MODULE = "services/stop_triggers.py"
STOP_CHECK_MODULE = "stop_check_cli.py"
SUPERSEDE_METHOD = "cancel_superseded_autopilot_rows"
SUPERSEDE_CHECK = "_cancel_if_superseded"
SUPERSEDE_REASON = "CANCEL_REASON_SUPERSEDED"
DOMAIN_OUTBOX_MODULE = "domain/outbox.py"
DELIVER_METHOD = "_deliver"
BEFORE_SEND_METHOD = "_before_send"
GUARD_METHOD = "_guarded_contact"
ENQUEUE_METHOD = "enqueue"
ENQUEUE_SEQUENCE_METHOD = "enqueue_sequence"
SCHEDULE_FOLLOWERS_METHOD = "schedule_followers"
CANCEL_ORPHANS_METHOD = "cancel_orphaned_followers"
PREDECESSOR_REASON = "CANCEL_REASON_PREDECESSOR"
OUTBOX_SERVICE_MODULE = "services/outbox_service.py"
DB_REPOSITORIES_MODULE = "db/repositories.py"
STORAGE_WRITE_METHODS = frozenset(
    {
        "set_status",
        "append",
        "enqueue",
        ENQUEUE_SEQUENCE_METHOD,
        SCHEDULE_FOLLOWERS_METHOD,
        CANCEL_ORPHANS_METHOD,
        "cancel_pending_for_contact",
        SUPERSEDE_METHOD,
        "commit",
        "update_identity",
    }
)
PAUSE_HANDLER_MODULE = "bot/handlers.py"
PAUSE_ACTIONS_NAME = "pause_actions"
TRANSPORT_METHOD = "send_text"
TYPING_METHOD = "show_typing"
READ_METHOD = "mark_read"
TRANSPORT_METHODS = frozenset({TRANSPORT_METHOD, TYPING_METHOD, READ_METHOD})
ADAPTER_WRITE_METHODS = frozenset({"send_message", "send_read_acknowledge", "action"})
RESCHEDULE_METHOD = "reschedule"
FLOOD_WAIT_HANDLER = "_handle_flood_wait"
LIMIT_HANDLER = "_hold_for_limit"
OUTSIDE_HOURS_HANDLER = "_hold_outside_hours"
PAUSE_METHOD = "pause_until"
STOP_METHOD = "stop"
RESUME_METHOD = "resume"
RETRY_METHOD = "retry_failed"
OWNER_COMMAND_MODULE = "outbox_cli.py"
REPLY_JOB_MODULE = "services/reply_job.py"
PROACTIVE_JOB_MODULE = "services/proactive_job.py"
AUTOPILOT_MODULE = "services/autopilot.py"
AUTOPILOT_COMMAND_MODULE = "autopilot_cli.py"
AUTOPILOT_PAUSE_HANDLER = "_pause"
AUTOPILOT_STOP_HANDLER = "_stop"
WORKER_PACKAGE = "worker"
APP_QUEUE_MODULE = "cyrano.worker.queue"
TELETHON_WRITE_METHODS = frozenset(
    {
        "send_message",
        "send_file",
        "send_read_acknowledge",
        "forward_messages",
        "edit_message",
        "delete_messages",
        "delete_dialog",
        "pin_message",
        "unpin_message",
        "action",
        "mark_read",
        "reply",
        "respond",
        "click",
        "upload_file",
        "edit_folder",
        "edit_permissions",
        "edit_admin",
        "kick_participant",
        "conversation",
    }
)
READ_REQUEST_ALLOWLIST = frozenset({"GetFullUserRequest"})
REQUEST_MODULE_PREFIX = "telethon.tl.functions"
REQUEST_CLASS_SUFFIX = "Request"
CALL_ONLY_WRITE_METHODS = frozenset({"action"})
WRITE_LOOKING_MEMBER = re.compile(
    r"^(?:send|edit|delete|forward|mark|read_history|read_ack|set_typing|set|typing|action|"
    r"upload|pin|unpin|reply|respond|click|invoke|request|__call__)"
)


def _modules() -> Iterator[tuple[str, ast.Module]]:
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        yield path.relative_to(SOURCE_ROOT).as_posix(), ast.parse(path.read_text())


def _called_attributes(tree: ast.AST) -> set[str]:
    return {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }


def _attributes_used(tree: ast.AST) -> set[str]:
    return {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}


def _names_passed_to_getattr(tree: ast.AST) -> set[str]:
    return {
        node.args[1].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "getattr"
        and len(node.args) >= 2
        and isinstance(node.args[1], ast.Constant)
        and isinstance(node.args[1].value, str)
    }


def _telethon_write_uses(tree: ast.AST) -> set[str]:
    referenced = _attributes_used(tree) - CALL_ONLY_WRITE_METHODS
    return (referenced | _called_attributes(tree) | _names_passed_to_getattr(tree)) & (
        TELETHON_WRITE_METHODS
    )


def _imported(tree: ast.AST) -> Iterator[tuple[str, str]]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, ""
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            for alias in node.names:
                yield node.module, alias.name


def _imported_names(tree: ast.AST) -> set[str]:
    return {name for _, name in _imported(tree)}


def _members(protocol: type) -> set[str]:
    return set(typing.get_protocol_members(protocol))


def test_the_transport_has_exactly_three_methods() -> None:
    assert _members(MessageTransport) == {"send_text", "show_typing", "mark_read"}


def test_the_notifier_has_exactly_one_method() -> None:
    assert _members(OwnerNotifier) == {"notify"}


def test_the_manual_pause_notifier_has_exactly_one_method() -> None:
    assert _members(ManualPauseNotifier) == {MANUAL_PAUSE_METHOD}


def test_the_manual_pause_notifier_is_implemented_only_in_the_bot_package() -> None:
    definers = {
        name
        for name, tree in _modules()
        if any(
            isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)
            and node.name == MANUAL_PAUSE_METHOD
            for node in ast.walk(tree)
        )
    }

    assert definers == {MANUAL_PAUSE_PROTOCOL_MODULE, MANUAL_PAUSE_NOTIFIER_MODULE}


def test_only_the_detector_calls_the_manual_pause_notifier_and_only_behind_the_deadline() -> None:
    callers = {name for name, tree in _modules() if MANUAL_PAUSE_METHOD in _called_attributes(tree)}
    source = (SOURCE_ROOT / DETECTOR_MODULE).read_text()

    assert callers == {DETECTOR_MODULE}
    assert len(re.findall(r"\.notify_manual_pause\(", source)) == 1
    assert re.search(
        r"asyncio\.timeout\(self\._notify_deadline\):\s+"
        r"await self\._notifier\.notify_manual_pause\(",
        source,
    )


def _calls_named(tree: ast.AST, attribute: str) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == attribute
    ]


def _resume_calls_on(tree: ast.AST, receiver: str) -> int:
    return sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == RESUME_PAUSE_METHOD
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == receiver
    )


def test_only_the_bot_handlers_resume_a_paused_contact() -> None:
    callers = {name for name, tree in _modules() if _resume_calls_on(tree, PAUSE_ACTIONS_NAME)}

    assert callers == {PAUSE_HANDLER_MODULE}


def test_the_hand_over_notifier_has_exactly_one_method() -> None:
    assert _members(HandOverNotifier) == {HAND_OVER_METHOD}


def test_the_hand_over_notifier_is_implemented_only_in_the_bot_package() -> None:
    definers = {
        name
        for name, tree in _modules()
        if any(
            isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)
            and node.name == HAND_OVER_METHOD
            for node in ast.walk(tree)
        )
    }

    assert definers == {HAND_OVER_PROTOCOL_MODULE, HAND_OVER_NOTIFIER_MODULE}


def test_only_the_hand_over_service_calls_its_notifier_and_only_behind_the_deadline() -> None:
    callers = {name for name, tree in _modules() if HAND_OVER_METHOD in _called_attributes(tree)}
    source = (SOURCE_ROOT / HAND_OVER_MODULE).read_text()

    assert callers == {HAND_OVER_MODULE}
    assert len(re.findall(r"\.notify_hand_over\(", source)) == 1
    assert re.search(
        r"asyncio\.timeout\(self\._notify_deadline\):\s+"
        r"await self\._notifier\.notify_hand_over\(",
        source,
    )


def test_only_the_bot_handlers_ignore_a_paused_contact() -> None:
    callers = {
        name
        for name, tree in _modules()
        if any(
            isinstance(call.func, ast.Attribute)
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id == PAUSE_ACTIONS_NAME
            for call in _calls_named(tree, IGNORE_PAUSE_METHOD)
        )
    }

    assert callers == {PAUSE_HANDLER_MODULE}


@pytest.mark.parametrize(
    "module",
    [HAND_OVER_MODULE, INTAKE_MODULE, STOP_TRIGGER_DETECTOR_MODULE, STOP_CHECK_MODULE],
)
def test_the_stop_trigger_path_never_touches_the_transport(module: str) -> None:
    tree = ast.parse((SOURCE_ROOT / module).read_text())

    assert _called_attributes(tree).isdisjoint(TRANSPORT_METHODS | TELETHON_WRITE_METHODS)
    assert _attributes_used(tree).isdisjoint(TRANSPORT_METHODS | TELETHON_WRITE_METHODS)


def test_the_stop_trigger_pause_reason_is_set_only_by_the_hand_over_service() -> None:
    users = {
        name
        for name, tree in _modules()
        if "STOP_TRIGGER" in _attributes_used(tree) and name != "domain/enums.py"
    }

    assert users == {HAND_OVER_MODULE}


def test_the_stop_check_command_writes_nothing() -> None:
    tree = ast.parse((SOURCE_ROOT / STOP_CHECK_MODULE).read_text())

    assert _called_attributes(tree).isdisjoint(STORAGE_WRITE_METHODS)


def test_the_stop_trigger_detector_imports_only_the_domain_and_its_pattern_data() -> None:
    tree = ast.parse((SOURCE_ROOT / STOP_TRIGGER_DETECTOR_MODULE).read_text())
    project = {module for module, _ in _imported(tree) if module.startswith("cyrano")}

    assert project
    assert all(
        module.startswith("cyrano.domain") or module == "cyrano.stop_trigger_patterns"
        for module in project
    ), project


def test_the_detector_never_touches_the_transport() -> None:
    tree = ast.parse((SOURCE_ROOT / DETECTOR_MODULE).read_text())

    assert _called_attributes(tree).isdisjoint(TRANSPORT_METHODS | TELETHON_WRITE_METHODS)
    assert _attributes_used(tree).isdisjoint(TRANSPORT_METHODS | TELETHON_WRITE_METHODS)


def test_the_manual_pause_reason_is_set_only_by_the_detector() -> None:
    users = {
        name
        for name, tree in _modules()
        if "MANUAL_MESSAGE" in _attributes_used(tree) and name != "domain/enums.py"
    }

    assert users == {DETECTOR_MODULE}


def test_the_sender_calls_the_notifier_in_one_place_behind_the_deadline() -> None:
    source = (SOURCE_ROOT / SENDER_MODULE).read_text()

    assert len(re.findall(r"\.notify\(", source)) == 1
    assert re.search(
        r"asyncio\.timeout\(self\._timings\.notify_deadline\):\s+await self\._notifier\.notify\(",
        source,
    )


def test_the_read_client_protocol_has_no_write_member() -> None:
    members = _members(UserbotClient)

    assert members.isdisjoint(TRANSPORT_METHODS)
    assert "__call__" not in members
    assert {member for member in members if WRITE_LOOKING_MEMBER.match(member)} == set()
    assert members & TELETHON_WRITE_METHODS == set()


@pytest.mark.parametrize("method", sorted(TRANSPORT_METHODS))
def test_only_the_sender_calls_the_transport(method: str) -> None:
    callers = {
        name
        for name, tree in _modules()
        if method in _called_attributes(tree) or method in _names_passed_to_getattr(tree)
    }

    assert callers == {SENDER_MODULE}


@pytest.mark.parametrize("method", sorted(TRANSPORT_METHODS))
def test_only_the_adapter_and_the_protocol_define_a_transport_method(method: str) -> None:
    definers = {
        name
        for name, tree in _modules()
        if any(
            isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == method
            for node in ast.walk(tree)
        )
    }

    assert definers == {ADAPTER_MODULE, "services/message_transport.py"}


def test_only_the_adapter_uses_a_telethon_write_method() -> None:
    users: dict[str, set[str]] = {}
    for name, tree in _modules():
        if name.split("/")[0] == BOT_PACKAGE:
            continue
        used = _telethon_write_uses(tree)
        if used:
            users[name] = used

    assert users == {ADAPTER_MODULE: set(ADAPTER_WRITE_METHODS), SENDER_MODULE: {READ_METHOD}}


def test_the_sender_reaches_the_read_mark_only_through_the_transport() -> None:
    tree = ast.parse((SOURCE_ROOT / SENDER_MODULE).read_text())
    receivers = {
        node.func.value.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in TRANSPORT_METHODS
        and isinstance(node.func.value, ast.Attribute)
    }

    assert receivers == {"_transport"}


def test_inside_the_bot_package_only_the_owner_chat_modules_send() -> None:
    senders = {
        name
        for name, tree in _modules()
        if name.split("/")[0] == BOT_PACKAGE
        and _attributes_used(tree) & {"send_message", "send_photo", "send_media_group"}
    }

    assert senders == OWNER_CHAT_MODULES


def test_the_adapter_never_asks_for_a_write_beyond_its_three() -> None:
    tree = ast.parse((SOURCE_ROOT / ADAPTER_MODULE).read_text())

    assert _telethon_write_uses(tree) == set(ADAPTER_WRITE_METHODS)


def test_the_typing_action_and_the_read_acknowledge_are_used_only_by_the_adapter() -> None:
    users = {
        name
        for name, tree in _modules()
        if {"send_read_acknowledge", "action"} & _telethon_write_uses(tree)
    }

    assert users == {ADAPTER_MODULE}


def test_telethon_requests_are_imported_only_by_the_adapter_and_only_from_the_allowlist() -> None:
    found: dict[str, set[str]] = {}
    for name, tree in _modules():
        for module, imported in _imported(tree):
            is_request_module = module.startswith(REQUEST_MODULE_PREFIX) or module.startswith(
                "telethon.functions"
            )
            is_request_name = imported.endswith(REQUEST_CLASS_SUFFIX)
            if module.startswith("telethon") and (is_request_module or is_request_name):
                found.setdefault(name, set()).add(imported)

    assert found == {ADAPTER_MODULE: set(READ_REQUEST_ALLOWLIST)}


def test_the_generic_request_call_is_gone_from_the_userbot() -> None:
    for name, tree in _modules():
        if name.split("/")[0] != USERBOT_PACKAGE or name == ADAPTER_MODULE:
            continue
        assert not any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_client"
            for node in ast.walk(tree)
        ), name


def test_nothing_outside_the_userbot_package_imports_telethon() -> None:
    importers = {
        name.split("/")[0]
        for name, tree in _modules()
        if any(module.split(".")[0] == "telethon" for module, _ in _imported(tree))
    }

    assert importers == {USERBOT_PACKAGE}


def test_nothing_outside_the_bot_package_imports_aiogram() -> None:
    importers = {
        name.split("/")[0]
        for name, tree in _modules()
        if any(module.split(".")[0] == "aiogram" for module, _ in _imported(tree))
    }

    assert importers == {BOT_PACKAGE}


def test_the_raw_telegram_client_is_wrapped_before_it_leaves_the_client_module() -> None:
    tree = ast.parse((SOURCE_ROOT / "userbot/client.py").read_text())
    build_access = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "build_access"
    )

    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "TelethonAdapter"
        for node in ast.walk(build_access)
    )


def _callers_in_module(tree: ast.AST, attribute: str) -> set[str]:
    return {
        function.name
        for function in ast.walk(tree)
        if isinstance(function, ast.AsyncFunctionDef | ast.FunctionDef)
        and attribute in _called_attributes(function)
    }


def test_only_a_flood_wait_a_reached_limit_and_a_closed_window_put_a_row_back() -> None:
    modules = {
        name: _callers_in_module(tree, RESCHEDULE_METHOD)
        for name, tree in _modules()
        if RESCHEDULE_METHOD in _called_attributes(tree)
    }

    assert modules == {SENDER_MODULE: {FLOOD_WAIT_HANDLER, LIMIT_HANDLER, OUTSIDE_HOURS_HANDLER}}


def test_the_state_repository_has_exactly_its_four_methods() -> None:
    assert _members(SenderStateRepository) == {"get", PAUSE_METHOD, STOP_METHOD, RESUME_METHOD}


def _calls_with_arguments(tree: ast.AST, attribute: str) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == attribute
        and (node.args or node.keywords)
    ]


def test_only_the_flood_path_pauses_the_sender() -> None:
    modules = {
        name: _callers_in_module(tree, PAUSE_METHOD)
        for name, tree in _modules()
        if PAUSE_METHOD in _called_attributes(tree)
    }

    assert modules == {
        SENDER_MODULE: {FLOOD_WAIT_HANDLER},
        REPLY_JOB_MODULE: {AUTOPILOT_PAUSE_HANDLER},
    }


def test_only_the_account_path_stops_the_sender() -> None:
    writers = {name for name, tree in _modules() if _calls_with_arguments(tree, STOP_METHOD)}
    tree = ast.parse((SOURCE_ROOT / SENDER_MODULE).read_text())
    job = ast.parse((SOURCE_ROOT / REPLY_JOB_MODULE).read_text())

    assert writers == {SENDER_MODULE, REPLY_JOB_MODULE}
    assert _callers_in_module(tree, STOP_METHOD) == {"_stop_for_account"}
    assert _callers_in_module(job, STOP_METHOD) == {AUTOPILOT_STOP_HANDLER}


def test_only_the_owner_command_resumes_the_sender() -> None:
    callers = {
        name
        for name, tree in _modules()
        if len(_calls_named(tree, RESUME_METHOD)) > _resume_calls_on(tree, PAUSE_ACTIONS_NAME)
    }

    assert callers == {OWNER_COMMAND_MODULE, AUTOPILOT_COMMAND_MODULE}


def _names_used(tree: ast.AST) -> set[str]:
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    return names | {alias for _, alias in _imported(tree)}


def test_the_sender_and_the_autopilot_never_touch_each_others_state() -> None:
    sender = ast.parse((SOURCE_ROOT / SENDER_MODULE).read_text())
    job = ast.parse((SOURCE_ROOT / REPLY_JOB_MODULE).read_text())
    command = ast.parse((SOURCE_ROOT / AUTOPILOT_COMMAND_MODULE).read_text())

    assert "AutopilotStateRepository" not in _names_used(sender)
    assert "SenderStateRepository" not in _names_used(job)
    assert "SenderStateRepository" not in _names_used(command)
    assert "AutopilotStateRepository" in _names_used(job)


def test_the_autopilot_state_repository_has_exactly_its_four_methods() -> None:
    assert _members(AutopilotStateRepository) == {
        "get",
        PAUSE_METHOD,
        STOP_METHOD,
        RESUME_METHOD,
    }


def test_the_reply_job_calls_the_notifier_in_one_place_behind_the_deadline() -> None:
    source = (SOURCE_ROOT / REPLY_JOB_MODULE).read_text()

    assert len(re.findall(r"\.notify\(", source)) == 1
    assert re.search(
        r"asyncio\.timeout\(self\._notify_deadline\):\s+await self\._notifier\.notify\(",
        source,
    )


def _autopilot_modules() -> list[str]:
    return [
        name
        for name, _ in _modules()
        if name in {REPLY_JOB_MODULE, AUTOPILOT_MODULE, AUTOPILOT_COMMAND_MODULE}
        or name.split("/")[0] == WORKER_PACKAGE
    ]


def test_the_autopilot_path_never_touches_the_transport() -> None:
    modules = _autopilot_modules()

    assert REPLY_JOB_MODULE in modules
    for name in modules:
        tree = ast.parse((SOURCE_ROOT / name).read_text())
        referenced = (_attributes_used(tree) - CALL_ONLY_WRITE_METHODS) | _called_attributes(tree)
        assert referenced.isdisjoint(TRANSPORT_METHODS | TELETHON_WRITE_METHODS), name
        assert _names_passed_to_getattr(tree).isdisjoint(TRANSPORT_METHODS), name


def test_the_worker_package_imports_neither_telethon_nor_the_userbot() -> None:
    for name, tree in _modules():
        if name.split("/")[0] != WORKER_PACKAGE:
            continue
        for module, _ in _imported(tree):
            assert module.split(".")[0] != "telethon", name
            assert not module.startswith("cyrano.userbot"), name


def test_only_the_worker_package_imports_arq() -> None:
    importers = {
        name.split("/")[0]
        for name, tree in _modules()
        if any(module.split(".")[0] == "arq" for module, _ in _imported(tree))
    }

    assert importers == {WORKER_PACKAGE}


def test_outside_the_worker_only_the_app_imports_the_queue_module_and_nothing_else_of_it() -> None:
    importers: dict[str, set[str]] = {}
    for name, tree in _modules():
        if name.split("/")[0] == WORKER_PACKAGE:
            continue
        for module, _ in _imported(tree):
            if module == "cyrano.worker" or module.startswith("cyrano.worker."):
                importers.setdefault(name, set()).add(module)

    assert importers == {"main.py": {APP_QUEUE_MODULE}}


def test_the_sender_reads_its_state_before_it_claims_a_row() -> None:
    tree = ast.parse((SOURCE_ROOT / SENDER_MODULE).read_text())
    send_next = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "send_next"
    )
    calls = [
        node.func.attr
        for statement in send_next.body
        for node in ast.walk(statement)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    ]

    assert calls.index("get") < calls.index("claim_due")
    assert calls.count("claim_due") == 1


def test_only_the_owner_command_takes_a_failed_row_back() -> None:
    callers = {name for name, tree in _modules() if RETRY_METHOD in _called_attributes(tree)}

    assert callers == {OWNER_COMMAND_MODULE}


def _call_lines(tree: ast.AST, function_name: str, attribute: str) -> list[int]:
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == function_name
    )
    return sorted(
        node.lineno
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == attribute
    )


def test_only_the_reply_job_cancels_superseded_rows_and_only_before_it_enqueues() -> None:
    callers = {name for name, tree in _modules() if SUPERSEDE_METHOD in _called_attributes(tree)}
    tree = ast.parse((SOURCE_ROOT / REPLY_JOB_MODULE).read_text())

    assert callers == {REPLY_JOB_MODULE}
    [cancel_line] = _call_lines(tree, "_enqueue", SUPERSEDE_METHOD)
    [enqueue_line] = _call_lines(tree, "_enqueue", ENQUEUE_SEQUENCE_METHOD)
    assert cancel_line < enqueue_line
    assert _call_lines(tree, "_enqueue", ENQUEUE_METHOD) == []


def test_only_the_sender_schedules_and_cancels_follower_parts() -> None:
    for method in (SCHEDULE_FOLLOWERS_METHOD, CANCEL_ORPHANS_METHOD):
        callers = {name for name, tree in _modules() if method in _called_attributes(tree)}
        assert callers == {SENDER_MODULE}, method


def test_the_sender_cancels_orphans_before_it_claims_and_schedules_followers_after_the_send() -> (
    None
):
    tree = ast.parse((SOURCE_ROOT / SENDER_MODULE).read_text())

    [orphans] = _call_lines(tree, "send_next", CANCEL_ORPHANS_METHOD)
    [claim] = _call_lines(tree, "send_next", "claim_due")
    [send] = _call_lines(tree, DELIVER_METHOD, TRANSPORT_METHOD)
    [complete] = _call_lines(tree, DELIVER_METHOD, "complete_send")
    [followers] = _call_lines(tree, DELIVER_METHOD, SCHEDULE_FOLLOWERS_METHOD)
    assert orphans < claim
    assert send < complete < followers


def test_only_the_outbox_service_and_the_two_jobs_enqueue_a_sequence() -> None:
    callers = {
        name for name, tree in _modules() if ENQUEUE_SEQUENCE_METHOD in _called_attributes(tree)
    }

    assert callers == {OUTBOX_SERVICE_MODULE, REPLY_JOB_MODULE, PROACTIVE_JOB_MODULE}


def test_the_predecessor_reason_is_used_only_by_the_repository() -> None:
    users = {name for name, tree in _modules() if PREDECESSOR_REASON in _imported_names(tree)}

    assert users == {DB_REPOSITORIES_MODULE}


def test_the_superseded_reason_is_used_only_by_the_domain_the_reply_job_and_the_sender() -> None:
    users = {name for name, tree in _modules() if SUPERSEDE_REASON in _imported_names(tree)}

    assert users == {REPLY_JOB_MODULE, SENDER_MODULE}


def test_the_sender_checks_supersession_once_before_the_pre_send_steps_and_never_after() -> None:
    tree = ast.parse((SOURCE_ROOT / SENDER_MODULE).read_text())

    [check] = _call_lines(tree, DELIVER_METHOD, SUPERSEDE_CHECK)
    [pre_send] = _call_lines(tree, DELIVER_METHOD, BEFORE_SEND_METHOD)
    guards = _call_lines(tree, DELIVER_METHOD, GUARD_METHOD)
    [send] = _call_lines(tree, DELIVER_METHOD, TRANSPORT_METHOD)
    assert guards[0] < check < pre_send < guards[-1] < send


def test_the_scans_catch_what_they_look_for() -> None:
    bad = ast.parse(
        "from telethon.tl.functions.messages import SendMessageRequest\n"
        "async def go(client, transport):\n"
        "    await client.send_file(1, 'x')\n"
        "    await client.action(1, 'typing')\n"
        "    await getattr(client, 'send_message')(1, 'x')\n"
        "    await transport.send_text(1, 'x')\n"
        "    await client(SendMessageRequest())\n"
        "    await state.stop('FakeKind')\n"
        "    await state.pause_until(until, 'FakeKind', now=now)\n"
        "    await bot.stop()\n"
        "    await client.send_read_acknowledge(1)\n"
        "    await transport.show_typing(1, 2)\n"
        "    await getattr(transport, 'mark_read')(1)\n"
    )

    assert {"send_file", "action", "send_message"} <= _telethon_write_uses(bad)
    assert _telethon_write_uses(ast.parse("request.action == 1\n")) == set()
    assert "send_file" in _attributes_used(bad)
    assert "send_message" in _names_passed_to_getattr(bad)
    assert TRANSPORT_METHOD in _called_attributes(bad)
    assert TYPING_METHOD in _called_attributes(bad)
    assert READ_METHOD in _names_passed_to_getattr(bad)
    assert "send_read_acknowledge" in _telethon_write_uses(bad)
    assert len(_calls_with_arguments(bad, STOP_METHOD)) == 1
    assert PAUSE_METHOD in _called_attributes(bad)
    assert ("telethon.tl.functions.messages", "SendMessageRequest") in set(_imported(bad))


def test_the_follower_scans_catch_what_they_look_for() -> None:
    bad = ast.parse(
        "async def go(outbox):\n"
        "    await outbox.schedule_followers(1, now)\n"
        "    await outbox.cancel_orphaned_followers()\n"
        "    await outbox.enqueue_sequence(1, ['x'], now, source)\n"
        "from cyrano.domain.outbox import CANCEL_REASON_PREDECESSOR\n"
    )

    assert {
        SCHEDULE_FOLLOWERS_METHOD,
        CANCEL_ORPHANS_METHOD,
        ENQUEUE_SEQUENCE_METHOD,
    } <= _called_attributes(bad)
    assert PREDECESSOR_REASON in _imported_names(bad)


def _functions_named(tree: ast.AST, names: frozenset[str]) -> list[ast.AsyncFunctionDef]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name in names
    ]


def test_the_registry_and_the_scheduler_never_reference_the_transport() -> None:
    for name in (EARLY_READ_MODULE, EARLY_READ_DOMAIN_MODULE):
        tree = ast.parse((SOURCE_ROOT / name).read_text())
        referenced = (_attributes_used(tree) - CALL_ONLY_WRITE_METHODS) | _called_attributes(tree)
        assert referenced.isdisjoint(TRANSPORT_METHODS | TELETHON_WRITE_METHODS), name
        assert _names_passed_to_getattr(tree).isdisjoint(TRANSPORT_METHODS), name
        assert "MessageTransport" not in _names_used(tree), name
        assert "_transport" not in _attributes_used(tree), name
        assert not any(module.startswith("cyrano.userbot") for module, _ in _imported(tree)), name


def test_only_the_sender_calls_mark_read_and_only_from_the_pre_send_part_and_the_early_read() -> (
    None
):
    tree = ast.parse((SOURCE_ROOT / SENDER_MODULE).read_text())
    callers = {
        name
        for name, module in _modules()
        if READ_METHOD in _called_attributes(module)
        or READ_METHOD in _names_passed_to_getattr(module)
    }

    assert callers == {SENDER_MODULE}
    assert _callers_in_module(tree, READ_METHOD) == {BEFORE_SEND_METHOD, EARLY_READ_EXECUTION}


def test_the_early_read_reaches_the_transport_only_through_the_senders_own_attribute() -> None:
    tree = ast.parse((SOURCE_ROOT / SENDER_MODULE).read_text())
    [execution] = _functions_named(tree, frozenset({EARLY_READ_EXECUTION}))

    called = _called_attributes(execution)

    assert called & TRANSPORT_METHODS == {READ_METHOD}
    receivers = {
        node.func.value.attr
        for node in ast.walk(execution)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == READ_METHOD
        and isinstance(node.func.value, ast.Attribute)
    }
    assert receivers == {"_transport"}


def test_the_early_read_is_not_a_message_and_never_touches_the_outbox_or_the_limiter() -> None:
    tree = ast.parse((SOURCE_ROOT / SENDER_MODULE).read_text())
    functions = _functions_named(tree, EARLY_READ_FUNCTIONS)

    assert {function.name for function in functions} == EARLY_READ_FUNCTIONS
    for function in functions:
        assert _attributes_used(function).isdisjoint(EARLY_READ_FORBIDDEN_RECEIVERS), function.name
        assert {TRANSPORT_METHOD, TYPING_METHOD}.isdisjoint(_called_attributes(function))


def test_the_early_read_shares_the_flood_and_account_paths_of_the_row_loop() -> None:
    tree = ast.parse((SOURCE_ROOT / SENDER_MODULE).read_text())

    assert _callers_in_module(tree, PAUSE_METHOD) == {FLOOD_WAIT_HANDLER}
    assert _callers_in_module(tree, STOP_METHOD) == {"_stop_for_account"}
    assert FLOOD_WAIT_HANDLER in {
        node.func.attr
        for function in _functions_named(tree, frozenset({"_react_to_early_read_failure"}))
        for node in ast.walk(function)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }


def test_the_sender_holds_no_lock_between_its_two_coroutines() -> None:
    tree = ast.parse((SOURCE_ROOT / SENDER_MODULE).read_text())
    names = _attributes_used(tree) | {
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
    }

    assert {"Lock", "Semaphore", "Condition"}.isdisjoint(names)


def test_the_intake_asks_the_early_read_scheduler_last() -> None:
    tree = ast.parse((SOURCE_ROOT / INTAKE_MODULE).read_text())

    [stop_check] = _call_lines(tree, INTAKE_HANDLE, INTAKE_STOP_CHECK)
    asks = _call_lines(tree, INTAKE_HANDLE, ON_INCOMING)
    publish = _call_lines(tree, INTAKE_HANDLE, "publish")
    assert len(asks) == 2
    assert stop_check < asks[0] < asks[1] < publish[0]
    handle = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == INTAKE_HANDLE
    )
    receivers = [
        node.func.value.attr
        for node in sorted(ast.walk(handle), key=lambda n: getattr(n, "lineno", 0))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == ON_INCOMING
        and isinstance(node.func.value, ast.Attribute)
    ]
    assert receivers == ["_scheduler", "_early_read"]
