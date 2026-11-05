from __future__ import annotations

import logging
import re
import string

import pytest
from aiogram.exceptions import TelegramForbiddenError, TelegramNetworkError
from aiogram.methods import SendMessage
from aiogram.types import InlineKeyboardMarkup
from pytest import LogCaptureFixture

from cyrano.bot import messages
from cyrano.bot.callbacks import PauseAction, PauseCallback
from cyrano.bot.notifier import OUTBOX_NOTICES, TelegramManualPauseNotifier, TelegramOwnerNotifier
from cyrano.domain.enums import LimitScope, LimitWindow
from cyrano.domain.outbox import ROW_STALE_AFTER_SECONDS, STALE_NOTICE_CONTACT_IDS_MAX
from cyrano.services.manual_pause_notifier import ManualPauseNotifier
from cyrano.services.owner_notifier import OwnerNotifier
from tests.bot.fakes import OWNER_ID, RecordingSession, recording_bot
from tests.privacy import FAKE_OTHER_INTERNAL_ID, FAKE_TELEGRAM_ID, assert_absent

NOTICE = "fake <notice> with #17"


def test_the_notifier_satisfies_the_service_interface() -> None:
    bot, _ = recording_bot()
    notifier: OwnerNotifier = TelegramOwnerNotifier(bot, OWNER_ID)

    assert notifier is not None


async def test_a_notice_is_one_plain_message_to_the_owner_chat() -> None:
    bot, session = recording_bot()

    await TelegramOwnerNotifier(bot, OWNER_ID).notify(NOTICE)

    calls = session.calls_of(SendMessage)
    assert len(session.calls) == len(calls) == 1
    assert calls[0].chat_id == OWNER_ID
    assert calls[0].text == NOTICE
    assert calls[0].parse_mode is None


def test_nothing_in_the_notifier_can_address_another_chat() -> None:
    public = {name for name in vars(TelegramOwnerNotifier) if not name.startswith("_")}

    assert public == {"notify"}


