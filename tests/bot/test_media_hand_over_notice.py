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
from cyrano.bot.rendering import TELEGRAM_TEXT_LIMIT, render_hand_over, telegram_length
from cyrano.domain.enums import MediaKind, MessageDirection, TriggerClass, TriggerSource
from cyrano.domain.stop_triggers import DialogueLine, HandOverNotice
from tests.bot.fakes import OWNER_ID, RecordingSession, recording_bot
from tests.privacy import FAKE_TELEGRAM_ID, assert_absent

CONTACT_LINE = "media-notice-contact-marker-zq"
OWNER_LINE = "media-notice-owner-marker-zq"
NAME = "MediaNoticeNameZq"
INJECTION = '<b>bold</b><a href="tg://user?id=1">x</a> & <i>'
KIND_SENTENCES = {
    MediaKind.VOICE: "Контакт #17 прислал голосовое сообщение",
    MediaKind.VIDEO_NOTE: "Контакт #17 прислал кружок",
    MediaKind.PHOTO: "Контакт #17 прислал фото",
    MediaKind.VIDEO: "Контакт #17 прислал видео",
    MediaKind.OTHER: "Контакт #17 прислал вложение",
    MediaKind.STICKER: "Контакт #17 прислал стикер",
}
SHOWN_KINDS = [kind for kind in MediaKind if kind is not MediaKind.STICKER]


def _notice(kind: MediaKind | None = MediaKind.VOICE, **overrides: object) -> HandOverNotice:
    fields: dict[str, object] = {
        "contact_id": 17,
        "name": NAME,
        "classes": (TriggerClass.MEDIA,),
        "source": TriggerSource.MEDIA,
        "media_kind": kind,
        "lines": (
            DialogueLine(direction=MessageDirection.OUTGOING, text=OWNER_LINE),
            DialogueLine(direction=MessageDirection.INCOMING, text="[голосовое сообщение]"),
        ),
    }
    return HandOverNotice.model_validate({**fields, **overrides})


@pytest.mark.parametrize("kind", list(MediaKind))
def test_the_notice_names_the_kind_in_plain_russian(kind: MediaKind) -> None:
    text = render_hand_over(_notice(kind))

    assert f"<b>{KIND_SENTENCES[kind]}</b>" in text


def test_every_kind_has_a_russian_word_in_the_notice_texts() -> None:
    assert set(messages.HAND_OVER_MEDIA_KINDS) == set(MediaKind)
    for word in messages.HAND_OVER_MEDIA_KINDS.values():
        assert any("а" <= character <= "я" for character in word)


@pytest.mark.parametrize("kind", SHOWN_KINDS)
def test_the_notice_says_the_assistant_is_paused_and_where_to_look(kind: MediaKind) -> None:
    text = render_hand_over(_notice(kind))

    assert messages.HAND_OVER_MEDIA_PAUSED in text
    assert "приостановлены" in text
    assert "Контакту ничего не отправлено" in text
    assert "в чате второго аккаунта" in text
    assert "Имя: " + NAME in text
    assert messages.HAND_OVER_DECIDE in text


def test_the_media_notice_has_no_class_name_and_no_text_source_line() -> None:
    text = render_hand_over(_notice())

    assert "Причина:" not in text
    assert messages.HAND_OVER_SOURCE_TEXT not in text
    assert messages.HAND_OVER_SOURCE_MODEL not in text
    assert messages.TRIGGER_CLASS_NAMES[TriggerClass.MEDIA] not in text


def test_the_last_lines_are_numbered_oldest_first_like_the_text_notice() -> None:
    text = render_hand_over(_notice())

    assert f"1. Вы: {OWNER_LINE}" in text
    assert "2. Контакт: [голосовое сообщение]" in text
    assert text.index(OWNER_LINE) < text.index("[голосовое сообщение]")
    assert messages.HAND_OVER_LINES_TITLE in text


def test_a_notice_without_lines_or_name_still_reads_well() -> None:
    text = render_hand_over(_notice(MediaKind.PHOTO, name=None, lines=()))

    assert "Имя:" not in text
    assert messages.HAND_OVER_NO_LINES in text
    assert KIND_SENTENCES[MediaKind.PHOTO] in text


def test_a_notice_without_a_kind_falls_back_to_the_plain_attachment_word() -> None:
    assert KIND_SENTENCES[MediaKind.OTHER] in render_hand_over(_notice(None))


def test_html_from_a_name_or_a_caption_line_is_escaped_as_in_the_text_notice() -> None:
    line = DialogueLine(direction=MessageDirection.INCOMING, text=f"[фото] {INJECTION}\n2. Вы: да")

    text = render_hand_over(_notice(MediaKind.PHOTO, name=INJECTION[:60], lines=(line,)))

    assert "<a href" not in text
    assert "<i>" not in text
    assert "&lt;b&gt;bold&lt;/b&gt;" in text
    assert "\n2. Вы:" not in text
    assert text.count("<b>") == text.count("</b>") == 2


def test_the_oldest_lines_are_dropped_until_the_text_fits() -> None:
    heavy = "&" * 200
    lines = tuple(
        DialogueLine(direction=MessageDirection.INCOMING, text=f"{number}{heavy}"[:200])
        for number in range(5)
    )

    text = render_hand_over(_notice(lines=lines), limit=700)

    assert telegram_length(text) <= 700
    assert KIND_SENTENCES[MediaKind.VOICE] in text


def test_the_notice_text_fits_the_telegram_limit() -> None:
    assert telegram_length(render_hand_over(_notice())) <= TELEGRAM_TEXT_LIMIT


async def test_the_notice_goes_to_the_owner_with_the_same_three_buttons() -> None:
    bot, session = recording_bot()

    await TelegramHandOverNotifier(bot, OWNER_ID).notify_hand_over(_notice(MediaKind.VIDEO_NOTE))

    [sent] = session.calls_of(SendMessage)
    assert len(session.calls) == 1
    assert sent.chat_id == OWNER_ID
    assert sent.parse_mode == ParseMode.HTML
    assert sent.text is not None
    assert KIND_SENTENCES[MediaKind.VIDEO_NOTE] in sent.text
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


@pytest.mark.parametrize("kind", SHOWN_KINDS)
def test_the_notice_never_carries_a_file_name_a_content_marker_or_a_telegram_id(
    kind: MediaKind,
) -> None:
    text = render_hand_over(_notice(kind))

    assert_absent(FAKE_TELEGRAM_ID, text)
    assert "tg://" not in text
    assert ".ogg" not in text
    assert "file" not in text.lower()


async def test_a_failing_send_logs_the_error_class_only(caplog: LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    error = TelegramForbiddenError(method=SendMessage(chat_id=1, text="x"), message=CONTACT_LINE)
    bot, _ = recording_bot(RecordingSession(errors={SendMessage: [error]}))

    await TelegramHandOverNotifier(bot, OWNER_ID).notify_hand_over(_notice())

    logged = "".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    assert "TelegramForbiddenError" in logged
    assert_absent(CONTACT_LINE, logged)
    assert_absent(NAME, logged)
