from __future__ import annotations

import asyncio
import logging
import typing
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone

import pytest
from telethon.errors import (
    AuthKeyUnregisteredError,
    ChatWriteForbiddenError,
    FloodPremiumWaitError,
    FloodTestPhoneWaitError,
    FloodWaitError,
    ForbiddenError,
    InputUserDeactivatedError,
    MessageTooLongError,
    PeerFloodError,
    PeerIdInvalidError,
    PeerIdNotSupportedError,
    RPCError,
    ServerError,
    SlowModeWaitError,
    UserBlockedError,
    UserDeactivatedBanError,
    UserDeactivatedError,
    UserIdInvalidError,
    UserIsBlockedError,
    UserPrivacyRestrictedError,
    YouBlockedUserError,
)
from telethon.tl.functions.users import GetFullUserRequest

from cyrano.domain.errors import SendFloodWait, SendRejected, SendUncertain
from cyrano.services.message_transport import MessageTransport
from cyrano.userbot.adapter import MISSING_RESPONSE, PEER_NOT_FOUND, TelethonAdapter
from cyrano.userbot.protocols import UserbotClient
from tests.privacy import FAKE_TELEGRAM_ID, assert_absent
from tests.userbot.fakes import FakeHistoryMessage, FakeSender

TEXT = "adapter-text-marker-zq"
SENT_AT = datetime(2025, 5, 6, 7, 8, 9, tzinfo=UTC)
TG_MESSAGE_ID = 700777
FAKE_PEER = object()
FAKE_FULL_USER = object()


@dataclass
class _Sent:
    id: int = TG_MESSAGE_ID
    date: datetime = SENT_AT


class _RawTelethon:
    def __init__(
        self,
        *,
        send_error: Exception | None = None,
        peer_error: Exception | None = None,
        sent: _Sent | None = None,
        returns_nothing: bool = False,
    ) -> None:
        self.send_calls: list[tuple[object, str, dict[str, object]]] = []
        self.peer_lookups: list[int] = []
        self.requests: list[object] = []
        self.disconnected: asyncio.Future[object] | None = None
        self.calls: list[str] = []
        self._send_error = send_error
        self._peer_error = peer_error
        self._sent = sent if sent is not None else _Sent()
        self._returns_nothing = returns_nothing

    async def get_input_entity(self, peer: int) -> object:
        self.peer_lookups.append(peer)
        if self._peer_error is not None:
            raise self._peer_error
        return FAKE_PEER

    async def send_message(self, peer: object, text: str, **kwargs: object) -> _Sent | None:
        self.send_calls.append((peer, text, kwargs))
        if self._send_error is not None:
            raise self._send_error
        return None if self._returns_nothing else self._sent

    async def __call__(self, request: object) -> object:
        self.requests.append(request)
        return FAKE_FULL_USER

    async def connect(self) -> None:
        self.calls.append("connect")

    async def disconnect(self) -> None:
        self.calls.append("disconnect")

    async def is_user_authorized(self) -> bool:
        self.calls.append("is_user_authorized")
        return True

    async def catch_up(self) -> None:
        self.calls.append("catch_up")

    def add_event_handler(self, callback: object, event: object) -> None:
        self.calls.append("add_event_handler")

    def remove_event_handler(self, callback: object) -> int:
        self.calls.append("remove_event_handler")
        return 1

    async def get_entity(self, entity: int | str) -> FakeSender:
        self.calls.append("get_entity")
        return FakeSender()

    async def download_profile_photo(
        self, entity: object, file: str, download_big: bool = True
    ) -> str:
        self.calls.append(f"download_profile_photo {download_big}")
        return file

    async def download_media(self, message: object, file: str) -> str:
        self.calls.append("download_media")
        return file

    async def iter_messages(
        self, entity: object, limit: int | None, *, search: str | None, offset_id: int
    ) -> AsyncIterator[FakeHistoryMessage]:
        self.calls.append(f"iter_messages {limit} {search} {offset_id}")
        yield FakeHistoryMessage(id=1)


def test_the_adapter_satisfies_both_interfaces() -> None:
    adapter = TelethonAdapter(_RawTelethon())
    transport: MessageTransport = adapter
    client: UserbotClient = adapter

    assert transport is not None
    assert client is not None


