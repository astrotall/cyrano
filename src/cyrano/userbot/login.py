"""The interactive `login` command. Phone, code and password are only passed on, never kept."""

from __future__ import annotations

import getpass
import os
from collections.abc import Callable
from dataclasses import dataclass

from telethon.errors import RPCError, SessionPasswordNeededError

from cyrano.config import Settings
from cyrano.userbot.client import (
    SESSION_UMASK,
    build_login_client,
    connect_client,
    disconnect_client,
    secure_session_file,
    session_file,
)
from cyrano.userbot.errors import LoginFailed
from cyrano.userbot.protocols import LoginClient

PHONE_PROMPT = "Phone number of the second account (international format): "
CODE_PROMPT = "Login code sent by Telegram: "
TWO_STEP_PROMPT = "Two-step verification password: "
SESSION_SAVED_MESSAGE = "Session saved."
ALREADY_AUTHORIZED_MESSAGE = "The session is already authorized. Nothing to do."


def _ask_phone() -> str:
    return input(PHONE_PROMPT)


def _ask_code() -> str:
    return input(CODE_PROMPT)


def _ask_password() -> str:
    return getpass.getpass(TWO_STEP_PROMPT)


@dataclass(frozen=True, slots=True)
class LoginPrompts:
    phone: Callable[[], str]
    code: Callable[[], str]
    password: Callable[[], str]


TERMINAL_PROMPTS = LoginPrompts(phone=_ask_phone, code=_ask_code, password=_ask_password)


async def _sign_in(client: LoginClient, prompts: LoginPrompts, phone: str) -> None:
    await client.send_code_request(phone)
    code = prompts.code().strip()
    try:
        await client.sign_in(phone, code)
    except SessionPasswordNeededError:
        await client.sign_in(password=prompts.password())


async def run_login(
    settings: Settings,
    prompts: LoginPrompts = TERMINAL_PROMPTS,
    client_factory: Callable[[Settings], LoginClient] = build_login_client,
    echo: Callable[[str], None] = print,
) -> None:
    phone = prompts.phone().strip()
    file = session_file(settings.userbot_session_path)
    existed = file.exists()
    previous_umask = os.umask(SESSION_UMASK)
    try:
        client = client_factory(settings)
    finally:
        os.umask(previous_umask)
    signed_in = False
    try:
        if await connect_client(client):
            echo(ALREADY_AUTHORIZED_MESSAGE)
        else:
            await _sign_in(client, prompts, phone)
            signed_in = True
    except RPCError as exc:
        raise LoginFailed(type(exc).__name__) from None
    finally:
        await disconnect_client(client)
        if not existed and not signed_in:
            file.unlink(missing_ok=True)
    secure_session_file(settings.userbot_session_path)
    if signed_in:
        echo(SESSION_SAVED_MESSAGE)
