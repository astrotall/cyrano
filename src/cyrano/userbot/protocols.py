"""The small surface of Telethon the application uses, so tests need no real Telethon objects."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from datetime import datetime
from typing import Protocol


class SenderLike(Protocol):
    @property
    def id(self) -> int: ...

    @property
    def username(self) -> str | None: ...

    @property
    def first_name(self) -> str | None: ...

    @property
    def last_name(self) -> str | None: ...

    @property
    def bot(self) -> bool | None: ...

    @property
    def deleted(self) -> bool | None: ...

    @property
    def is_self(self) -> bool | None: ...


class IncomingEvent(Protocol):
    @property
    def id(self) -> int: ...

    @property
    def date(self) -> datetime: ...

    @property
    def out(self) -> bool: ...

    @property
    def is_private(self) -> bool | None: ...

    @property
    def is_group(self) -> bool | None: ...

    @property
    def is_channel(self) -> bool | None: ...

    @property
    def sender_id(self) -> int | None: ...

    @property
    def chat_id(self) -> int | None: ...

    @property
    def raw_text(self) -> str | None: ...

    async def get_sender(self) -> SenderLike | None: ...

    async def get_chat(self) -> SenderLike | None: ...


class FullUserInfo(Protocol):
    @property
    def about(self) -> str | None: ...


class FullUserResponse(Protocol):
    @property
    def full_user(self) -> FullUserInfo: ...


class HistoryMessage(Protocol):
    @property
    def id(self) -> int: ...

    @property
    def out(self) -> bool | None: ...

    @property
    def raw_text(self) -> str | None: ...

    @property
    def entities(self) -> Sequence[object] | None: ...

    @property
    def photo(self) -> object | None: ...

    @property
    def grouped_id(self) -> int | None: ...


EventCallback = Callable[[IncomingEvent], Awaitable[None]]


class UserbotClient(Protocol):
    @property
    def disconnected(self) -> asyncio.Future[object]: ...

    async def connect(self) -> None: ...

    def disconnect(self) -> Awaitable[None]: ...

    async def is_user_authorized(self) -> bool: ...

    async def catch_up(self) -> None: ...

    def add_event_handler(self, callback: EventCallback, event: object) -> None: ...

    def remove_event_handler(self, callback: EventCallback) -> int: ...

    def get_full_user(self, tg_id: int) -> Awaitable[FullUserResponse]: ...

    async def get_entity(self, entity: int | str) -> SenderLike: ...

    def download_profile_photo(
        self, entity: SenderLike, file: str, *, download_big: bool = True
    ) -> Awaitable[str | None]: ...

    def download_media(self, message: HistoryMessage, file: str) -> Awaitable[str | None]: ...

    def iter_messages(
        self,
        entity: SenderLike,
        limit: int | None = None,
        *,
        search: str | None = None,
        offset_id: int = 0,
    ) -> AsyncIterator[HistoryMessage]: ...


class LoginClient(Protocol):
    async def connect(self) -> None: ...

    def disconnect(self) -> Awaitable[None]: ...

    async def is_user_authorized(self) -> bool: ...

    async def send_code_request(self, phone: str) -> object: ...

    async def sign_in(
        self, phone: str | None = None, code: str | None = None, *, password: str | None = None
    ) -> object: ...