def test_the_public_surface_is_the_read_protocol_plus_exactly_one_send() -> None:
    public = {name for name in vars(TelethonAdapter) if not name.startswith("_")}

    assert public == set(typing.get_protocol_members(UserbotClient)) | {"send_text"}


async def test_a_text_goes_out_plain_without_a_link_preview() -> None:
    raw = _RawTelethon()

    sent = await TelethonAdapter(raw).send_text(FAKE_TELEGRAM_ID, TEXT)

    assert raw.peer_lookups == [FAKE_TELEGRAM_ID]
    assert raw.send_calls == [(FAKE_PEER, TEXT, {"parse_mode": None, "link_preview": False})]
    assert (sent.tg_message_id, sent.sent_at) == (TG_MESSAGE_ID, SENT_AT)


async def test_the_sent_time_is_normalized_to_utc() -> None:
    moscow = timezone(timedelta(hours=3))
    raw = _RawTelethon(sent=_Sent(date=SENT_AT.astimezone(moscow)))

    sent = await TelethonAdapter(raw).send_text(FAKE_TELEGRAM_ID, TEXT)

    assert sent.sent_at == SENT_AT
    assert sent.sent_at.tzinfo is UTC


@pytest.mark.parametrize(
    "error",
    [
        FloodWaitError(request=None, capture=90),
        FloodPremiumWaitError(request=None, capture=90),
        FloodTestPhoneWaitError(request=None, capture=90),
        SlowModeWaitError(request=None, capture=90),
    ],
)
async def test_flood_waits_carry_their_seconds(error: Exception) -> None:
    with pytest.raises(SendFloodWait) as raised:
        await TelethonAdapter(_RawTelethon(send_error=error)).send_text(FAKE_TELEGRAM_ID, TEXT)

    assert raised.value.seconds == 90
    assert raised.value.error_class == type(error).__name__
    assert raised.value.__suppress_context__ is True


async def test_a_flood_wait_while_resolving_the_peer_is_a_flood_wait() -> None:
    raw = _RawTelethon(peer_error=FloodWaitError(request=None, capture=90))

    with pytest.raises(SendFloodWait):
        await TelethonAdapter(raw).send_text(FAKE_TELEGRAM_ID, TEXT)

    assert raw.send_calls == []


@pytest.mark.parametrize(
    "error",
    [
        UserIsBlockedError(request=None),
        UserBlockedError(request=None),
        YouBlockedUserError(request=None),
        InputUserDeactivatedError(request=None),
        UserPrivacyRestrictedError(request=None),
        PeerIdInvalidError(request=None),
        PeerIdNotSupportedError(request=None),
        UserIdInvalidError(request=None),
        ChatWriteForbiddenError(request=None),
        ForbiddenError(request=None, message="FAKE_NEW_REFUSAL"),
        MessageTooLongError(request=None),
    ],
)
async def test_permanent_refusals_are_rejected(error: Exception) -> None:
    with pytest.raises(SendRejected) as raised:
        await TelethonAdapter(_RawTelethon(send_error=error)).send_text(FAKE_TELEGRAM_ID, TEXT)

    assert raised.value.error_class == type(error).__name__
    assert raised.value.__suppress_context__ is True


async def test_a_peer_that_cannot_be_resolved_is_rejected_and_nothing_is_sent() -> None:
    raw = _RawTelethon(peer_error=ValueError(f"no entity for {FAKE_TELEGRAM_ID}"))

    with pytest.raises(SendRejected) as raised:
        await TelethonAdapter(raw).send_text(FAKE_TELEGRAM_ID, TEXT)

    assert raised.value.error_class == PEER_NOT_FOUND
    assert raw.send_calls == []
    assert_absent(FAKE_TELEGRAM_ID, str(raised.value))


