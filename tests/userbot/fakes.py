from __future__ import annotations

import asyncio
import itertools
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from telethon.errors import SessionPasswordNeededError

from cyrano.domain.enums import (
    ContactGender,
    ContactStatus,
    ManualMessageOutcome,
    MessageDirection,
    PauseReason,
)
from cyrano.domain.errors import ContactNotFound, ReplyAfterRefused
from cyrano.domain.models import (
    Contact,
    ContactCreate,
    ContactUpdate,
    IncomingMessage,
    Message,
    MessageCreate,
    OutgoingMessage,
    StatusChange,
)
from cyrano.domain.outbox import OutgoingPeak
from cyrano.domain.transitions import transition_needed
from cyrano.services.intake import IntakeResult
from cyrano.userbot.protocols import (
    EventCallback,
    FullUserResponse,
    HistoryMessage,
    IncomingEvent,
    SenderLike,
)
from tests.privacy import FAKE_TELEGRAM_ID

FAKE_SENT_AT = datetime(2025, 1, 2, 3, 4, 5, tzinfo=UTC)
FAKE_TEXT = "fake-incoming-text-that-must-not-leak"
FAKE_FIRST_NAME = "Fakefirst"
FAKE_LAST_NAME = "Fakelast"
FAKE_USERNAME = "fake_username_that_must_not_leak"
FAKE_SENDER_ID = FAKE_TELEGRAM_ID


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
class FakeFullUserInfo:
    about: str | None = None


@dataclass
class FakeFullUserResponse:
    full_user: FakeFullUserInfo = field(default_factory=FakeFullUserInfo)


@dataclass
class FakeHistoryMessage:
    id: int
    raw_text: str | None = ""
    out: bool | None = False
    entities: Sequence[object] | None = None
    photo: object | None = None
    grouped_id: int | None = None
    media_bytes: bytes | None = None


@dataclass
class FakeEvent:
    id: int = 77
    date: datetime | None = FAKE_SENT_AT
    out: bool = False
    is_private: bool | None = True
    is_group: bool | None = False
    is_channel: bool | None = False
    sender_id: int | None = FAKE_SENDER_ID
    chat_id: int | None = FAKE_SENDER_ID
    raw_text: str | None = FAKE_TEXT
    sender: SenderLike | None = field(default_factory=FakeSender)
    chat: SenderLike | None = field(default_factory=FakeSender)
    chat_error: Exception | None = None
    write_attempts: list[str] = field(default_factory=list)

    async def get_sender(self) -> SenderLike | None:
        return self.sender

    async def get_chat(self) -> SenderLike | None:
        if self.chat_error is not None:
            raise self.chat_error
        return self.chat

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


class FakeDetector:
    def __init__(
        self,
        outcome: ManualMessageOutcome = ManualMessageOutcome.STORED,
        error: BaseException | None = None,
        failures: int | None = None,
    ) -> None:
        self.received: list[OutgoingMessage] = []
        self.calls = 0
        self._outcome = outcome
        self._error = error
        self._failures = failures

    async def handle(self, outgoing: OutgoingMessage) -> ManualMessageOutcome:
        self.calls += 1
        if self._error is not None and (self._failures is None or self.calls <= self._failures):
            raise self._error
        self.received.append(outgoing)
        return self._outcome


