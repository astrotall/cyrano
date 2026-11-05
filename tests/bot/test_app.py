from __future__ import annotations

import asyncio

import pytest
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiogram.methods import GetMe, GetUpdates

from cyrano.bot.app import ControlBot
from cyrano.bot.errors import BotTokenRejected, PollingFailed
from cyrano.config import load_settings
from cyrano.services.card_actions import CardActions
from cyrano.services.pause_actions import PauseActions
from tests.bot.fakes import FAKE_BOT_TOKEN, OWNER_ID, RecordingSession
from tests.services.fakes import InMemoryModes
from tests.userbot.fakes import InMemoryContacts

WAIT_ATTEMPTS = 200
WAIT_INTERVAL_SECONDS = 0.01
OWNER_ENV = {"OWNER_TELEGRAM_ID": str(OWNER_ID), "BOT_TOKEN": FAKE_BOT_TOKEN}


def _control_bot(
    monkeypatch: pytest.MonkeyPatch,
    full_env: dict[str, str],
    session: RecordingSession | None = None,
    token: str = FAKE_BOT_TOKEN,
) -> ControlBot:
    for name, value in {**OWNER_ENV, "BOT_TOKEN": token}.items():
        monkeypatch.setenv(name, value)
    settings = load_settings(env_file=None)
    contacts = InMemoryContacts()
    return ControlBot(
        settings, CardActions(contacts, InMemoryModes()), PauseActions(contacts), session=session
    )


def test_a_malformed_token_is_a_readable_error_without_the_token(
    monkeypatch: pytest.MonkeyPatch, full_env: dict[str, str]
) -> None:
    with pytest.raises(BotTokenRejected) as raised:
        _control_bot(monkeypatch, full_env, token="not a token")

    assert "BOT_TOKEN" in str(raised.value)
    assert "not a token" not in str(raised.value)
    assert raised.value.__cause__ is None


async def test_a_token_that_telegram_rejects_is_a_readable_error(
    monkeypatch: pytest.MonkeyPatch, full_env: dict[str, str]
) -> None:
    rejected = TelegramUnauthorizedError(GetMe(), "Unauthorized")
    session = RecordingSession({GetMe: [rejected]})
    control_bot = _control_bot(monkeypatch, full_env, session)

    with pytest.raises(BotTokenRejected) as raised:
        await control_bot.poll()

    assert FAKE_BOT_TOKEN not in str(raised.value)
    assert session.calls_of(GetUpdates) == []


async def test_any_other_polling_failure_is_a_readable_error_with_the_class_name_only(
    monkeypatch: pytest.MonkeyPatch, full_env: dict[str, str]
) -> None:
    session = RecordingSession({GetMe: [TelegramNetworkError(GetMe(), "secret detail")]})
    control_bot = _control_bot(monkeypatch, full_env, session)

    with pytest.raises(PollingFailed) as raised:
        await control_bot.poll()

    assert "TelegramNetworkError" in str(raised.value)
    assert "secret detail" not in str(raised.value)


async def test_polling_runs_until_stopped_then_closes_the_session(
    monkeypatch: pytest.MonkeyPatch, full_env: dict[str, str]
) -> None:
    session = RecordingSession()
    control_bot = _control_bot(monkeypatch, full_env, session)
    polling = asyncio.create_task(control_bot.poll())
    for _ in range(WAIT_ATTEMPTS):
        if session.calls_of(GetUpdates):
            break
        await asyncio.sleep(WAIT_INTERVAL_SECONDS)
    else:
        pytest.fail("polling never asked for updates")

    await control_bot.stop()
    await asyncio.wait_for(polling, timeout=2)

    assert session.closed is True
    assert not [c for c in session.calls if not isinstance(c, GetMe | GetUpdates)]


async def test_stopping_a_bot_that_never_polled_only_closes_the_session(
    monkeypatch: pytest.MonkeyPatch, full_env: dict[str, str]
) -> None:
    session = RecordingSession()
    control_bot = _control_bot(monkeypatch, full_env, session)

    await control_bot.stop()

    assert session.closed is True
    assert session.calls == []


def test_the_card_sender_belongs_to_the_control_bot(
    monkeypatch: pytest.MonkeyPatch, full_env: dict[str, str]
) -> None:
    control_bot = _control_bot(monkeypatch, full_env, RecordingSession())

    assert control_bot.sender.sent == 0
