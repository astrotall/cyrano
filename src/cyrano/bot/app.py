"""The control bot: bot, dispatcher and long polling, built from the settings."""

from __future__ import annotations

import asyncio
import contextlib
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.utils.token import TokenValidationError

from cyrano.bot.errors import BotTokenRejected, PollingFailed
from cyrano.bot.handlers import build_router
from cyrano.bot.middleware import OwnerOnlyMiddleware
from cyrano.bot.sender import TelegramCardSender
from cyrano.config import Settings
from cyrano.services.card_actions import CardActions

logger = logging.getLogger("cyrano.bot")

STOP_POLLING_TIMEOUT_SECONDS = 5.0


def build_dispatcher(owner_id: int, actions: CardActions) -> Dispatcher:
    dispatcher = Dispatcher()
    dispatcher.update.outer_middleware(OwnerOnlyMiddleware(owner_id))
    dispatcher.include_router(build_router(actions))
    return dispatcher


class ControlBot:
    def __init__(
        self, settings: Settings, actions: CardActions, *, session: BaseSession | None = None
    ) -> None:
        try:
            self._bot = Bot(
                token=settings.bot_token.get_secret_value(),
                session=session,
                default=DefaultBotProperties(
                    parse_mode=ParseMode.HTML, link_preview_is_disabled=True
                ),
            )
        except TokenValidationError:
            raise BotTokenRejected from None
        self._dispatcher = build_dispatcher(settings.owner_telegram_id, actions)
        self._sender = TelegramCardSender(self._bot, settings.owner_telegram_id, settings.media_dir)

    @property
    def sender(self) -> TelegramCardSender:
        return self._sender

    async def poll(self) -> None:
        try:
            await self._dispatcher.start_polling(
                self._bot, handle_signals=False, close_bot_session=False
            )
        except TelegramUnauthorizedError:
            raise BotTokenRejected from None
        except Exception as exc:
            raise PollingFailed(type(exc).__name__) from None

    async def stop(self) -> None:
        with contextlib.suppress(RuntimeError, TimeoutError):
            await asyncio.wait_for(self._dispatcher.stop_polling(), STOP_POLLING_TIMEOUT_SECONDS)
        await self._bot.session.close()
        logger.info("control bot stopped")