class FakeClient:
    def __init__(
        self,
        *,
        authorized: bool = True,
        hang_on_connect: bool = False,
        hang_on_catch_up: bool = False,
        connect_error: OSError | None = None,
        calls: list[str] | None = None,
        hang_on_read: bool = False,
        read_error: Exception | None = None,
        hang_on_refresh: bool = False,
        refresh_error: Exception | None = None,
        refresh_dialogs: int = 0,
    ) -> None:
        self.calls = calls if calls is not None else []
        self.refreshes: list[int] = []
        self._hang_on_refresh = hang_on_refresh
        self._refresh_error = refresh_error
        self._refresh_dialogs = refresh_dialogs
        self.handlers: list[tuple[EventCallback, object]] = []
        self.write_attempts: list[str] = []
        self.users: dict[int, SenderLike] = {}
        self.chats: dict[str, SenderLike] = {}
        self.about: str | None = None
        self.photo_bytes: bytes | None = None
        self.history: list[HistoryMessage] = []
        self.full_user_requests: list[int] = []
        self.history_queries: list[tuple[int | None, str | None, int]] = []
        self.photo_downloads: list[tuple[str, bool]] = []
        self.media_downloads: list[tuple[int, str]] = []
        self._hang_on_read = hang_on_read
        self._read_error = read_error
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

    async def _read(self, name: str) -> None:
        self.calls.append(name)
        if self._hang_on_read:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                self.calls.append(f"{name} cancelled")
                raise
        if self._read_error is not None:
            raise self._read_error

    async def get_full_user(self, tg_id: int) -> FullUserResponse:
        await self._read("get_full_user")
        self.full_user_requests.append(tg_id)
        return FakeFullUserResponse(FakeFullUserInfo(self.about))

    async def refresh_entities(self, limit: int) -> int:
        self.calls.append("refresh_entities")
        self.refreshes.append(limit)
        if self._hang_on_refresh:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                self.calls.append("refresh_entities cancelled")
                raise
        if self._refresh_error is not None:
            raise self._refresh_error
        return self._refresh_dialogs

    async def get_entity(self, entity: int | str) -> SenderLike:
        await self._read("get_entity")
        found = self.users.get(entity) if isinstance(entity, int) else self.chats.get(entity)
        if found is None:
            raise ValueError("fake entity is not in the cache")
        return found

    async def download_profile_photo(
        self, entity: SenderLike, file: str, *, download_big: bool = True
    ) -> str | None:
        await self._read("download_profile_photo")
        self.photo_downloads.append((file, download_big))
        if self.photo_bytes is None:
            return None
        await asyncio.to_thread(Path(file).write_bytes, self.photo_bytes)
        return file

    async def download_media(self, message: HistoryMessage, file: str) -> str | None:
        await self._read("download_media")
        self.media_downloads.append((message.id, file))
        content = message.media_bytes if isinstance(message, FakeHistoryMessage) else None
        if content is None:
            return None
        await asyncio.to_thread(Path(file).write_bytes, content)
        return file

    async def iter_messages(
        self,
        entity: SenderLike,
        limit: int | None = None,
        *,
        search: str | None = None,
        offset_id: int = 0,
    ) -> AsyncIterator[HistoryMessage]:
        await self._read("iter_messages")
        self.history_queries.append((limit, search, offset_id))
        newest_first = sorted(self.history, key=lambda message: message.id, reverse=True)
        older = [m for m in newest_first if not offset_id or m.id < offset_id]
        wanted = [
            m for m in older if search is None or search.casefold() in (m.raw_text or "").casefold()
        ]
        for message in itertools.islice(wanted, limit):
            yield message

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
        self.status_changes: list[tuple[int, ContactStatus]] = []
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
            pause_reason=None,
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
        self,
        contact_id: int,
        status: ContactStatus,
        *,
        mode_id: int | None = None,
        pause_reason: PauseReason | None = None,
        reply_after: datetime | None = None,
    ) -> StatusChange:
        contact = await self.get_by_id(contact_id)
        if contact is None:
            raise ContactNotFound(contact_id)
        mode_on_activation = mode_id if status is ContactStatus.ACTIVE else None
        has_mode = mode_on_activation is not None or contact.mode_id is not None
        if not transition_needed(contact.status, status, has_mode=has_mode):
            return StatusChange.model_validate({**contact.model_dump(), "changed": False})
        if reply_after is not None and status is not ContactStatus.ACTIVE:
            raise ReplyAfterRefused(status)
        changes: dict[str, object] = {"status": status, "pause_reason": pause_reason}
        if mode_on_activation is not None:
            changes["mode_id"] = mode_on_activation
        if reply_after is not None:
            changes["reply_after"] = reply_after
        changed = contact.model_copy(update=changes)
        self.stored[contact.tg_id] = changed
        self.status_changes.append((contact_id, status))
        return StatusChange.model_validate({**changed.model_dump(), "changed": True})

    async def set_mode(self, contact_id: int, mode_id: int) -> Contact:
        raise NotImplementedError

    async def set_gender(self, contact_id: int, gender: ContactGender | None) -> Contact:
        contact = await self.get_by_id(contact_id)
        if contact is None:
            raise ContactNotFound(contact_id)
        changed = contact.model_copy(update={"gender": gender})
        self.stored[contact.tg_id] = changed
        return changed

    async def update(self, contact_id: int, data: ContactUpdate) -> Contact:
        contact = await self.get_by_id(contact_id)
        assert contact is not None
        changes: dict[str, object] = {}
        if data.profile is not None:
            changes["profile"] = data.profile
        if data.summary is not None:
            changes["summary"] = data.summary
        changed = contact.model_copy(update=changes)
        self.stored[contact.tg_id] = changed
        return changed

    async def update_identity(
        self, contact_id: int, username: str | None, display_name: str | None
    ) -> Contact:
        contact = await self.get_by_id(contact_id)
        assert contact is not None
        changed = contact.model_copy(update={"username": username, "display_name": display_name})
        self.stored[contact.tg_id] = changed
        return changed

    async def seed(
        self,
        tg_id: int,
        *,
        status: ContactStatus = ContactStatus.NEW,
        username: str | None = None,
        display_name: str | None = None,
        profile: dict[str, object] | None = None,
        created_at: datetime = FAKE_SENT_AT,
        mode_id: int | None = None,
        pause_reason: PauseReason | None = None,
        gender: ContactGender | None = None,
        summary: dict[str, object] | None = None,
    ) -> Contact:
        created = await self.create(
            ContactCreate(
                tg_id=tg_id, username=username, display_name=display_name, profile=profile or {}
            )
        )
        seeded = created.model_copy(
            update={
                "status": status,
                "created_at": created_at,
                "mode_id": mode_id,
                "pause_reason": pause_reason,
                "gender": gender,
                "summary": summary or {},
            }
        )
        self.stored[tg_id] = seeded
        return seeded

    async def touch_last_message_at(self, contact_id: int, at: datetime) -> Contact:
        contact = await self.get_by_id(contact_id)
        assert contact is not None
        touched = contact.model_copy(update={"last_message_at": at})
        self.stored[contact.tg_id] = touched
        return touched

    async def set_reply_after(self, contact_id: int, when: datetime) -> Contact:
        contact = await self.get_by_id(contact_id)
        assert contact is not None
        if contact.reply_after is not None and contact.reply_after >= when:
            return contact
        moved = contact.model_copy(update={"reply_after": when})
        self.stored[contact.tg_id] = moved
        return moved

    async def list_by_status(self, status: ContactStatus) -> list[Contact]:
        return [c for c in self.stored.values() if c.status is status]


