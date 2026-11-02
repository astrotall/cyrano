from __future__ import annotations

import logging
from pathlib import Path

import pytest
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.methods import SendMediaGroup, SendMessage, SendPhoto
from aiogram.types import InlineKeyboardMarkup

from cyrano.bot import messages
from cyrano.bot.sender import MAX_RETRY_AFTER_SECONDS, TelegramCardSender
from cyrano.domain.models import ContactCard
from tests.bot.fakes import OWNER_ID, RecordingSession, recording_bot
from tests.services.fakes import fake_card
from tests.userbot.fakes import RecordingSleep

PROFILE_PHOTO = "profiles/17.jpg"
CARD_PHOTO = "profiles/17-card.jpg"
PROBE = SendMessage(chat_id=OWNER_ID, text="probe")


def _forbidden() -> TelegramForbiddenError:
    return TelegramForbiddenError(PROBE, "Forbidden: bot can't initiate conversation with a user")


def _chat_not_found() -> TelegramBadRequest:
    return TelegramBadRequest(PROBE, "Bad Request: chat not found")


def _bad_photo() -> TelegramBadRequest:
    return TelegramBadRequest(PROBE, "Bad Request: PHOTO_INVALID_DIMENSIONS")


def _retry_after(seconds: int) -> TelegramRetryAfter:
    return TelegramRetryAfter(PROBE, "Too Many Requests", seconds)


def _media(tmp_path: Path, *names: str, content: bytes = b"fake-jpeg") -> Path:
    root = tmp_path / "media"
    for name in names:
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(content)
    return root


def _sender(
    tmp_path: Path, session: RecordingSession, sleep: RecordingSleep | None = None
) -> TelegramCardSender:
    bot, _ = recording_bot(session)
    return TelegramCardSender(
        bot, OWNER_ID, tmp_path / "media", sleep=sleep if sleep is not None else RecordingSleep()
    )


def _kinds(session: RecordingSession) -> list[str]:
    return [type(call).__name__ for call in session.calls]


async def test_a_card_without_photos_is_one_text_message_with_the_keyboard(
    tmp_path: Path,
) -> None:
    session = RecordingSession()
    sender = _sender(tmp_path, session)

    assert await sender.send_card(fake_card()) is True

    assert _kinds(session) == ["SendMessage"]
    [sent] = session.calls_of(SendMessage)
    assert sent.chat_id == OWNER_ID
    assert isinstance(sent.reply_markup, InlineKeyboardMarkup)
    assert "Fakefirst Fakelast" in sent.text
    assert sender.sent == 1


async def test_one_photo_is_sent_before_the_text_card(tmp_path: Path) -> None:
    session = RecordingSession()
    _media(tmp_path, PROFILE_PHOTO)
    sender = _sender(tmp_path, session)

    assert await sender.send_card(fake_card(profile_photo=PROFILE_PHOTO)) is True

    assert _kinds(session) == ["SendPhoto", "SendMessage"]
    [photo] = session.calls_of(SendPhoto)
    assert photo.caption == messages.PROFILE_PHOTO_CAPTION
    [text] = session.calls_of(SendMessage)
    assert text.reply_markup is not None


async def test_only_the_card_photo_gets_the_card_photo_caption(tmp_path: Path) -> None:
    session = RecordingSession()
    _media(tmp_path, CARD_PHOTO)
    sender = _sender(tmp_path, session)

    await sender.send_card(fake_card(card_photo=CARD_PHOTO))

    [photo] = session.calls_of(SendPhoto)
    assert photo.caption == messages.CARD_PHOTO_CAPTION


async def test_two_photos_go_as_a_media_group_before_the_text_card(tmp_path: Path) -> None:
    session = RecordingSession()
    _media(tmp_path, PROFILE_PHOTO, CARD_PHOTO)
    sender = _sender(tmp_path, session)

    assert await sender.send_card(fake_card(profile_photo=PROFILE_PHOTO, card_photo=CARD_PHOTO))

    assert _kinds(session) == ["SendMediaGroup", "SendMessage"]
    [group] = session.calls_of(SendMediaGroup)
    assert [item.caption for item in group.media] == [
        messages.PROFILE_PHOTO_CAPTION,
        messages.CARD_PHOTO_CAPTION,
    ]
    [text] = session.calls_of(SendMessage)
    assert isinstance(text.reply_markup, InlineKeyboardMarkup)


async def test_a_missing_photo_file_is_skipped_and_counted(tmp_path: Path) -> None:
    session = RecordingSession()
    _media(tmp_path, CARD_PHOTO)
    sender = _sender(tmp_path, session)

    assert await sender.send_card(fake_card(profile_photo=PROFILE_PHOTO, card_photo=CARD_PHOTO))

    assert _kinds(session) == ["SendPhoto", "SendMessage"]
    assert sender.photos_skipped == 1


async def test_all_photos_missing_still_sends_the_text_card(tmp_path: Path) -> None:
    session = RecordingSession()
    sender = _sender(tmp_path, session)

    assert await sender.send_card(fake_card(profile_photo=PROFILE_PHOTO, card_photo=CARD_PHOTO))

    assert _kinds(session) == ["SendMessage"]
    assert sender.photos_skipped == 2


async def test_an_empty_photo_file_is_skipped(tmp_path: Path) -> None:
    session = RecordingSession()
    _media(tmp_path, PROFILE_PHOTO, content=b"")
    sender = _sender(tmp_path, session)

    assert await sender.send_card(fake_card(profile_photo=PROFILE_PHOTO))

    assert _kinds(session) == ["SendMessage"]
    assert sender.photos_skipped == 1


