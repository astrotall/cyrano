from __future__ import annotations

import logging

import pytest
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import SendMessage
from aiogram.types import InlineKeyboardMarkup
from pytest import LogCaptureFixture

from cyrano.bot import messages
from cyrano.bot.callbacks import PauseAction, PauseCallback
from cyrano.bot.notifier import TelegramHandOverNotifier
from cyrano.bot.rendering import (
    TELEGRAM_TEXT_LIMIT,
    hand_over_keyboard,
    render_hand_over,
    telegram_length,
)
from cyrano.domain.enums import MessageDirection, TriggerClass, TriggerSource
from cyrano.domain.stop_triggers import HAND_OVER_LINE_MAX_CHARS, DialogueLine, HandOverNotice
from cyrano.services.hand_over_notifier import HandOverNotifier
from tests.bot.fakes import OWNER_ID, RecordingSession, recording_bot

CONTACT_LINE = "notice-contact-marker-zq давай встретимся"
OWNER_LINE = "notice-owner-marker-zq"
INJECTION = '<b>bold</b><a href="tg://user?id=1">x</a> & <i>'


def _notice(**overrides: object) -> HandOverNotice:
    fields: dict[str, object] = {
        "contact_id": 17,
        "name": "Fakename",
        "classes": (TriggerClass.MEETING, TriggerClass.MONEY),
        "source": TriggerSource.TEXT,
        "lines": (
            DialogueLine(direction=MessageDirection.OUTGOING, text=OWNER_LINE),
            DialogueLine(direction=MessageDirection.INCOMING, text=CONTACT_LINE),
        ),
    }
    return HandOverNotice.model_validate({**fields, **overrides})


def test_the_notifier_satisfies_the_service_interface() -> None:
    bot, _ = recording_bot()
    notifier: HandOverNotifier = TelegramHandOverNotifier(bot, OWNER_ID)

    assert notifier is not None


def test_nothing_in_the_hand_over_notifier_can_address_another_chat() -> None:
    public = {name for name in vars(TelegramHandOverNotifier) if not name.startswith("_")}

    assert public == {"notify_hand_over"}


async def test_the_notice_is_one_html_message_to_the_owner_with_three_buttons() -> None:
    bot, session = recording_bot()

    await TelegramHandOverNotifier(bot, OWNER_ID).notify_hand_over(_notice())

    [sent] = session.calls_of(SendMessage)
    assert len(session.calls) == 1
    assert sent.chat_id == OWNER_ID
    assert sent.parse_mode == ParseMode.HTML
    assert sent.text == render_hand_over(_notice())
    keyboard = sent.reply_markup
    assert isinstance(keyboard, InlineKeyboardMarkup)
    rows = keyboard.inline_keyboard
    assert [[button.text for button in row] for row in rows] == [
        ["Возобновить автоответы"],
        ["Оставить на паузе"],
        ["Игнорировать"],
    ]
    assert [row[0].callback_data for row in rows] == [
        PauseCallback(action=action, contact_id=17).pack()
        for action in (PauseAction.RESUME, PauseAction.KEEP, PauseAction.IGNORE)
    ]


def test_the_notice_names_the_classes_the_source_and_numbers_the_lines_oldest_first() -> None:
    text = render_hand_over(_notice())

    assert "контакт #17" in text
    assert "Имя: Fakename" in text
    assert "предложение встретиться, деньги" in text
    assert messages.HAND_OVER_SOURCE_TEXT in text
    assert f"1. Вы: {OWNER_LINE}" in text
    assert f"2. Контакт: {CONTACT_LINE}" in text
    assert text.index(OWNER_LINE) < text.index(CONTACT_LINE)
    assert "Контакту ничего не отправлено" in text


def test_every_class_has_a_plain_russian_name() -> None:
    assert set(messages.TRIGGER_CLASS_NAMES) == set(TriggerClass)
    for name in messages.TRIGGER_CLASS_NAMES.values():
        assert any("а" <= character <= "я" for character in name)


def test_the_model_signal_notice_says_so_and_names_no_class() -> None:
    text = render_hand_over(_notice(classes=(), source=TriggerSource.MODEL, lines=()))

    assert messages.HAND_OVER_SOURCE_MODEL in text
    assert messages.HAND_OVER_NO_CLASSES in text
    assert messages.HAND_OVER_NO_LINES in text


