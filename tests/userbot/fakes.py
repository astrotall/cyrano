from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from telethon.errors import SessionPasswordNeededError

from cyrano.domain.enums import ContactStatus
from cyrano.domain.models import (
    Contact,
    ContactCreate,
    ContactUpdate,
    IncomingMessage,
    Message,
    MessageCreate,
)
from cyrano.services.intake import IntakeResult
from cyrano.userbot.protocols import EventCallback, IncomingEvent, SenderLike

FAKE_SENT_AT = datetime(2025, 1, 2, 3, 4, 5, tzinfo=UTC)
FAKE_TEXT = "fake-incoming-text-that-must-not-leak"
FAKE_FIRST_NAME = "Fakefirst"
FAKE_LAST_NAME = "Fakelast"
FAKE_USERNAME = "fake_username_that_must_not_leak"
FAKE_SENDER_ID = 5001


@dataclass
class FakeSender:
    id: int = FAKE_SENDER_ID
    username: str | None = FAKE_USERNAME
    first_name: str | None = FAKE_FIRST_NAME
    last_name: str | None = FAKE_LAST_NAME
    bot: bool | None = False
    deleted: bool | None = False
    is_self: bool | None = False


@dataclass
class FakeEvent:
    id: int = 77
    date: datetime = FAKE_SENT_AT
    out: bool = False
    is_private: bool | None = True
    is_group: bool | None = False
    is_channel: bool | None = False
    sender_id: int | None = FAKE_SENDER_ID
    raw_text: str | None = FAKE_TEXT
    sender: SenderLike | None = field(default_factory=FakeSender)
    write_attempts: list[str] = field(default_factory=list)

    async def get_sender(self) -> SenderLike | None:
        return self.sender

    async def reply(self, *args: object, **kwargs: object) -> None:
        self.write_attempts.append("reply")
        raise AssertionError

    async def respond(self, *args: object, **kwargs: object) -> None:
        self.write_attempts.append("respond")
        raise AssertionError

    async def mark_read(self, *args: object, **kwargs: object) -> None:
        self.write_attempts.append("mark_read")
        raise AssertionError


class FakeIntake:
    def __init__(self, error: BaseException | None = None, failures: int | None = None) -> None:
        self.received: list[IncomingMessage] = []
        self.calls = 0
        self._error = error
        self._failures = failures

    async def handle(self, incoming: IncomingMessage) -> IntakeResult:
        self.calls += 1
        if self._error is not None and (self._failures is None or self.calls <= self._failures):
            raise self._error
        self.received.append(incoming)
        return IntakeResult(contact_id=1, message_id=1, contact_created=True)


class FakeClient:
    def __init__(
        self,
        *,
        authorized: bool = True,
        hang_on_connect: bool = False,
        hang_on_catch_up: bool = False,
        connect_error: OSError | None = None,
        calls: list[str] | None = None,
    ) -> None:
        self.calls = calls if calls is not None else []
        self.handlers: list[tuple[EventCallback, object]] = []
        self.write_attempts: list[str] = []
        self._authorized = authorized
        self._hang_on_connect = hang_on_connect
        self._hang_on_catch_up = hang_on_catch_up
        self._connect_error = connect_error
        self._disconnected: asyncio.Future[object] | None = None

    @property
    def disconnected(self) -> asyncio.Future[object]:
        if self._disconnected is None:
            self._disconnected = asyncio.get_running_loop().create_future()
        return self._disconnected

    def lose_connection(self) -> None:
        self.disconnected.set_result(None)

    async def connect(self) -> None:
        self.calls.append("connect")
        if self._hang_on_connect:
            await asyncio.sleep(3600)
        if self._connect_error is not None:
            raise self._connect_error

    def disconnect(self) -> Awaitable[None]:
        return self._disconnect()

    async def _disconnect(self) -> None:
        self.calls.append("disconnect")
        if not self.disconnected.done():
            self.disconnected.set_result(None)

    async def is_user_authorized(self) -> bool:
        self.calls.append("is_user_authorized")
        return self._authorized

    async def catch_up(self) -> None:
        self.calls.append("catch_up")
        if self._hang_on_catch_up:
            await asyncio.sleep(3600)

    def add_event_handler(self, callback: EventCallback, event: object) -> None:
        self.calls.append("add_event_handler")
        self.handlers.append((callback, event))

    def remove_event_handler(self, callback: EventCallback) -> int:
        self.calls.append("remove_event_handler")
        before = len(self.handlers)
        self.handlers = [(cb, ev) for cb, ev in self.handlers if cb != callback]
        return before - len(self.handlers)

    async def deliver(self, event: IncomingEvent) -> None:
        for callback, _ in list(self.handlers):
            await callback(event)

    async def send_message(self, *args: object, **kwargs: object) -> None:
        self.write_attempts.append("send_message")
        raise AssertionError

    async def send_read_acknowledge(self, *args: object, **kwargs: object) -> None:
        self.write_attempts.append("send_read_acknowledge")
        raise AssertionError

    async def delete_messages(self, *args: object, **kwargs: object) -> None:
        self.write_attempts.append("delete_messages")
        raise AssertionError


