from __future__ import annotations

import asyncio
import contextlib
import os
from dataclasses import dataclass
from pathlib import Path

from telethon import TelegramClient

from cyrano.config import Settings
from cyrano.services.message_transport import MessageTransport
from cyrano.userbot.adapter import TelethonAdapter
from cyrano.userbot.errors import (
    ConnectFailed,
    ConnectTimedOut,
    SessionMissing,
    SessionNotAuthorized,
)
from cyrano.userbot.protocols import LoginClient, UserbotClient

SESSION_EXTENSION = ".session"
SESSION_FILE_MODE = 0o600
SESSION_UMASK = 0o077
CONNECT_TIMEOUT_SECONDS = 30.0
DISCONNECT_TIMEOUT_SECONDS = 10.0
REQUEST_TIMEOUT_SECONDS = 10
CONNECTION_RETRIES = 3
REQUEST_RETRIES = 3
RETRY_DELAY_SECONDS = 1


@dataclass(frozen=True, slots=True)
class UserbotAccess:
    client: UserbotClient
    transport: MessageTransport


def session_file(session_path: Path) -> Path:
    name = str(session_path)
    return Path(name if name.endswith(SESSION_EXTENSION) else name + SESSION_EXTENSION)


def ensure_session_exists(session_path: Path) -> None:
    if not session_file(session_path).is_file():
        raise SessionMissing


def secure_session_file(session_path: Path) -> None:
    with contextlib.suppress(FileNotFoundError):
        os.chmod(session_file(session_path), SESSION_FILE_MODE)


def _telegram_client(settings: Settings) -> TelegramClient:
    settings.userbot_session_path.parent.mkdir(parents=True, exist_ok=True)
    return TelegramClient(
        str(settings.userbot_session_path),
        settings.telegram_api_id,
        settings.telegram_api_hash.get_secret_value(),
        timeout=REQUEST_TIMEOUT_SECONDS,
        connection_retries=CONNECTION_RETRIES,
        request_retries=REQUEST_RETRIES,
        retry_delay=RETRY_DELAY_SECONDS,
        auto_reconnect=True,
        sequential_updates=True,
        catch_up=True,
    )


def build_access(settings: Settings) -> UserbotAccess:
    adapter = TelethonAdapter(_telegram_client(settings))
    return UserbotAccess(client=adapter, transport=adapter)


def build_login_client(settings: Settings) -> LoginClient:
    client: LoginClient = _telegram_client(settings)
    return client


async def disconnect_client(client: UserbotClient | LoginClient) -> None:
    with contextlib.suppress(Exception):
        async with asyncio.timeout(DISCONNECT_TIMEOUT_SECONDS):
            await client.disconnect()


async def connect_client(client: UserbotClient | LoginClient) -> bool:
    try:
        async with asyncio.timeout(CONNECT_TIMEOUT_SECONDS):
            await client.connect()
            return await client.is_user_authorized()
    except TimeoutError:
        await disconnect_client(client)
        raise ConnectTimedOut(CONNECT_TIMEOUT_SECONDS) from None
    except OSError as exc:
        await disconnect_client(client)
        raise ConnectFailed(type(exc).__name__) from None


async def connect_authorized(client: UserbotClient) -> None:
    if not await connect_client(client):
        await disconnect_client(client)
        raise SessionNotAuthorized
