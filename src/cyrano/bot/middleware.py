"""Owner-only access: updates from anyone else are dropped without a trace."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, User

logger = logging.getLogger("cyrano.bot.middleware")

EVENT_USER_KEY = "event_from_user"


class OwnerOnlyMiddleware(BaseMiddleware):
    def __init__(self, owner_id: int) -> None:
        self._owner_id = owner_id
        self._dropped = 0

    @property
    def dropped(self) -> int:
        return self._dropped

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, object]], Awaitable[object]],
        event: TelegramObject,
        data: dict[str, object],
    ) -> object:
        user = data.get(EVENT_USER_KEY)
        if not isinstance(user, User) or user.id != self._owner_id:
            self._dropped += 1
            logger.debug("update from a non-owner dropped", extra={"dropped": self._dropped})
            return None
        return await handler(event, data)
