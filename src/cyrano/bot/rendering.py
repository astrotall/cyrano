from __future__ import annotations

import re
from html import escape

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from cyrano.bot import messages
from cyrano.bot.callbacks import (
    CardAction,
    CardCallback,
    GenderAction,
    GenderCallback,
    PauseAction,
    PauseCallback,
)
from cyrano.domain.enums import (
    CardOutcome,
    ContactGender,
    ContactStatus,
    LeomatchNameSource,
    MediaKind,
    MessageDirection,
    TriggerSource,
)
from cyrano.domain.gender import ResolvedGender
from cyrano.domain.models import CardActionResult, CardLeomatch, ContactCard
from cyrano.domain.outbox import UTF16_BYTES_PER_UNIT, UTF16_CODEC, UTF16_ERROR_POLICY
from cyrano.domain.stop_triggers import DialogueLine, HandOverNotice

TELEGRAM_TEXT_LIMIT = 4096
CARD_TEXT_BUDGET = 3500
SECTION_SEPARATOR = "\n\n"
LINE_SEPARATOR = "\n"
MESSAGE_BULLET = "• "
FIELD_SEPARATOR = ", "
WHITESPACE_RUN = re.compile(r"\s+")
WORD_SEPARATOR = " "
DECIDABLE_STATUSES = frozenset({ContactStatus.NEW, ContactStatus.PENDING})
GENDER_BUTTONS = (
    (GenderAction.FEMALE, ContactGender.FEMALE, messages.GENDER_FEMALE_BUTTON),
    (GenderAction.MALE, ContactGender.MALE, messages.GENDER_MALE_BUTTON),
)
HAND_OVER_SPEAKERS = {
    MessageDirection.INCOMING: messages.HAND_OVER_SPEAKER_CONTACT,
    MessageDirection.OUTGOING: messages.HAND_OVER_SPEAKER_OWNER,
}
HAND_OVER_SOURCES = {
    TriggerSource.TEXT: messages.HAND_OVER_SOURCE_TEXT,
    TriggerSource.MODEL: messages.HAND_OVER_SOURCE_MODEL,
}
UNKNOWN_MEDIA_KIND = MediaKind.OTHER


def _escaped(value: str) -> str:
    return escape(value, quote=False)


def _titled(title: str, lines: list[str]) -> str:
    return LINE_SEPARATOR.join([f"<b>{title}</b>", *lines])


def gender_line(gender: ResolvedGender) -> str:
    if gender.value is None:
        return messages.GENDER_UNKNOWN_LINE
    return messages.GENDER_LINE.format(
        label=messages.LABEL_GENDER,
        value=messages.GENDER_VALUE_NAMES[gender.value],
        source=messages.GENDER_SOURCE_NAMES[gender.source],
    )


def _identity_section(card: ContactCard) -> str:
    lines: list[str] = []
    if card.display_name:
        lines.append(f"{messages.LABEL_NAME}: {_escaped(card.display_name)}")
    if card.username:
        lines.append(f"{messages.LABEL_USERNAME}: @{_escaped(card.username)}")
    lines.append(gender_line(card.gender))
    if card.status not in DECIDABLE_STATUSES:
        lines.append(
            messages.STATUS_LINE.format(
                label=messages.LABEL_STATUS, status=messages.STATUS_NAMES[card.status]
            )
        )
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


def _gender_buttons(card: ContactCard) -> list[InlineKeyboardButton]:
    buttons: list[InlineKeyboardButton] = []
    for action, gender, label in GENDER_BUTTONS:
        text = f"{messages.GENDER_CHECK_MARK}{label}" if card.gender.value is gender else label
        callback = GenderCallback(action=action, contact_id=card.contact_id)
        buttons.append(InlineKeyboardButton(text=text, callback_data=callback.pack()))
    return buttons


def card_keyboard(card: ContactCard) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(*_gender_buttons(card))
    if card.status not in DECIDABLE_STATUSES:
        return builder.as_markup()
    reply_modes = card.modes if card.gender.is_known else []
    for mode in reply_modes:
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
        case CardOutcome.GENDER_REQUIRED:
            return messages.ALERT_GENDER_REQUIRED
        case CardOutcome.APPLIED | CardOutcome.ALREADY_APPLIED:
            return None


def telegram_length(text: str) -> int:
    return len(text.encode(UTF16_CODEC, UTF16_ERROR_POLICY)) // UTF16_BYTES_PER_UNIT


def _one_line(text: str) -> str:
    return WHITESPACE_RUN.sub(WORD_SEPARATOR, text).strip()


def _media_hand_over_head(notice: HandOverNotice) -> str:
    kind = notice.media_kind or UNKNOWN_MEDIA_KIND
    title = messages.HAND_OVER_MEDIA_TITLE.format(
        contact_id=notice.contact_id, what=messages.HAND_OVER_MEDIA_KINDS[kind]
    )
    lines = [f"<b>{title}</b>"]
    if notice.name:
        lines.append(messages.HAND_OVER_NAME.format(name=_escaped(_one_line(notice.name))))
    lines.append(messages.HAND_OVER_MEDIA_PAUSED)
    return LINE_SEPARATOR.join(lines)


def _hand_over_head(notice: HandOverNotice) -> str:
    if notice.source is TriggerSource.MEDIA:
        return _media_hand_over_head(notice)
    title = messages.HAND_OVER_TITLE.format(contact_id=notice.contact_id)
    lines = [f"<b>{title}</b>"]
    if notice.name:
        lines.append(messages.HAND_OVER_NAME.format(name=_escaped(_one_line(notice.name))))
    names = [messages.TRIGGER_CLASS_NAMES[trigger] for trigger in notice.classes]
    reason = messages.TRIGGER_CLASS_SEPARATOR.join(names) or messages.HAND_OVER_NO_CLASSES
    lines.extend(
        [
            messages.HAND_OVER_PAUSED,
            messages.HAND_OVER_REASON.format(classes=reason),
            HAND_OVER_SOURCES[notice.source],
        ]
    )
    return LINE_SEPARATOR.join(lines)


def _dialogue_line(number: int, line: DialogueLine) -> str:
    text = _one_line(line.text)
    return messages.HAND_OVER_LINE.format(
        number=number,
        speaker=HAND_OVER_SPEAKERS[line.direction],
        text=_escaped(text) if text else messages.HAND_OVER_EMPTY_TEXT,
    )


def _dialogue_section(lines: tuple[DialogueLine, ...]) -> str:
    if not lines:
        return messages.HAND_OVER_NO_LINES
    rendered = [_dialogue_line(number, line) for number, line in enumerate(lines, start=1)]
    return _titled(messages.HAND_OVER_LINES_TITLE, rendered)


def render_hand_over(notice: HandOverNotice, limit: int = TELEGRAM_TEXT_LIMIT) -> str:
    lines = notice.lines
    while True:
        text = SECTION_SEPARATOR.join(
            [_hand_over_head(notice), _dialogue_section(lines), messages.HAND_OVER_DECIDE]
        )
        if telegram_length(text) <= limit or not lines:
            return text
        lines = lines[1:]


def hand_over_keyboard(contact_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for action, label in (
        (PauseAction.RESUME, messages.RESUME_BUTTON),
        (PauseAction.KEEP, messages.KEEP_BUTTON),
        (PauseAction.IGNORE, messages.HAND_OVER_IGNORE_BUTTON),
    ):
        callback = PauseCallback(action=action, contact_id=contact_id)
        builder.row(InlineKeyboardButton(text=label, callback_data=callback.pack()))
    return builder.as_markup()
