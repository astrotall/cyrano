from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable
from datetime import UTC, datetime
from typing import cast

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.methods import (
    AnswerCallbackQuery,
    EditMessageReplyMarkup,
    EditMessageText,
    GetMe,
    GetUpdates,
    SendMediaGroup,
    SendMessage,
    SendPhoto,
    TelegramMethod,
)
from aiogram.methods.base import TelegramType
from aiogram.types import Chat, Message, PhotoSize, User

from cyrano.config import Settings
from cyrano.services.card_actions import CardActions
from cyrano.services.pause_actions import PauseActions
from tests.privacy import FAKE_OWNER_TELEGRAM_ID, FAKE_STRANGER_TELEGRAM_ID
from tests.services.fakes import FakeCardSender
from tests.services.outbox_fakes import (
    RecordingHandOverNotifier,
    RecordingManualPauseNotifier,
    RecordingNotifier,
)

OWNER_ID = FAKE_OWNER_TELEGRAM_ID
STRANGER_ID = FAKE_STRANGER_TELEGRAM_ID
FAKE_BOT_TOKEN = "123456:fake-token-for-tests-only-abcdefghijklmnop"
FAKE_BOT_ID = 123456
FAKE_TIME = datetime(2025, 1, 2, 3, 4, 5, tzinfo=UTC)
PRIVATE_CHAT = "private"
GROUP_CHAT = "group"
POLL_PAUSE_SECONDS = 0.01


class RecordingSession(BaseSession):
    def __init__(self, errors: dict[type, list[Exception]] | None = None) -> None:
        super().__init__()
        self.calls: list[TelegramMethod[object]] = []
        self.closed = False
        self._errors = errors if errors is not None else {}
        self._next_message_id = 100

    def calls_of[M](self, kind: type[M]) -> list[M]:
        return [call for call in self.calls if isinstance(call, kind)]

    def chat_ids(self) -> list[int | str | None]:
        return [getattr(call, "chat_id", None) for call in self.calls]

    def _message(self, chat_id: int | str, text: str | None = None) -> Message:
        self._next_message_id += 1
        photo = (
            None
            if text is not None
            else [PhotoSize(file_id="f", file_unique_id="u", width=1, height=1)]
        )
        return Message(
            message_id=self._next_message_id,
            date=FAKE_TIME,
            chat=Chat(id=int(chat_id), type=PRIVATE_CHAT),
            text=text,
            photo=photo,
        )

    def _result(self, method: TelegramMethod[object]) -> object:
        match method:
            case GetMe():
                return User(id=FAKE_BOT_ID, is_bot=True, first_name="Fake", username="fake_bot")
            case SendMessage():
                return self._message(method.chat_id, method.text)
            case SendPhoto():
                return self._message(method.chat_id)
            case SendMediaGroup():
                return [self._message(method.chat_id) for _ in method.media]
            case AnswerCallbackQuery() | EditMessageText() | EditMessageReplyMarkup():
                return True
            case _:
                return []

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[TelegramType],
        timeout: int | None = None,  # noqa: ASYNC109
    ) -> TelegramType:
        self.calls.append(cast(TelegramMethod[object], method))
        if isinstance(method, GetUpdates):
            await asyncio.sleep(POLL_PAUSE_SECONDS)
        queued = self._errors.get(type(method))
        if queued:
            raise queued.pop(0)
        return cast(TelegramType, self._result(cast(TelegramMethod[object], method)))

    async def stream_content(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        timeout: int = 30,  # noqa: ASYNC109
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes]:
        yield b""

    async def close(self) -> None:
        self.closed = True


def recording_bot(session: RecordingSession | None = None) -> tuple[Bot, RecordingSession]:
    session = session if session is not None else RecordingSession()
    bot = Bot(
        token=FAKE_BOT_TOKEN,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )
    return bot, session


def _user(user_id: int) -> dict[str, object]:
    return {"id": user_id, "is_bot": False, "first_name": "Fakefirst"}


def message_update(
    user_id: int,
    text: str,
    *,
    update_id: int = 1,
    chat_id: int | None = None,
    chat_type: str = PRIVATE_CHAT,
) -> dict[str, object]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": 7,
            "date": int(FAKE_TIME.timestamp()),
            "chat": {"id": chat_id if chat_id is not None else user_id, "type": chat_type},
            "from": _user(user_id),
            "text": text,
            "entities": [{"type": "bot_command", "offset": 0, "length": len(text)}]
            if text.startswith("/")
            else [],
        },
    }


def callback_update(
    user_id: int,
    data: str,
    *,
    update_id: int = 2,
    card_text: str | None = "Новый контакт",
    chat_id: int | None = None,
    inaccessible: bool = False,
) -> dict[str, object]:
    chat = {"id": chat_id if chat_id is not None else user_id, "type": PRIVATE_CHAT}
    message: dict[str, object] = {
        "message_id": 41,
        "date": int(FAKE_TIME.timestamp()),
        "chat": chat,
    }
    if inaccessible:
        message["date"] = 0
    elif card_text is not None:
        message["text"] = card_text
    return {
        "update_id": update_id,
        "callback_query": {
            "id": "callback-1",
            "from": _user(user_id),
            "chat_instance": "instance-1",
            "data": data,
            "message": message,
        },
    }


class FakeControlBot:
    def __init__(
        self,
        calls: list[str],
        *,
        poll_error: Exception | None = None,
        poll_returns: bool = False,
        sender: FakeCardSender | None = None,
    ) -> None:
        self.calls = calls
        self.sender = sender if sender is not None else FakeCardSender()
        self.notifier = RecordingNotifier()
        self.manual_pause_notifier = RecordingManualPauseNotifier()
        self.hand_over_notifier = RecordingHandOverNotifier()
        self._poll_error = poll_error
        self._poll_returns = poll_returns

    async def poll(self) -> None:
        self.calls.append("bot.poll")
        if self._poll_error is not None:
            raise self._poll_error
        if self._poll_returns:
            return
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            self.calls.append("bot.poll cancelled")
            raise

    async def stop(self) -> None:
        self.calls.append("bot.stop")


def fake_control_bot_class(
    calls: list[str] | None = None,
    *,
    poll_error: Exception | None = None,
    poll_returns: bool = False,
    sender: FakeCardSender | None = None,
) -> Callable[[Settings, CardActions, PauseActions], FakeControlBot]:
    recorded = calls if calls is not None else []

    def build(
        settings: Settings, actions: CardActions, pause_actions: PauseActions
    ) -> FakeControlBot:
        return FakeControlBot(
            recorded, poll_error=poll_error, poll_returns=poll_returns, sender=sender
        )

    return build
