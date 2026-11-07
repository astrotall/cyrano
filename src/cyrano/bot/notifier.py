from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from cyrano.bot import messages
from cyrano.bot.callbacks import PauseAction, PauseCallback
from cyrano.bot.rendering import hand_over_keyboard, render_hand_over
from cyrano.domain.enums import LimitScope, LimitWindow
from cyrano.domain.stop_triggers import HandOverNotice
from cyrano.services.outbox_sender import SenderNotices
from cyrano.services.reply_job import AutopilotNotices

logger = logging.getLogger("cyrano.bot.notifier")

OUTBOX_NOTICES = SenderNotices(
    interrupted=messages.OUTBOX_INTERRUPTED,
    failed=messages.OUTBOX_FAILED,
    uncertain=messages.OUTBOX_UNCERTAIN,
    flood_wait=messages.OUTBOX_FLOOD_WAIT,
    account_stopped=messages.OUTBOX_ACCOUNT_STOPPED,
    contact_paused=messages.OUTBOX_CONTACT_PAUSED,
    stale_summary=messages.OUTBOX_STALE_SUMMARY,
    stop_reminder=messages.OUTBOX_STOP_REMINDER,
    limit_reached={
        (LimitScope.GLOBAL, LimitWindow.HOUR): messages.OUTBOX_LIMIT_GLOBAL_HOUR,
        (LimitScope.GLOBAL, LimitWindow.DAY): messages.OUTBOX_LIMIT_GLOBAL_DAY,
        (LimitScope.CONTACT, LimitWindow.HOUR): messages.OUTBOX_LIMIT_CONTACT_HOUR,
        (LimitScope.CONTACT, LimitWindow.DAY): messages.OUTBOX_LIMIT_CONTACT_DAY,
    },
)

AUTOPILOT_NOTICES = AutopilotNotices(
    stopped=messages.AUTOPILOT_STOPPED,
    paused=messages.AUTOPILOT_PAUSED,
    stop_reasons=messages.AUTOPILOT_STOP_REASONS,
    pause_reasons=messages.AUTOPILOT_PAUSE_REASONS,
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


class TelegramManualPauseNotifier:
    def __init__(self, bot: Bot, owner_id: int) -> None:
        self._bot = bot
        self._owner_id = owner_id

    async def notify_manual_pause(self, contact_id: int) -> None:
        button = InlineKeyboardButton(
            text=messages.RESUME_BUTTON,
            callback_data=PauseCallback(action=PauseAction.RESUME, contact_id=contact_id).pack(),
        )
        try:
            await self._bot.send_message(
                chat_id=self._owner_id,
                text=messages.MANUAL_PAUSE_NOTICE.format(contact_id=contact_id),
                parse_mode=None,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[button]]),
            )
        except TelegramAPIError as exc:
            logger.warning("manual pause notice was not sent", extra={"error": type(exc).__name__})


class TelegramHandOverNotifier:
    def __init__(self, bot: Bot, owner_id: int) -> None:
        self._bot = bot
        self._owner_id = owner_id

    async def notify_hand_over(self, notice: HandOverNotice) -> None:
        try:
            await self._bot.send_message(
                chat_id=self._owner_id,
                text=render_hand_over(notice),
                parse_mode=ParseMode.HTML,
                reply_markup=hand_over_keyboard(notice.contact_id),
            )
        except TelegramAPIError as exc:
            logger.warning("hand-over notice was not sent", extra={"error": type(exc).__name__})
