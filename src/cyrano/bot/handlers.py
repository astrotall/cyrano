"""Handlers of the control bot: /start, the card buttons and the pause notice buttons."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, Message

from cyrano.bot import messages
from cyrano.bot.callbacks import CardAction, CardCallback, PauseAction, PauseCallback
from cyrano.bot.rendering import alert_text, append_decision
from cyrano.domain.enums import CardOutcome, ContactStatus, IgnoreOutcome, ResumeOutcome
from cyrano.domain.models import CardActionResult
from cyrano.services.card_actions import CardActions
from cyrano.services.pause_actions import PauseActions

logger = logging.getLogger("cyrano.bot.handlers")

DECIDED_OUTCOMES = frozenset({CardOutcome.APPLIED, CardOutcome.ALREADY_APPLIED})
RESUMED_OUTCOMES = frozenset({ResumeOutcome.APPLIED, ResumeOutcome.ALREADY_APPLIED})
IGNORED_OUTCOMES = frozenset({IgnoreOutcome.APPLIED, IgnoreOutcome.ALREADY_APPLIED})
KEPT_OUTCOME = "kept"


async def _apply(actions: CardActions, data: CardCallback) -> CardActionResult:
    if data.action is CardAction.IGNORE:
        return await actions.ignore(data.contact_id)
    if data.mode_id is None:
        return CardActionResult(outcome=CardOutcome.MODE_UNAVAILABLE, status=ContactStatus.PENDING)
    return await actions.reply(data.contact_id, data.mode_id)


async def _update_card(message: Message, result: CardActionResult) -> None:
    if message.text is None:
        return
    try:
        if result.outcome in DECIDED_OUTCOMES:
            await message.edit_text(text=append_decision(message.html_text, result))
        elif result.status is not ContactStatus.PENDING:
            await message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError as exc:
        logger.debug("card message was not edited", extra={"error": type(exc).__name__})


async def _present(query: CallbackQuery, result: CardActionResult) -> str | None:
    if isinstance(query.message, Message):
        await _update_card(query.message, result)
    return alert_text(result.outcome)


async def _answer(query: CallbackQuery, alert: str | None) -> None:
    try:
        await query.answer(text=alert, show_alert=alert is not None)
    except TelegramAPIError as exc:
        logger.debug("callback was not answered", extra={"error": type(exc).__name__})


async def _show_decision(query: CallbackQuery, text: str) -> None:
    if not isinstance(query.message, Message):
        return
    try:
        await query.message.edit_text(text=text, reply_markup=None)
    except TelegramAPIError as exc:
        logger.debug("pause notice was not edited", extra={"error": type(exc).__name__})


def _resume_alert(outcome: ResumeOutcome) -> str | None:
    if outcome in RESUMED_OUTCOMES:
        return None
    if outcome is ResumeOutcome.NOT_FOUND:
        return messages.ALERT_NOT_FOUND
    return messages.ALERT_RESUME_REFUSED


def _ignore_alert(outcome: IgnoreOutcome) -> str | None:
    if outcome in IGNORED_OUTCOMES:
        return None
    if outcome is IgnoreOutcome.NOT_FOUND:
        return messages.ALERT_NOT_FOUND
    return messages.ALERT_IGNORE_REFUSED


async def _apply_pause_action(
    pause_actions: PauseActions, query: CallbackQuery, data: PauseCallback
) -> tuple[str, str | None]:
    contact_id = data.contact_id
    match data.action:
        case PauseAction.RESUME:
            resumed = await pause_actions.resume(contact_id)
            if resumed in RESUMED_OUTCOMES:
                await _show_decision(query, messages.RESUMED_TEXT.format(contact_id=contact_id))
            return resumed.value, _resume_alert(resumed)
        case PauseAction.IGNORE:
            ignored = await pause_actions.ignore(contact_id)
            if ignored in IGNORED_OUTCOMES:
                await _show_decision(query, messages.IGNORED_TEXT.format(contact_id=contact_id))
            return ignored.value, _ignore_alert(ignored)
        case PauseAction.KEEP:
            await _show_decision(query, messages.KEPT_TEXT.format(contact_id=contact_id))
            return KEPT_OUTCOME, None


def build_router(actions: CardActions, pause_actions: PauseActions) -> Router:
    router = Router(name="control")

    @router.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
    async def on_start(message: Message) -> None:
        await message.answer(messages.START_TEXT)

    @router.callback_query(CardCallback.filter())
    async def on_card_button(query: CallbackQuery, callback_data: CardCallback) -> None:
        alert: str | None
        try:
            result = await _apply(actions, callback_data)
            alert = await _present(query, result)
        except Exception as exc:
            logger.warning("card button failed", extra={"error": type(exc).__name__})
            alert = messages.ALERT_FAILED
        else:
            logger.info(
                "card button handled",
                extra={
                    "contact_id": callback_data.contact_id,
                    "action": callback_data.action.value,
                    "outcome": result.outcome.value,
                },
            )
        await _answer(query, alert)

    @router.callback_query(PauseCallback.filter())
    async def on_pause_button(query: CallbackQuery, callback_data: PauseCallback) -> None:
        alert: str | None
        try:
            outcome, alert = await _apply_pause_action(pause_actions, query, callback_data)
        except Exception as exc:
            logger.warning("pause button failed", extra={"error": type(exc).__name__})
            alert = messages.ALERT_FAILED
        else:
            logger.info(
                "pause button handled",
                extra={
                    "contact_id": callback_data.contact_id,
                    "action": callback_data.action.value,
                    "outcome": outcome,
                },
            )
        await _answer(query, alert)

    return router
