"""Delivery of the card to the owner chat, the only chat this module can address."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter
from aiogram.types import FSInputFile, InputMediaPhoto

from cyrano.bot import messages
from cyrano.bot.rendering import card_keyboard, render_card
from cyrano.domain.models import ContactCard

logger = logging.getLogger("cyrano.bot.sender")

MAX_RETRY_AFTER_SECONDS = 60
CHAT_NOT_FOUND_MARKER = "chat not found"


@dataclass(frozen=True, slots=True)
class _Photo:
    path: Path
    caption: str


def _is_chat_not_found(error: TelegramBadRequest) -> bool:
    return CHAT_NOT_FOUND_MARKER in error.message.lower()


class TelegramCardSender:
    def __init__(
        self,
        bot: Bot,
        owner_id: int,
        media_dir: Path,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._bot = bot
        self._owner_id = owner_id
        self._media_dir = media_dir
        self._sleep = sleep
        self._sent = 0
        self._failed = 0
        self._photos_skipped = 0

    @property
    def sent(self) -> int:
        return self._sent

    @property
    def failed(self) -> int:
        return self._failed

    @property
    def photos_skipped(self) -> int:
        return self._photos_skipped

    async def send_card(self, card: ContactCard) -> bool:
        try:
            await self._send_photos(self._photos_of(card))
            await self._with_retry(
                lambda: self._bot.send_message(
                    chat_id=self._owner_id, text=render_card(card), reply_markup=card_keyboard(card)
                )
            )
        except TelegramAPIError as exc:
            self._failed += 1
            logger.warning(
                "card was not sent",
                extra={"error": type(exc).__name__, "sent": self._sent, "failed": self._failed},
            )
            return False
        self._sent += 1
        return True

    def _photos_of(self, card: ContactCard) -> list[_Photo]:
        candidates = (
            (card.profile_photo, messages.PROFILE_PHOTO_CAPTION),
            (card.card_photo, messages.CARD_PHOTO_CAPTION),
        )
        photos: list[_Photo] = []
        for relative, caption in candidates:
            if relative is None:
                continue
            path = self._readable_photo(relative)
            if path is None:
                self._photos_skipped += 1
            else:
                photos.append(_Photo(path, caption))
        return photos

    def _readable_photo(self, relative: str) -> Path | None:
        root = self._media_dir.resolve()
        path = (root / relative).resolve()
        try:
            usable = (
                path.is_relative_to(root)
                and path.is_file()
                and os.access(path, os.R_OK)
                and path.stat().st_size > 0
            )
        except OSError:
            return None
        return path if usable else None

    async def _send_photos(self, photos: list[_Photo]) -> None:
        if not photos:
            return
        try:
            await self._with_retry(lambda: self._send_photo_batch(photos))
        except TelegramBadRequest as exc:
            if _is_chat_not_found(exc):
                raise
            self._photos_skipped += len(photos)
        except OSError:
            self._photos_skipped += len(photos)

    async def _send_photo_batch(self, photos: list[_Photo]) -> None:
        if len(photos) == 1:
            only = photos[0]
            await self._bot.send_photo(
                chat_id=self._owner_id, photo=FSInputFile(only.path), caption=only.caption
            )
            return
        await self._bot.send_media_group(
            chat_id=self._owner_id,
            media=[InputMediaPhoto(media=FSInputFile(p.path), caption=p.caption) for p in photos],
        )

    async def _with_retry[T](self, call: Callable[[], Awaitable[T]]) -> T:
        try:
            return await call()
        except TelegramRetryAfter as first:
            if first.retry_after > MAX_RETRY_AFTER_SECONDS:
                raise
            await self._sleep(first.retry_after)
            return await call()