class InMemoryMessages:
    def __init__(self) -> None:
        self.stored: list[Message] = []

    async def get(self, message_id: int) -> Message | None:
        return next((m for m in self.stored if m.id == message_id), None)

    async def exists(self, contact_id: int, tg_message_id: int) -> bool:
        return any(
            (m.contact_id, m.tg_message_id) == (contact_id, tg_message_id) for m in self.stored
        )

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
            created_at=data.created_at if data.created_at is not None else FAKE_SENT_AT,
        )
        self.stored.append(message)
        return message

    async def list_last(self, contact_id: int, limit: int) -> list[Message]:
        return [m for m in self.stored if m.contact_id == contact_id][-limit:]

    async def outgoing_times(
        self, since: datetime, limit: int, contact_id: int | None = None
    ) -> list[datetime]:
        times = [
            m.created_at
            for m in self.stored
            if m.direction is MessageDirection.OUTGOING
            and m.created_at >= since
            and (contact_id is None or m.contact_id == contact_id)
        ]
        return sorted(times, reverse=True)[:limit]

    async def busiest_outgoing_contact(self, after: datetime) -> OutgoingPeak | None:
        counts = Counter(
            m.contact_id
            for m in self.stored
            if m.direction is MessageDirection.OUTGOING and m.created_at > after
        )
        if not counts:
            return None
        contact_id, count = min(counts.items(), key=lambda item: (-item[1], item[0]))
        return OutgoingPeak(contact_id=contact_id, count=count)


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
