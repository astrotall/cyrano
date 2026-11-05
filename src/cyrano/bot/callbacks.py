"""Callback data of the card buttons."""

from __future__ import annotations

from enum import StrEnum

from aiogram.filters.callback_data import CallbackData

CARD_CALLBACK_PREFIX = "card"


class CardAction(StrEnum):
    REPLY = "reply"
    IGNORE = "ignore"


class CardCallback(CallbackData, prefix=CARD_CALLBACK_PREFIX):
    action: CardAction
    contact_id: int
    mode_id: int | None = None


PAUSE_CALLBACK_PREFIX = "pause"


class PauseAction(StrEnum):
    RESUME = "resume"


class PauseCallback(CallbackData, prefix=PAUSE_CALLBACK_PREFIX):
    action: PauseAction
    contact_id: int
