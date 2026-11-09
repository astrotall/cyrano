from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime

from telethon import TelegramClient
from telethon.errors import (
    ActiveUserRequiredError,
    AuthKeyDuplicatedError,
    AuthKeyInvalidError,
    AuthKeyPermEmptyError,
    AuthKeyUnregisteredError,
    FloodPremiumWaitError,
    FloodTestPhoneWaitError,
    FloodWaitError,
    ForbiddenError,
    FrozenMethodInvalidError,
    InputUserDeactivatedError,
    MessageTooLongError,
    PeerFloodError,
    PeerIdInvalidError,
    PeerIdNotSupportedError,
    SessionExpiredError,
    SessionPasswordNeededError,
    SessionRevokedError,
    SlowModeWaitError,
    UnauthorizedError,
    UserBlockedError,
    UserDeactivatedBanError,
    UserDeactivatedError,
    UserIdInvalidError,
    UserIsBlockedError,
    UserRestrictedError,
    YouBlockedUserError,
)
from telethon.tl.functions.users import GetFullUserRequest

from cyrano.domain.enums import RejectionReason
from cyrano.domain.errors import (
    SendAccountBlocked,
    SendFailure,
    SendFloodWait,
    SendRejected,
    SendUncertain,
)
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
ACCOUNT_BLOCKS = (
    PeerFloodError,
    UserDeactivatedError,
    UserDeactivatedBanError,
    AuthKeyUnregisteredError,
    AuthKeyInvalidError,
    SessionRevokedError,
    SessionExpiredError,
    UserRestrictedError,
    FrozenMethodInvalidError,
    AuthKeyDuplicatedError,
    AuthKeyPermEmptyError,
    ActiveUserRequiredError,
    SessionPasswordNeededError,
    UnauthorizedError,
)
CONTENT_REFUSALS = (MessageTooLongError,)
RECIPIENT_REFUSALS = (
    ForbiddenError,
    InputUserDeactivatedError,
    PeerIdInvalidError,
    PeerIdNotSupportedError,
    UserBlockedError,
    UserIdInvalidError,
    UserIsBlockedError,
    YouBlockedUserError,
)
PEER_NOT_FOUND = "PeerNotFound"
MISSING_RESPONSE = "MissingSentMessage"
TYPING_ACTION = "typing"
ENTITY_REFRESH_MIN_INTERVAL_SECONDS = 60
ENTITY_REFRESH_DEADLINE_SECONDS = 60
ENTITY_REFRESH_LIMIT = 500
ENTITY_WARMUP_LIMIT = 500

logger = logging.getLogger("cyrano.userbot")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def classify_failure(error: Exception) -> SendFailure:
    name = type(error).__name__
    if isinstance(error, FLOOD_WAITS):
        return SendFloodWait(name, error.seconds)
    if isinstance(error, ACCOUNT_BLOCKS):
        return SendAccountBlocked(name)
    if isinstance(error, CONTENT_REFUSALS):
        return SendRejected(name, RejectionReason.CONTENT)
    if isinstance(error, RECIPIENT_REFUSALS):
        return SendRejected(name, RejectionReason.RECIPIENT)
    return SendUncertain(name)


class TelethonAdapter:
    def __init__(
        self,
        client: TelegramClient,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._client = client
        self._sleep = sleep
        self._clock = clock
        self._refresh_lock = asyncio.Lock()
        self._last_refresh_at: datetime | None = None
        self._failed_refreshes = 0

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

    async def refresh_entities(self, limit: int) -> int:
        async with self._refresh_lock:
            if self._refresh_is_recent():
                logger.info("entity refresh skipped", extra={"refreshed": False})
                return 0
            started = self._clock()
            try:
                async with asyncio.timeout(ENTITY_REFRESH_DEADLINE_SECONDS):
                    seen = await self._iterate_dialogs(limit)
            except Exception as exc:
                self._failed_refreshes += 1
                logger.warning(
                    "entity refresh failed",
                    extra={
                        "refreshed": False,
                        "error": type(exc).__name__,
                        "failed_refreshes": self._failed_refreshes,
                    },
                )
                return 0
            finally:
                self._last_refresh_at = self._clock()
            logger.info(
                "entity refresh finished",
                extra={
                    "refreshed": True,
                    "dialogs": seen,
                    "duration_seconds": (self._clock() - started).total_seconds(),
                },
            )
            return seen

    def _refresh_is_recent(self) -> bool:
        if self._last_refresh_at is None:
            return False
        elapsed = (self._clock() - self._last_refresh_at).total_seconds()
        return elapsed < ENTITY_REFRESH_MIN_INTERVAL_SECONDS

    async def _iterate_dialogs(self, limit: int) -> int:
        seen = 0
        async for _ in self._client.iter_dialogs(limit):
            seen += 1
        return seen

    async def _input_entity(self, tg_id: int) -> object:
        try:
            peer: object = await self._client.get_input_entity(tg_id)
        except ValueError:
            raise
        except Exception as exc:
            raise classify_failure(exc) from None
        return peer

    async def _peer(self, tg_id: int) -> object:
        try:
            return await self._input_entity(tg_id)
        except ValueError:
            pass
        await self.refresh_entities(ENTITY_REFRESH_LIMIT)
        try:
            return await self._input_entity(tg_id)
        except ValueError:
            raise SendRejected(PEER_NOT_FOUND, RejectionReason.RECIPIENT) from None

    async def send_text(self, tg_id: int, text: str) -> SentMessage:
        peer = await self._peer(tg_id)
        try:
            sent = await self._client.send_message(peer, text, parse_mode=None, link_preview=False)
        except Exception as exc:
            raise classify_failure(exc) from None
        if sent is None:
            raise SendUncertain(MISSING_RESPONSE)
        return SentMessage(tg_message_id=sent.id, sent_at=sent.date)

    async def show_typing(self, tg_id: int, seconds: float) -> None:
        peer = await self._peer(tg_id)
        try:
            async with self._client.action(peer, TYPING_ACTION, auto_cancel=False):
                await self._sleep(seconds)
        except Exception as exc:
            raise classify_failure(exc) from None

    async def mark_read(self, tg_id: int) -> None:
        peer = await self._peer(tg_id)
        try:
            await self._client.send_read_acknowledge(peer)
        except Exception as exc:
            raise classify_failure(exc) from None