async def test_an_unreadable_photo_file_is_skipped(tmp_path: Path) -> None:
    session = RecordingSession()
    root = _media(tmp_path, PROFILE_PHOTO)
    (root / PROFILE_PHOTO).chmod(0)
    sender = _sender(tmp_path, session)
    try:
        assert await sender.send_card(fake_card(profile_photo=PROFILE_PHOTO))
    finally:
        (root / PROFILE_PHOTO).chmod(0o600)

    assert _kinds(session) == ["SendMessage"]
    assert sender.photos_skipped == 1


async def test_a_photo_path_that_leaves_the_media_directory_is_skipped(tmp_path: Path) -> None:
    session = RecordingSession()
    _media(tmp_path, PROFILE_PHOTO)
    (tmp_path / "secret.jpg").write_bytes(b"outside")
    sender = _sender(tmp_path, session)

    assert await sender.send_card(fake_card(profile_photo="../secret.jpg"))

    assert _kinds(session) == ["SendMessage"]
    assert sender.photos_skipped == 1


async def test_a_photo_that_telegram_rejects_does_not_block_the_text_card(tmp_path: Path) -> None:
    session = RecordingSession({SendPhoto: [_bad_photo()]})
    _media(tmp_path, PROFILE_PHOTO)
    sender = _sender(tmp_path, session)

    assert await sender.send_card(fake_card(profile_photo=PROFILE_PHOTO)) is True

    assert _kinds(session) == ["SendPhoto", "SendMessage"]
    assert sender.photos_skipped == 1


async def test_chat_not_found_on_the_photo_step_fails_the_card(tmp_path: Path) -> None:
    session = RecordingSession({SendPhoto: [_chat_not_found()]})
    _media(tmp_path, PROFILE_PHOTO)
    sender = _sender(tmp_path, session)

    assert await sender.send_card(fake_card(profile_photo=PROFILE_PHOTO)) is False

    assert _kinds(session) == ["SendPhoto"]


async def test_retry_after_waits_the_requested_time_and_retries_once(tmp_path: Path) -> None:
    session = RecordingSession({SendMessage: [_retry_after(3)]})
    sleep = RecordingSleep()
    sender = _sender(tmp_path, session, sleep)

    assert await sender.send_card(fake_card()) is True

    assert sleep.delays == [3]
    assert _kinds(session) == ["SendMessage", "SendMessage"]


async def test_retry_after_on_a_photo_retries_the_photo(tmp_path: Path) -> None:
    session = RecordingSession({SendPhoto: [_retry_after(2)]})
    _media(tmp_path, PROFILE_PHOTO)
    sleep = RecordingSleep()
    sender = _sender(tmp_path, session, sleep)

    assert await sender.send_card(fake_card(profile_photo=PROFILE_PHOTO)) is True

    assert sleep.delays == [2]
    assert _kinds(session) == ["SendPhoto", "SendPhoto", "SendMessage"]


async def test_a_second_retry_after_gives_up(tmp_path: Path) -> None:
    session = RecordingSession({SendMessage: [_retry_after(1), _retry_after(1)]})
    sleep = RecordingSleep()
    sender = _sender(tmp_path, session, sleep)

    assert await sender.send_card(fake_card()) is False

    assert sleep.delays == [1]
    assert len(session.calls_of(SendMessage)) == 2
    assert sender.failed == 1


async def test_a_retry_after_beyond_the_bound_is_not_waited_for(tmp_path: Path) -> None:
    session = RecordingSession({SendMessage: [_retry_after(MAX_RETRY_AFTER_SECONDS + 1)]})
    sleep = RecordingSleep()
    sender = _sender(tmp_path, session, sleep)

    assert await sender.send_card(fake_card()) is False

    assert sleep.delays == []
    assert len(session.calls_of(SendMessage)) == 1


@pytest.mark.parametrize(
    "error",
    [
        _forbidden(),
        _chat_not_found(),
        TelegramNetworkError(PROBE, "fake network failure"),
        TelegramAPIError(PROBE, "fake api failure"),
    ],
    ids=["forbidden", "chat-not-found", "network", "other-api-error"],
)
async def test_telegram_errors_on_the_text_card_return_false(
    tmp_path: Path, error: TelegramAPIError
) -> None:
    session = RecordingSession({SendMessage: [error]})
    sender = _sender(tmp_path, session)

    assert await sender.send_card(fake_card()) is False

    assert sender.sent == 0
    assert sender.failed == 1


async def test_a_forbidden_photo_fails_the_card_before_any_text(tmp_path: Path) -> None:
    session = RecordingSession({SendPhoto: [_forbidden()]})
    _media(tmp_path, PROFILE_PHOTO)
    sender = _sender(tmp_path, session)

    assert await sender.send_card(fake_card(profile_photo=PROFILE_PHOTO)) is False

    assert _kinds(session) == ["SendPhoto"]


async def test_every_call_targets_the_owner_chat(tmp_path: Path) -> None:
    session = RecordingSession({SendMessage: [_retry_after(1)]})
    _media(tmp_path, PROFILE_PHOTO, CARD_PHOTO)
    sender = _sender(tmp_path, session)

    await sender.send_card(fake_card(profile_photo=PROFILE_PHOTO, card_photo=CARD_PHOTO))
    await sender.send_card(fake_card(contact_id=99))

    assert session.calls
    assert set(session.chat_ids()) == {OWNER_ID}


async def test_a_failure_is_logged_without_personal_data(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    session = RecordingSession({SendMessage: [_forbidden()]})
    sender = _sender(tmp_path, session)
    card: ContactCard = fake_card(
        display_name="Secretname", username="secret_nick", bio="secretbio"
    )

    with caplog.at_level(logging.INFO):
        await sender.send_card(card)

    logged = "".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    assert "TelegramForbiddenError" in logged
    for private in ("Secretname", "secret_nick", "secretbio", str(OWNER_ID)):
        assert private not in logged