@pytest.mark.parametrize(
    "error",
    [
        ConnectionError("fake detail"),
        TimeoutError(),
        OSError("fake detail"),
        ValueError("Request was unsuccessful 3 time(s)"),
        RPCError(request=None, message="FAKE_RPC", code=500),
        ServerError(request=None, message="FAKE_SERVER"),
        PeerFloodError(request=None),
        AuthKeyUnregisteredError(request=None),
        UserDeactivatedError(request=None),
        UserDeactivatedBanError(request=None),
        RuntimeError("fake detail"),
    ],
)
async def test_everything_else_is_uncertain_with_the_class_name_only(error: Exception) -> None:
    with pytest.raises(SendUncertain) as raised:
        await TelethonAdapter(_RawTelethon(send_error=error)).send_text(FAKE_TELEGRAM_ID, TEXT)

    assert raised.value.error_class == type(error).__name__
    assert "fake detail" not in str(raised.value)
    assert raised.value.__suppress_context__ is True


async def test_a_send_without_a_response_message_is_uncertain() -> None:
    raw = _RawTelethon(returns_nothing=True)

    with pytest.raises(SendUncertain) as raised:
        await TelethonAdapter(raw).send_text(FAKE_TELEGRAM_ID, TEXT)

    assert raised.value.error_class == MISSING_RESPONSE
    assert len(raw.send_calls) == 1


async def test_a_network_failure_while_resolving_the_peer_is_uncertain_and_nothing_is_sent() -> (
    None
):
    raw = _RawTelethon(peer_error=ConnectionError("fake detail"))

    with pytest.raises(SendUncertain) as raised:
        await TelethonAdapter(raw).send_text(FAKE_TELEGRAM_ID, TEXT)

    assert raised.value.error_class == "ConnectionError"
    assert raw.send_calls == []


async def test_cancellation_is_not_turned_into_a_send_failure() -> None:
    raw = _RawTelethon(send_error=asyncio.CancelledError())  # type: ignore[arg-type]

    with pytest.raises(asyncio.CancelledError):
        await TelethonAdapter(raw).send_text(FAKE_TELEGRAM_ID, TEXT)


async def test_the_adapter_logs_nothing_at_any_level(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)

    await TelethonAdapter(_RawTelethon()).send_text(FAKE_TELEGRAM_ID, TEXT)
    with pytest.raises(SendRejected):
        await TelethonAdapter(_RawTelethon(send_error=UserIsBlockedError(request=None))).send_text(
            FAKE_TELEGRAM_ID, TEXT
        )

    assert caplog.records == []


async def test_get_full_user_builds_the_read_request_for_the_id() -> None:
    raw = _RawTelethon()

    response = await TelethonAdapter(raw).get_full_user(FAKE_TELEGRAM_ID)

    assert response is FAKE_FULL_USER
    assert len(raw.requests) == 1
    request = raw.requests[0]
    assert isinstance(request, GetFullUserRequest)
    assert request.id == FAKE_TELEGRAM_ID


async def test_the_read_and_lifecycle_members_are_passed_through() -> None:
    raw = _RawTelethon()
    adapter = TelethonAdapter(raw)

    async def callback(_: object) -> None:
        return None

    await adapter.connect()
    assert await adapter.is_user_authorized() is True
    await adapter.catch_up()
    adapter.add_event_handler(callback, object())
    assert adapter.remove_event_handler(callback) == 1
    await adapter.get_entity(FAKE_TELEGRAM_ID)
    assert (
        await adapter.download_profile_photo(FakeSender(), "a.jpg", download_big=False) == "a.jpg"
    )
    assert await adapter.download_media(FakeHistoryMessage(id=1), "b.jpg") == "b.jpg"
    seen = [m.id async for m in adapter.iter_messages(FakeSender(), 5, search="q", offset_id=9)]
    await adapter.disconnect()

    assert seen == [1]
    assert raw.calls == [
        "connect",
        "is_user_authorized",
        "catch_up",
        "add_event_handler",
        "remove_event_handler",
        "get_entity",
        "download_profile_photo False",
        "download_media",
        "iter_messages 5 q 9",
        "disconnect",
    ]
    assert raw.send_calls == []


async def test_the_disconnected_future_is_the_clients() -> None:
    raw = _RawTelethon()
    future: asyncio.Future[object] = asyncio.get_running_loop().create_future()
    raw.disconnected = future

    assert TelethonAdapter(raw).disconnected is future
