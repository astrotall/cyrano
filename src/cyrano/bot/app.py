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
from cyrano.bot.notifier import (
    TelegramHandOverNotifier,
    TelegramManualPauseNotifier,
    TelegramOwnerNotifier,
)
from cyrano.bot.sender import TelegramCardSender
from cyrano.config import Settings
from cyrano.services.card_actions import CardActions
from cyrano.services.pause_actions import PauseActions

logger = logging.getLogger("cyrano.bot")

STOP_POLLING_TIMEOUT_SECONDS = 5.0


def build_dispatcher(
    owner_id: int, actions: CardActions, pause_actions: PauseActions
) -> Dispatcher:
    dispatcher = Dispatcher()
    dispatcher.update.outer_middleware(OwnerOnlyMiddleware(owner_id))
    dispatcher.include_router(build_router(actions, pause_actions))
    return dispatcher


def build_bot(settings: Settings, session: BaseSession | None = None) -> Bot:
    try:
        return Bot(
            token=settings.bot_token.get_secret_value(),
            session=session,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
        )
    except TokenValidationError:
        raise BotTokenRejected from None


class NoticeBot:
    def __init__(self, settings: Settings, *, session: BaseSession | None = None) -> None:
        self._bot = build_bot(settings, session)
        self._notifier = TelegramOwnerNotifier(self._bot, settings.owner_telegram_id)
        self._hand_over_notifier = TelegramHandOverNotifier(self._bot, settings.owner_telegram_id)

    @property
    def notifier(self) -> TelegramOwnerNotifier:
        return self._notifier

    @property
    def hand_over_notifier(self) -> TelegramHandOverNotifier:
        return self._hand_over_notifier

    async def close(self) -> None:
        await self._bot.session.close()


class ControlBot:
    def __init__(
        self,
        settings: Settings,
        actions: CardActions,
        pause_actions: PauseActions,
        *,
        session: BaseSession | None = None,
    ) -> None:
        self._bot = build_bot(settings, session)
        self._dispatcher = build_dispatcher(settings.owner_telegram_id, actions, pause_actions)
        self._sender = TelegramCardSender(self._bot, settings.owner_telegram_id, settings.media_dir)
        self._notifier = TelegramOwnerNotifier(self._bot, settings.owner_telegram_id)
        self._manual_pause_notifier = TelegramManualPauseNotifier(
            self._bot, settings.owner_telegram_id
        )
        self._hand_over_notifier = TelegramHandOverNotifier(self._bot, settings.owner_telegram_id)

    @property
    def sender(self) -> TelegramCardSender:
        return self._sender

    @property
    def notifier(self) -> TelegramOwnerNotifier:
        return self._notifier

    @property
    def manual_pause_notifier(self) -> TelegramManualPauseNotifier:
        return self._manual_pause_notifier

    @property
    def hand_over_notifier(self) -> TelegramHandOverNotifier:
        return self._hand_over_notifier

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
