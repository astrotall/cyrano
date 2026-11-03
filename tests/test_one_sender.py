from __future__ import annotations

import ast
import re
import typing
from collections.abc import Iterator
from pathlib import Path

from cyrano.services.message_transport import MessageTransport
from cyrano.services.owner_notifier import OwnerNotifier
from cyrano.userbot.protocols import UserbotClient

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src" / "cyrano"
SENDER_MODULE = "services/outbox_sender.py"
ADAPTER_MODULE = "userbot/adapter.py"
USERBOT_PACKAGE = "userbot"
BOT_PACKAGE = "bot"
OWNER_CHAT_MODULES = {"bot/sender.py", "bot/notifier.py"}
TRANSPORT_METHOD = "send_text"
RESCHEDULE_METHOD = "reschedule"
FLOOD_WAIT_HANDLER = "_handle_flood_wait"
RETRY_METHOD = "retry_failed"
OWNER_COMMAND_MODULE = "outbox_cli.py"
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


def _members(protocol: type) -> set[str]:
    return set(typing.get_protocol_members(protocol))


def test_the_transport_has_exactly_one_method() -> None:
    assert _members(MessageTransport) == {TRANSPORT_METHOD}


def test_the_notifier_has_exactly_one_method() -> None:
    assert _members(OwnerNotifier) == {"notify"}


def test_the_read_client_protocol_has_no_write_member() -> None:
    members = _members(UserbotClient)

    assert TRANSPORT_METHOD not in members
    assert "__call__" not in members
    assert {member for member in members if WRITE_LOOKING_MEMBER.match(member)} == set()
    assert members & TELETHON_WRITE_METHODS == set()


def test_only_the_sender_calls_the_transport() -> None:
    callers = {
        name
        for name, tree in _modules()
        if TRANSPORT_METHOD in _called_attributes(tree)
        or TRANSPORT_METHOD in _names_passed_to_getattr(tree)
    }

    assert callers == {SENDER_MODULE}


def test_only_the_adapter_and_the_protocol_define_a_send_text() -> None:
    definers = {
        name
        for name, tree in _modules()
        if any(
            isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)
            and node.name == TRANSPORT_METHOD
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

    assert users == {ADAPTER_MODULE: {"send_message"}}


def test_inside_the_bot_package_only_the_owner_chat_modules_send() -> None:
    senders = {
        name
        for name, tree in _modules()
        if name.split("/")[0] == BOT_PACKAGE
        and _attributes_used(tree) & {"send_message", "send_photo", "send_media_group"}
    }

    assert senders == OWNER_CHAT_MODULES


def test_the_adapter_never_asks_for_a_write_beyond_send_message() -> None:
    tree = ast.parse((SOURCE_ROOT / ADAPTER_MODULE).read_text())

    assert _telethon_write_uses(tree) == {"send_message"}


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


def test_only_a_flood_wait_puts_a_row_back_automatically() -> None:
    modules = {
        name: _callers_in_module(tree, RESCHEDULE_METHOD)
        for name, tree in _modules()
        if RESCHEDULE_METHOD in _called_attributes(tree)
    }

    assert modules == {SENDER_MODULE: {FLOOD_WAIT_HANDLER}}


def test_only_the_owner_command_takes_a_failed_row_back() -> None:
    callers = {name for name, tree in _modules() if RETRY_METHOD in _called_attributes(tree)}

    assert callers == {OWNER_COMMAND_MODULE}


def test_the_scans_catch_what_they_look_for() -> None:
    bad = ast.parse(
        "from telethon.tl.functions.messages import SendMessageRequest\n"
        "async def go(client, transport):\n"
        "    await client.send_file(1, 'x')\n"
        "    await client.action(1, 'typing')\n"
        "    await getattr(client, 'send_message')(1, 'x')\n"
        "    await transport.send_text(1, 'x')\n"
        "    await client(SendMessageRequest())\n"
    )

    assert {"send_file", "action", "send_message"} <= _telethon_write_uses(bad)
    assert _telethon_write_uses(ast.parse("request.action == 1\n")) == set()
    assert "send_file" in _attributes_used(bad)
    assert "send_message" in _names_passed_to_getattr(bad)
    assert TRANSPORT_METHOD in _called_attributes(bad)
    assert ("telethon.tl.functions.messages", "SendMessageRequest") in set(_imported(bad))