@pytest.mark.parametrize(
    "error",
    [
        TelegramForbiddenError(method=SendMessage(chat_id=1, text="x"), message="fake detail"),
        TelegramNetworkError(method=SendMessage(chat_id=1, text="x"), message="fake detail"),
    ],
)
async def test_a_failing_notice_is_logged_with_the_class_only_and_never_raises(
    error: Exception, caplog: LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    bot, _ = recording_bot(RecordingSession(errors={SendMessage: [error]}))

    await TelegramOwnerNotifier(bot, OWNER_ID).notify(NOTICE)

    assert "fake detail" not in caplog.text
    assert NOTICE not in caplog.text
    assert any(getattr(r, "error", None) == type(error).__name__ for r in caplog.records)


def test_the_texts_are_russian_and_take_only_ids_and_class_names() -> None:
    rendered = "\n".join(
        [
            OUTBOX_NOTICES.interrupted.format(
                row_id=FAKE_OTHER_INTERNAL_ID, contact_id=2, error="interrupted"
            ),
            OUTBOX_NOTICES.failed.format(row_id=1, contact_id=2, error="FakeError"),
            OUTBOX_NOTICES.uncertain.format(row_id=FAKE_OTHER_INTERNAL_ID, contact_id=2),
            OUTBOX_NOTICES.flood_wait.format(row_id=1, contact_id=2, seconds=90),
        ]
    )

    assert str(FAKE_OTHER_INTERNAL_ID) in rendered
    assert_absent(FAKE_TELEGRAM_ID, rendered)
    assert OUTBOX_NOTICES.interrupted == messages.OUTBOX_INTERRUPTED
    assert OUTBOX_NOTICES.failed == messages.OUTBOX_FAILED
    assert OUTBOX_NOTICES.uncertain == messages.OUTBOX_UNCERTAIN
    assert OUTBOX_NOTICES.flood_wait == messages.OUTBOX_FLOOD_WAIT
    for text in (
        messages.OUTBOX_INTERRUPTED,
        messages.OUTBOX_FAILED,
        messages.OUTBOX_UNCERTAIN,
        messages.OUTBOX_FLOOD_WAIT,
    ):
        assert any("а" <= character <= "я" for character in text)


def test_the_interrupted_notice_says_what_to_do_and_takes_only_the_ids_and_the_class() -> None:
    rendered = OUTBOX_NOTICES.interrupted.format(row_id=17, contact_id=23, error="interrupted")

    assert set(re.findall(r"\d+", rendered)) == {"17", "23"}
    assert "Повторной отправки не будет" in rendered
    assert "Проверьте чат" in rendered
    assert "python -m cyrano outbox retry 17 --yes" in rendered
    assert {field for _, field, _, _ in string.Formatter().parse(messages.OUTBOX_INTERRUPTED)} - {
        None
    } == {"row_id", "contact_id", "error"}


def test_the_uncertain_notice_says_what_to_do_and_takes_only_the_two_ids() -> None:
    rendered = OUTBOX_NOTICES.uncertain.format(row_id=17, contact_id=23)

    assert set(re.findall(r"\d+", rendered)) == {"17", "23"}
    assert "неизвестен" in rendered
    assert "Проверьте чат" in rendered
    assert "Автоматического повтора не будет" in rendered
    assert "python -m cyrano outbox retry 17 --yes" in rendered
    assert {field for _, field, _, _ in string.Formatter().parse(messages.OUTBOX_UNCERTAIN)} - {
        None
    } == {"row_id", "contact_id"}


def _fields(template: str) -> set[str]:
    return {field for _, field, _, _ in string.Formatter().parse(template) if field is not None}


def test_the_flood_notice_says_the_whole_sender_waits_and_takes_ids_and_seconds_only() -> None:
    rendered = OUTBOX_NOTICES.flood_wait.format(row_id=17, contact_id=23, seconds=90)

    assert set(re.findall(r"\d+", rendered)) == {"17", "23", "90"}
    assert "вся отправка приостановлена" in rendered
    assert _fields(messages.OUTBOX_FLOOD_WAIT) == {"row_id", "contact_id", "seconds"}


def test_the_stopped_notice_names_the_kind_and_the_resume_command() -> None:
    rendered = OUTBOX_NOTICES.account_stopped.format(kind="FakeKind")

    assert "FakeKind" in rendered
    assert "Проверьте" in rendered or "проверьте" in rendered
    assert "python -m cyrano outbox resume --yes" in rendered
    assert _fields(messages.OUTBOX_ACCOUNT_STOPPED) == {"kind"}


def test_the_contact_paused_notice_takes_the_contact_id_and_the_class_name_only() -> None:
    rendered = OUTBOX_NOTICES.contact_paused.format(contact_id=23, error="FakeError")

    assert set(re.findall(r"\d+", rendered)) == {"23"}
    assert "FakeError" in rendered
    assert _fields(messages.OUTBOX_CONTACT_PAUSED) == {"contact_id", "error"}


def test_the_limit_note_is_truthful_about_postponing_and_the_stale_cancel() -> None:
    note = messages.OUTBOX_LIMIT_NOTE

    assert "отложены до освобождения слота" in note
    assert f"{ROW_STALE_AFTER_SECONDS // 3600} часов" in note
    assert "отменяется как устаревшее" in note
    assert "не потеряны" not in note
    assert "уйдут сами" not in note
    for template in OUTBOX_NOTICES.limit_reached.values():
        assert note in template


def test_the_stale_summary_notice_takes_the_count_and_the_contact_ids_only() -> None:
    rendered = OUTBOX_NOTICES.stale_summary.format(count=3, contact_ids="#23, #24")

    assert _fields(messages.OUTBOX_STALE_SUMMARY) == {"count", "contact_ids"}
    assert "3" in rendered
    assert "#23, #24" in rendered
    assert "устаревших" in rendered
    assert f"{ROW_STALE_AFTER_SECONDS // 3600} часов" in rendered
    assert f"до {STALE_NOTICE_CONTACT_IDS_MAX}" in rendered
    assert any("а" <= character <= "я" for character in rendered)
    assert OUTBOX_NOTICES.stale_summary == messages.OUTBOX_STALE_SUMMARY


def test_the_stop_reminder_names_the_kind_and_the_resume_command_and_nothing_else() -> None:
    rendered = OUTBOX_NOTICES.stop_reminder.format(kind="FakeKind")

    assert _fields(messages.OUTBOX_STOP_REMINDER) == {"kind"}
    assert "FakeKind" in rendered
    assert "python -m cyrano outbox resume --yes" in rendered
    assert "по-прежнему остановлена" in rendered
    assert OUTBOX_NOTICES.stop_reminder == messages.OUTBOX_STOP_REMINDER


def test_a_limit_notice_exists_for_every_hourly_and_daily_limit_and_for_nothing_else() -> None:
    assert set(OUTBOX_NOTICES.limit_reached) == {
        (scope, window) for scope in LimitScope for window in (LimitWindow.HOUR, LimitWindow.DAY)
    }


@pytest.mark.parametrize(
    ("scope", "window", "expected"),
    [
        (LimitScope.GLOBAL, LimitWindow.HOUR, "общий лимит отправки за час"),
        (LimitScope.GLOBAL, LimitWindow.DAY, "общий лимит отправки за сутки"),
        (LimitScope.CONTACT, LimitWindow.HOUR, "за час для контакта #23"),
        (LimitScope.CONTACT, LimitWindow.DAY, "за сутки для контакта #23"),
    ],
)
def test_a_limit_notice_says_which_limit_and_keeps_only_the_contact_id(
    scope: LimitScope, window: LimitWindow, expected: str
) -> None:
    template = OUTBOX_NOTICES.limit_reached[(scope, window)]
    rendered = template.format(contact_id=23)

    assert expected in rendered
    assert "python -m cyrano outbox status" in rendered
    assert _fields(template) <= {"contact_id"}
    assert any("а" <= character <= "я" for character in rendered)


def test_the_manual_pause_notifier_satisfies_the_service_interface() -> None:
    bot, _ = recording_bot()
    notifier: ManualPauseNotifier = TelegramManualPauseNotifier(bot, OWNER_ID)

    assert notifier is not None


async def test_a_manual_pause_notice_is_one_plain_message_to_the_owner_with_one_button() -> None:
    bot, session = recording_bot()

    await TelegramManualPauseNotifier(bot, OWNER_ID).notify_manual_pause(17)

    [sent] = session.calls_of(SendMessage)
    assert len(session.calls) == 1
    assert sent.chat_id == OWNER_ID
    assert sent.text == messages.MANUAL_PAUSE_NOTICE.format(contact_id=17)
    assert sent.parse_mode is None
    keyboard = sent.reply_markup
    assert isinstance(keyboard, InlineKeyboardMarkup)
    [[button]] = keyboard.inline_keyboard
    assert button.text == messages.RESUME_BUTTON
    assert button.callback_data == PauseCallback(action=PauseAction.RESUME, contact_id=17).pack()


async def test_the_manual_pause_notice_carries_the_internal_id_and_no_other_number() -> None:
    bot, session = recording_bot()

    await TelegramManualPauseNotifier(bot, OWNER_ID).notify_manual_pause(17)

    [sent] = session.calls_of(SendMessage)
    assert sent.text is not None
    assert set(re.findall(r"\d+", sent.text)) == {"17"}


def test_nothing_in_the_manual_pause_notifier_can_address_another_chat() -> None:
    public = {name for name in vars(TelegramManualPauseNotifier) if not name.startswith("_")}

    assert public == {"notify_manual_pause"}


async def test_a_failing_manual_pause_notice_is_logged_with_the_class_only_and_never_raises(
    caplog: LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    error = TelegramForbiddenError(method=SendMessage(chat_id=1, text="x"), message="fake detail")
    bot, _ = recording_bot(RecordingSession(errors={SendMessage: [error]}))

    await TelegramManualPauseNotifier(bot, OWNER_ID).notify_manual_pause(17)

    logged = "".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    assert "TelegramForbiddenError" in logged
    assert "fake detail" not in logged


def test_the_manual_pause_notice_names_the_button_and_has_no_personal_placeholder() -> None:
    fields = {
        name for _, name, _, _ in string.Formatter().parse(messages.MANUAL_PAUSE_NOTICE) if name
    }

    assert fields == {"contact_id"}
    assert "кнопк" in messages.MANUAL_PAUSE_NOTICE
