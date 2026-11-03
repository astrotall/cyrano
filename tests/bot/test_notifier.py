from __future__ import annotations

import logging
import re
import string

import pytest
from aiogram.exceptions import TelegramForbiddenError, TelegramNetworkError
from aiogram.methods import SendMessage
from pytest import LogCaptureFixture

from cyrano.bot import messages
from cyrano.bot.notifier import OUTBOX_NOTICES, TelegramOwnerNotifier
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
