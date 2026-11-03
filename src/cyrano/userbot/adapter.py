"""The only module that touches the Telethon client directly, and the only one that sends."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable

from telethon import TelegramClient
from telethon.errors import (
    FloodPremiumWaitError,
    FloodTestPhoneWaitError,
    FloodWaitError,
    ForbiddenError,
    InputUserDeactivatedError,
    MessageTooLongError,
    PeerIdInvalidError,
    PeerIdNotSupportedError,
    SlowModeWaitError,
    UserBlockedError,
    UserIdInvalidError,
    UserIsBlockedError,
    YouBlockedUserError,
)
from telethon.tl.functions.users import GetFullUserRequest

from cyrano.domain.errors import SendFailure, SendFloodWait, SendRejected, SendUncertain
from cyrano.domain.outbox import SentMessage
from cyrano.userbot.protocols import (
    EventCallback,
    FullUserResponse,
    HistoryMessage,
    SenderLike,
)

FLOOD_WAITS = (
    FloodWaitError,
    FloodPremiumWaitError,
    FloodTestPhoneWaitError,
    SlowModeWaitError,
)
PERMANENT_REFUSALS = (
    ForbiddenError,
    InputUserDeactivatedError,
    MessageTooLongError,
    PeerIdInvalidError,
    PeerIdNotSupportedError,
    UserBlockedError,
    UserIdInvalidError,
    UserIsBlockedError,
    YouBlockedUserError,
)
PEER_NOT_FOUND = "PeerNotFound"
MISSING_RESPONSE = "MissingSentMessage"


def classify_failure(error: Exception) -> SendFailure:
    name = type(error).__name__
    if isinstance(error, FLOOD_WAITS):
        return SendFloodWait(name, error.seconds)
    if isinstance(error, PERMANENT_REFUSALS):
        return SendRejected(name)
    return SendUncertain(name)


class TelethonAdapter:
    def __init__(self, client: TelegramClient) -> None:
        self._client = client

    @property
    def disconnected(self) -> asyncio.Future[object]:
        future: asyncio.Future[object] = self._client.disconnected
        return future

    async def connect(self) -> None:
        await self._client.connect()

    def disconnect(self) -> Awaitable[None]:
        result: Awaitable[None] = self._client.disconnect()
        return result

    async def is_user_authorized(self) -> bool:
        authorized: bool = await self._client.is_user_authorized()
        return authorized

    async def catch_up(self) -> None:
        await self._client.catch_up()

    def add_event_handler(self, callback: EventCallback, event: object) -> None:
        self._client.add_event_handler(callback, event)

    def remove_event_handler(self, callback: EventCallback) -> int:
        removed: int = self._client.remove_event_handler(callback)
        return removed

    def get_full_user(self, tg_id: int) -> Awaitable[FullUserResponse]:
        response: Awaitable[FullUserResponse] = self._client(GetFullUserRequest(tg_id))
        return response

    async def get_entity(self, entity: int | str) -> SenderLike:
        found: SenderLike = await self._client.get_entity(entity)
        return found

    def download_profile_photo(
        self, entity: SenderLike, file: str, *, download_big: bool = True
    ) -> Awaitable[str | None]:
        saved: Awaitable[str | None] = self._client.download_profile_photo(
            entity, file=file, download_big=download_big
        )
        return saved

    def download_media(self, message: HistoryMessage, file: str) -> Awaitable[str | None]:
        saved: Awaitable[str | None] = self._client.download_media(message, file)
        return saved

    def iter_messages(
        self,
        entity: SenderLike,
        limit: int | None = None,
        *,
        search: str | None = None,
        offset_id: int = 0,
    ) -> AsyncIterator[HistoryMessage]:
        messages: AsyncIterator[HistoryMessage] = self._client.iter_messages(
            entity, limit, search=search, offset_id=offset_id
        )
        return messages

    async def send_text(self, tg_id: int, text: str) -> SentMessage:
        try:
            peer = await self._client.get_input_entity(tg_id)
        except ValueError:
            raise SendRejected(PEER_NOT_FOUND) from None
        except Exception as exc:
            raise classify_failure(exc) from None
        try:
            sent = await self._client.send_message(peer, text, parse_mode=None, link_preview=False)
        except Exception as exc:
            raise classify_failure(exc) from None
        if sent is None:
            raise SendUncertain(MISSING_RESPONSE)
        return SentMessage(tg_message_id=sent.id, sent_at=sent.date)
