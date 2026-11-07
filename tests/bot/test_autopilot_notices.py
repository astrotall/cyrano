from __future__ import annotations

import string

import pytest
from aiogram.methods import SendMessage

from cyrano.bot import messages
from cyrano.bot.app import NoticeBot
from cyrano.bot.errors import BotTokenRejected
from cyrano.bot.notifier import AUTOPILOT_NOTICES
from cyrano.config import load_settings
from cyrano.domain.autopilot import PROVIDER_COOLDOWN_SECONDS, SECONDS_PER_MINUTE
from cyrano.domain.enums import AutopilotPauseReason, AutopilotStopReason, TriggerSource
from cyrano.domain.stop_triggers import HandOverNotice
from tests.bot.fakes import RecordingSession
from tests.privacy import FAKE_OWNER_TELEGRAM_ID


def _fields(template: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(template) if name}


def test_the_notice_templates_take_only_reasons_minutes_and_class_names() -> None:
    assert _fields(AUTOPILOT_NOTICES.stopped) == {"reason", "error"}
    assert _fields(AUTOPILOT_NOTICES.paused) == {"minutes", "reason", "error"}


def test_every_reason_has_plain_words() -> None:
    assert set(AUTOPILOT_NOTICES.stop_reasons) == set(AutopilotStopReason)
    assert set(AUTOPILOT_NOTICES.pause_reasons) == set(AutopilotPauseReason)


@pytest.mark.parametrize("reason", list(AutopilotStopReason))
def test_the_stop_notice_names_the_reason_the_class_and_the_resume_command(
    reason: AutopilotStopReason,
) -> None:
    text = AUTOPILOT_NOTICES.stopped.format(
        reason=AUTOPILOT_NOTICES.stop_reasons[reason], error="LLMAuthError"
    )

    assert AUTOPILOT_NOTICES.stop_reasons[reason] in text
    assert "LLMAuthError" in text
    assert "python -m cyrano autopilot resume --yes" in text


def test_the_pause_notice_names_the_minutes_and_the_resume_command() -> None:
    text = AUTOPILOT_NOTICES.paused.format(
        minutes=PROVIDER_COOLDOWN_SECONDS // SECONDS_PER_MINUTE,
        reason=AUTOPILOT_NOTICES.pause_reasons[AutopilotPauseReason.PROVIDER_UNAVAILABLE],
        error="LLMUnavailableError",
    )

    assert "15 мин" in text
    assert "python -m cyrano autopilot resume" in text
    assert "--yes" not in text


def test_the_hand_over_texts_fit_a_model_signal_without_classes() -> None:
    assert "тема" not in messages.HAND_OVER_NO_CLASSES
    assert "модел" in messages.HAND_OVER_SOURCE_MODEL


async def test_the_notice_bot_sends_both_notices_to_the_owner_only_and_closes(
    full_env: dict[str, str],
) -> None:
    session = RecordingSession()
    bot = NoticeBot(load_settings(env_file=None), session=session)

    await bot.notifier.notify("invented notice")
    await bot.hand_over_notifier.notify_hand_over(
        HandOverNotice(contact_id=3, source=TriggerSource.MODEL)
    )
    await bot.close()

    calls = session.calls_of(SendMessage)
    assert len(session.calls) == len(calls) == 2
    assert {call.chat_id for call in calls} == {FAKE_OWNER_TELEGRAM_ID}
    assert session.closed


def test_the_notice_bot_has_no_polling_and_no_way_to_another_chat() -> None:
    public = {name for name in vars(NoticeBot) if not name.startswith("_")}

    assert public == {"notifier", "hand_over_notifier", "close"}


def test_a_malformed_token_is_the_readable_error(
    full_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BOT_TOKEN", "not-a-token")

    with pytest.raises(BotTokenRejected):
        NoticeBot(load_settings(env_file=None))
