"""Pure functions that turn a card and a decision into Telegram text and buttons."""

from __future__ import annotations

from html import escape

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from cyrano.bot import messages
from cyrano.bot.callbacks import CardAction, CardCallback
from cyrano.domain.enums import CardOutcome, ContactStatus, LeomatchNameSource
from cyrano.domain.models import CardActionResult, CardLeomatch, ContactCard

TELEGRAM_TEXT_LIMIT = 4096
CARD_TEXT_BUDGET = 3500
SECTION_SEPARATOR = "\n\n"
LINE_SEPARATOR = "\n"
MESSAGE_BULLET = "• "
FIELD_SEPARATOR = ", "


def _escaped(value: str) -> str:
    return escape(value, quote=False)


def _titled(title: str, lines: list[str]) -> str:
    return LINE_SEPARATOR.join([f"<b>{title}</b>", *lines])


def _identity_section(card: ContactCard) -> str:
    lines: list[str] = []
    if card.display_name:
        lines.append(f"{messages.LABEL_NAME}: {_escaped(card.display_name)}")
    if card.username:
        lines.append(f"{messages.LABEL_USERNAME}: @{_escaped(card.username)}")
    return _titled(messages.CARD_TITLE, lines)


def _messages_section(card: ContactCard) -> str | None:
    if not card.messages:
        return None
    return _titled(
        messages.LABEL_MESSAGES, [f"{MESSAGE_BULLET}{_escaped(t)}" for t in card.messages]
    )


def _bio_section(card: ContactCard) -> str | None:
    if not card.bio:
        return None
    return _titled(messages.LABEL_BIO, [_escaped(card.bio)])


def _leomatch_name_line(leomatch: CardLeomatch) -> list[str]:
    if leomatch.name is None:
        return []
    return [f"{messages.LABEL_NAME}: {_escaped(leomatch.name)}"]


def _leomatch_section(leomatch: CardLeomatch | None) -> str | None:
    if leomatch is None:
        return None
    if leomatch.raw_text is None:
        return _titled(
            messages.LABEL_ANKETA, [messages.CARD_NOT_FOUND, *_leomatch_name_line(leomatch)]
        )
    if leomatch.name is None or leomatch.name_source is not LeomatchNameSource.CARD:
        return _titled(
            messages.LABEL_ANKETA_RAW,
            [*_leomatch_name_line(leomatch), _escaped(leomatch.raw_text)],
        )
    head = [leomatch.name, *(str(v) for v in (leomatch.age, leomatch.city) if v is not None)]
    lines = [_escaped(FIELD_SEPARATOR.join(head))]
    if leomatch.about:
        lines.append(_escaped(leomatch.about))
    return _titled(messages.LABEL_ANKETA, lines)


def render_card(card: ContactCard) -> str:
    text = _identity_section(card)
    optional = [
        None if card.profile_collected else messages.PROFILE_NOT_COLLECTED,
        _messages_section(card),
        _bio_section(card),
        _leomatch_section(card.leomatch),
        None if card.modes else messages.NO_MODES_NOTICE,
    ]
    for section in optional:
        if (
            section is not None
            and len(text) + len(SECTION_SEPARATOR) + len(section) <= CARD_TEXT_BUDGET
        ):
            text += SECTION_SEPARATOR + section
    return text


def card_keyboard(card: ContactCard) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for mode in card.modes:
        callback = CardCallback(
            action=CardAction.REPLY, contact_id=card.contact_id, mode_id=mode.id
        )
        builder.row(
            InlineKeyboardButton(
                text=messages.REPLY_BUTTON.format(mode=mode.name), callback_data=callback.pack()
            )
        )
    ignore = CardCallback(action=CardAction.IGNORE, contact_id=card.contact_id)
    builder.row(InlineKeyboardButton(text=messages.IGNORE_BUTTON, callback_data=ignore.pack()))
    return builder.as_markup()


def decision_text(result: CardActionResult) -> str | None:
    if result.status is ContactStatus.ACTIVE:
        if result.mode_name is None:
            return messages.DECISION_REPLY_NO_MODE
        return messages.DECISION_REPLY.format(mode=_escaped(result.mode_name))
    if result.status is ContactStatus.IGNORED:
        return messages.DECISION_IGNORE
    return None


def append_decision(card_html: str, result: CardActionResult) -> str:
    decision = decision_text(result)
    head, separator, _ = card_html.rpartition(messages.DECISION_MARKER)
    base = head if separator else card_html
    if decision is None:
        return base
    return f"{base}{messages.DECISION_MARKER} {decision}"


def alert_text(outcome: CardOutcome) -> str | None:
    match outcome:
        case CardOutcome.REFUSED:
            return messages.ALERT_REFUSED
        case CardOutcome.NOT_FOUND:
            return messages.ALERT_NOT_FOUND
        case CardOutcome.MODE_UNAVAILABLE:
            return messages.ALERT_MODE_UNAVAILABLE
        case CardOutcome.APPLIED | CardOutcome.ALREADY_APPLIED:
            return None