def test_a_media_only_line_and_a_missing_name_are_shown_plainly() -> None:
    line = DialogueLine(direction=MessageDirection.INCOMING, text="")
    text = render_hand_over(_notice(name=None, lines=(line,)))

    assert "Имя:" not in text
    assert f"1. Контакт: {messages.HAND_OVER_EMPTY_TEXT}" in text


def test_html_from_a_name_or_a_text_is_escaped() -> None:
    line = DialogueLine(direction=MessageDirection.INCOMING, text=INJECTION)
    text = render_hand_over(_notice(name=INJECTION[:60], lines=(line,)))

    assert "<b>bold</b>" not in text
    assert "<a href" not in text
    assert "<i>" not in text
    assert "&lt;b&gt;bold&lt;/b&gt;" in text
    assert "&amp;" in text
    assert text.count("<b>") == text.count("</b>") == 2


def test_a_line_break_inside_a_text_cannot_fake_another_numbered_line() -> None:
    line = DialogueLine(direction=MessageDirection.INCOMING, text="привет\n2. Вы: да, встретимся")
    text = render_hand_over(_notice(lines=(line,)))

    assert "\n2. Вы:" not in text
    assert "1. Контакт: привет 2. Вы: да, встретимся" in text


def test_the_oldest_lines_are_dropped_until_the_text_fits_the_limit() -> None:
    heavy = "&" * HAND_OVER_LINE_MAX_CHARS
    lines = tuple(
        DialogueLine(direction=MessageDirection.INCOMING, text=f"{number}{heavy}"[:200])
        for number in range(5)
    )
    full = render_hand_over(_notice(lines=lines), limit=10**6)
    assert telegram_length(full) > TELEGRAM_TEXT_LIMIT

    text = render_hand_over(_notice(lines=lines))

    assert telegram_length(text) <= TELEGRAM_TEXT_LIMIT
    assert "4&amp;" in text
    assert "0&amp;" not in text
    rendered = text.splitlines()
    heading = rendered.index(f"<b>{messages.HAND_OVER_LINES_TITLE}</b>")
    kept = [line for line in rendered[heading + 1 :] if line[:1].isdigit()]
    assert 0 < len(kept) < len(lines)
    assert [line.split(".")[0] for line in kept] == [str(n) for n in range(1, len(kept) + 1)]
    assert kept[-1].startswith(f"{len(kept)}. Контакт: 4")


def test_a_small_limit_keeps_the_head_even_with_no_line_left() -> None:
    text = render_hand_over(_notice(), limit=10)

    assert "контакт #17" in text
    assert OWNER_LINE not in text
    assert CONTACT_LINE not in text


def test_the_keyboard_has_the_three_actions_in_order() -> None:
    rows = hand_over_keyboard(23).inline_keyboard

    assert [PauseCallback.unpack(row[0].callback_data or "").action for row in rows] == [
        PauseAction.RESUME,
        PauseAction.KEEP,
        PauseAction.IGNORE,
    ]


def test_the_callback_data_of_every_action_is_short_and_has_the_pause_prefix() -> None:
    for action in PauseAction:
        packed = PauseCallback(action=action, contact_id=10**12).pack()
        assert packed.startswith("pause:")
        assert len(packed.encode()) <= 64


async def test_a_failing_notice_is_logged_with_the_class_only_and_never_raises(
    caplog: LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    error = TelegramForbiddenError(method=SendMessage(chat_id=1, text="x"), message="fake detail")
    bot, _ = recording_bot(RecordingSession(errors={SendMessage: [error]}))

    await TelegramHandOverNotifier(bot, OWNER_ID).notify_hand_over(_notice())

    logged = "".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    assert "TelegramForbiddenError" in logged
    assert "fake detail" not in logged
    assert CONTACT_LINE not in logged
    assert OWNER_LINE not in logged


@pytest.mark.parametrize("source", list(TriggerSource))
def test_every_source_has_a_line(source: TriggerSource) -> None:
    assert render_hand_over(_notice(source=source))
