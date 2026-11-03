"""Plain notifications to the owner chat, the only chat this module can address."""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from cyrano.bot import messages
from cyrano.services.outbox_sender import SenderNotices

logger = logging.getLogger("cyrano.bot.notifier")

OUTBOX_NOTICES = SenderNotices(
    interrupted=messages.OUTBOX_INTERRUPTED,
    failed=messages.OUTBOX_FAILED,
    uncertain=messages.OUTBOX_UNCERTAIN,
    flood_wait=messages.OUTBOX_FLOOD_WAIT,
)


class TelegramOwnerNotifier:
    def __init__(self, bot: Bot, owner_id: int) -> None:
        self._bot = bot
        self._owner_id = owner_id

    async def notify(self, text: str) -> None:
        try:
            await self._bot.send_message(chat_id=self._owner_id, text=text, parse_mode=None)
        except TelegramAPIError as exc:
            logger.warning("owner notification was not sent", extra={"error": type(exc).__name__})