class InMemoryContacts:
    def __init__(self, lookup_failures: int = 0) -> None:
        self.stored: dict[int, Contact] = {}
        self._lookup_failures = lookup_failures

    async def get_by_id(self, contact_id: int) -> Contact | None:
        return next((c for c in self.stored.values() if c.id == contact_id), None)

    async def get_by_tg_id(self, tg_id: int) -> Contact | None:
        if self._lookup_failures > 0:
            self._lookup_failures -= 1
            raise ConnectionError("fake transient failure")
        return self.stored.get(tg_id)

    async def create(self, data: ContactCreate) -> Contact:
        contact = Contact(
            id=len(self.stored) + 1,
            tg_id=data.tg_id,
            username=data.username,
            display_name=data.display_name,
            status=ContactStatus.NEW,
            mode_id=None,
            profile=data.profile,
            summary={},
            last_message_at=None,
            created_at=FAKE_SENT_AT,
            updated_at=FAKE_SENT_AT,
        )
        self.stored[data.tg_id] = contact
        return contact

    async def set_status(
        self, contact_id: int, status: ContactStatus, *, mode_id: int | None = None
    ) -> Contact:
        raise NotImplementedError

    async def set_mode(self, contact_id: int, mode_id: int) -> Contact:
        raise NotImplementedError

    async def update(self, contact_id: int, data: ContactUpdate) -> Contact:
        raise NotImplementedError

    async def touch_last_message_at(self, contact_id: int, at: datetime) -> Contact:
        contact = await self.get_by_id(contact_id)
        assert contact is not None
        touched = contact.model_copy(update={"last_message_at": at})
        self.stored[contact.tg_id] = touched
        return touched

    async def list_by_status(self, status: ContactStatus) -> list[Contact]:
        return [c for c in self.stored.values() if c.status is status]


class InMemoryMessages:
    def __init__(self) -> None:
        self.stored: list[Message] = []

    async def append(self, data: MessageCreate) -> Message:
        for existing in self.stored:
            if (existing.contact_id, existing.tg_message_id) == (
                data.contact_id,
                data.tg_message_id,
            ):
                return existing
        message = Message(
            id=len(self.stored) + 1,
            contact_id=data.contact_id,
            direction=data.direction,
            text=data.text,
            tg_message_id=data.tg_message_id,
            created_at=FAKE_SENT_AT,
        )
        self.stored.append(message)
        return message

    async def list_last(self, contact_id: int, limit: int) -> list[Message]:
        return [m for m in self.stored if m.contact_id == contact_id][-limit:]


class RecordingSleep:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


class FakeLoginClient:
    def __init__(
        self,
        *,
        authorized: bool = False,
        password_required: bool = False,
        code_error: Exception | None = None,
        connect_error: OSError | None = None,
    ) -> None:
        self.calls: list[str] = []
        self.sign_in_arguments: list[tuple[str | None, str | None, str | None]] = []
        self._authorized = authorized
        self._password_required = password_required
        self._code_error = code_error
        self._connect_error = connect_error

    async def connect(self) -> None:
        self.calls.append("connect")
        if self._connect_error is not None:
            raise self._connect_error

    def disconnect(self) -> Awaitable[None]:
        return self._disconnect()

    async def _disconnect(self) -> None:
        self.calls.append("disconnect")

    async def is_user_authorized(self) -> bool:
        return self._authorized

    async def send_code_request(self, phone: str) -> object:
        self.calls.append("send_code_request")
        return None

    async def sign_in(
        self, phone: str | None = None, code: str | None = None, *, password: str | None = None
    ) -> object:
        self.calls.append("sign_in")
        self.sign_in_arguments.append((phone, code, password))
        if password is None and self._code_error is not None:
            raise self._code_error
        if password is None and self._password_required:
            raise SessionPasswordNeededError(request=None)
        return None


def touch_session_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
