from __future__ import annotations

import ast
import re
import typing
from pathlib import Path

from cyrano.userbot.protocols import IncomingEvent, LoginClient, SenderLike, UserbotClient

USERBOT_ROOT = Path(__file__).resolve().parents[2] / "src" / "cyrano" / "userbot"
ALLOWED_CLIENT_MEMBERS = frozenset(
    {
        "disconnected",
        "connect",
        "disconnect",
        "is_user_authorized",
        "catch_up",
        "add_event_handler",
        "remove_event_handler",
    }
)
ALLOWED_EVENT_MEMBERS = frozenset(
    {
        "id",
        "date",
        "out",
        "is_private",
        "is_group",
        "is_channel",
        "sender_id",
        "raw_text",
        "get_sender",
    }
)
ALLOWED_SENDER_MEMBERS = frozenset(
    {"id", "username", "first_name", "last_name", "bot", "deleted", "is_self"}
)
WRITE_NAME = re.compile(
    r"^(?:(?:send|mark|edit|delete|forward|pin|unpin|upload|react|kick|ban|block|unblock|join|"
    r"leave|report|archive|clear|click|set|request)_\w+|"
    r"send|reply|respond|edit|delete|forward|upload|invoke)$"
)
ALLOWED_WRITE_LOOKING_NAMES = {
    "login.py": {"send_code_request"},
    "protocols.py": {"send_code_request"},
}


def _members(protocol: type) -> set[str]:
    return set(typing.get_protocol_members(protocol))


def test_the_client_protocol_exposes_exactly_the_read_and_lifecycle_surface() -> None:
    assert _members(UserbotClient) == ALLOWED_CLIENT_MEMBERS


def test_no_client_protocol_member_is_a_write_method() -> None:
    for member in _members(UserbotClient):
        assert not WRITE_NAME.match(member), member


def test_the_event_and_sender_protocols_expose_only_read_access() -> None:
    assert _members(IncomingEvent) == ALLOWED_EVENT_MEMBERS
    assert _members(SenderLike) == ALLOWED_SENDER_MEMBERS


def test_the_login_protocol_is_the_only_one_that_can_ask_for_a_code() -> None:
    assert _members(LoginClient) == {
        "connect",
        "disconnect",
        "is_user_authorized",
        "send_code_request",
        "sign_in",
    }


def _used_names(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            names.add(node.name)
    return names


def test_no_userbot_module_uses_a_write_looking_name() -> None:
    modules = sorted(USERBOT_ROOT.glob("*.py"))
    assert len(modules) >= 7
    for module in modules:
        allowed = ALLOWED_WRITE_LOOKING_NAMES.get(module.name, set())
        offending = {
            name for name in _used_names(module) if WRITE_NAME.match(name) and name not in allowed
        }
        assert offending == set(), module.name


def test_the_scan_catches_a_send_call(tmp_path: Path) -> None:
    module = tmp_path / "bad.py"
    module.write_text("async def go(client):\n    await client.send_message(1, 'x')\n")

    assert {name for name in _used_names(module) if WRITE_NAME.match(name)} == {"send_message"}
