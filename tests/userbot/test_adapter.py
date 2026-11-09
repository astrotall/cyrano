from __future__ import annotations

import asyncio
import inspect
import logging
import re
import typing
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone

import pytest
from telethon import errors as telethon_errors
from telethon.errors import (
    ActiveUserRequiredError,
    AuthKeyDuplicatedError,
    AuthKeyInvalidError,
    AuthKeyPermEmptyError,
    AuthKeyUnregisteredError,
    ChatWriteForbiddenError,
    FloodError,
    FloodPremiumWaitError,
    FloodTestPhoneWaitError,
    FloodWaitError,
    ForbiddenError,
    FrozenMethodInvalidError,
    InputUserDeactivatedError,
    MessageIdInvalidError,
    MessageIdsEmptyError,
    MessageTooLongError,
    MsgIdInvalidError,
    NotAllowedError,
    PeerFloodError,
    PeerIdInvalidError,
    PeerIdNotSupportedError,
    PremiumAccountRequiredError,
    RPCError,
    SensitiveChangeForbiddenError,
    ServerError,
    SessionExpiredError,
    SessionPasswordNeededError,
    SessionRevokedError,
    SlowModeWaitError,
    UnauthorizedError,
    UserBlockedError,
    UserDeactivatedBanError,
    UserDeactivatedError,
    UserIdInvalidError,
    UserIsBlockedError,
    UserPrivacyRestrictedError,
    UserRestrictedError,
    YouBlockedUserError,
)
from telethon.tl.functions.users import GetFullUserRequest

from cyrano.domain.enums import RejectionReason
from cyrano.domain.errors import (
    SendAccountBlocked,
    SendFailure,
    SendFloodWait,
    SendRejected,
    SendReplyTargetGone,
    SendUncertain,
)
from cyrano.services.message_transport import MessageTransport
from cyrano.userbot.adapter import (
    ACCOUNT_BLOCKS,
    CONTENT_REFUSALS,
    FLOOD_WAITS,
    MISSING_RESPONSE,
    PEER_NOT_FOUND,
    RECIPIENT_REFUSALS,
    REPLY_TARGET_REFUSALS,
    TelethonAdapter,
    classify_failure,
)
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

    assert public == set(typing.get_protocol_members(UserbotClient)) | {
        "send_text",
        "show_typing",
        "mark_read",
    }


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
    ],
)
async def test_refusals_about_the_recipient_are_rejected_for_the_recipient(
    error: Exception,
) -> None:
    with pytest.raises(SendRejected) as raised:
        await TelethonAdapter(_RawTelethon(send_error=error)).send_text(FAKE_TELEGRAM_ID, TEXT)

    assert raised.value.error_class == type(error).__name__
    assert raised.value.reason is RejectionReason.RECIPIENT
    assert raised.value.__suppress_context__ is True


async def test_a_text_telegram_finds_too_long_is_rejected_for_its_content() -> None:
    raw = _RawTelethon(send_error=MessageTooLongError(request=None))

    with pytest.raises(SendRejected) as raised:
        await TelethonAdapter(raw).send_text(FAKE_TELEGRAM_ID, TEXT)

    assert raised.value.error_class == "MessageTooLongError"
    assert raised.value.reason is RejectionReason.CONTENT
    assert raised.value.__suppress_context__ is True


ACCOUNT_LEVEL_ERRORS = (
    PeerFloodError(request=None),
    UserDeactivatedError(request=None),
    UserDeactivatedBanError(request=None),
    AuthKeyUnregisteredError(request=None),
    AuthKeyInvalidError(request=None),
    SessionRevokedError(request=None),
    SessionExpiredError(request=None),
    UserRestrictedError(request=None),
    FrozenMethodInvalidError(request=None),
    AuthKeyDuplicatedError(request=None),
    AuthKeyPermEmptyError(request=None),
    ActiveUserRequiredError(request=None),
    SessionPasswordNeededError(request=None),
    UnauthorizedError(request=None, message="FAKE_UNAUTHORIZED"),
)


@pytest.mark.parametrize(
    ("error_class", "parent"),
    [
        (AuthKeyInvalidError, UnauthorizedError),
        (SessionRevokedError, UnauthorizedError),
        (SessionExpiredError, UnauthorizedError),
        (UserRestrictedError, ForbiddenError),
        (UserPrivacyRestrictedError, ForbiddenError),
        (FrozenMethodInvalidError, FloodError),
        (AuthKeyPermEmptyError, UnauthorizedError),
        (ActiveUserRequiredError, UnauthorizedError),
        (SessionPasswordNeededError, UnauthorizedError),
        (PremiumAccountRequiredError, ForbiddenError),
        (SensitiveChangeForbiddenError, ForbiddenError),
        (NotAllowedError, ForbiddenError),
    ],
)
def test_the_installed_telethon_has_the_parents_the_mapping_relies_on(
    error_class: type[Exception], parent: type[Exception]
) -> None:
    assert issubclass(error_class, parent)


def test_user_restricted_is_a_recipient_class_by_inheritance_and_the_account_check_wins() -> None:
    error = UserRestrictedError(request=None)

    assert isinstance(error, RECIPIENT_REFUSALS)
    assert isinstance(error, ACCOUNT_BLOCKS)
    assert isinstance(classify_failure(error), SendAccountBlocked)


def test_a_frozen_method_error_is_a_flood_error_but_not_one_the_flood_branch_lists() -> None:
    error = FrozenMethodInvalidError(request=None)

    assert isinstance(error, FloodError)
    assert not isinstance(error, FLOOD_WAITS)
    assert not issubclass(FrozenMethodInvalidError, FLOOD_WAITS)
    assert FloodError not in FLOOD_WAITS


def test_a_frozen_method_error_is_an_account_block_and_never_a_flood_wait() -> None:
    failure = classify_failure(FrozenMethodInvalidError(request=None))

    assert isinstance(failure, SendAccountBlocked)
    assert not isinstance(failure, SendFloodWait)
    assert failure.kind == "FrozenMethodInvalidError"


def test_the_flood_branch_lists_specific_classes_and_none_of_the_account_errors() -> None:
    assert all(error_class is not FloodError for error_class in FLOOD_WAITS)
    assert not any(issubclass(error_class, FLOOD_WAITS) for error_class in ACCOUNT_BLOCKS)


def test_a_real_flood_wait_still_wins_over_the_account_blocks() -> None:
    failure = classify_failure(FloodWaitError(request=None, capture=7))

    assert isinstance(failure, SendFloodWait)
    assert failure.seconds == 7


def test_the_bare_unauthorized_error_is_listed_after_every_subclass_that_is_mapped() -> None:
    position = ACCOUNT_BLOCKS.index(UnauthorizedError)
    subclasses = [
        error_class
        for error_class in ACCOUNT_BLOCKS
        if error_class is not UnauthorizedError and issubclass(error_class, UnauthorizedError)
    ]

    assert subclasses
    assert all(ACCOUNT_BLOCKS.index(error_class) < position for error_class in subclasses)


def test_the_bare_unauthorized_error_blocks_under_its_own_name() -> None:
    failure = classify_failure(UnauthorizedError(request=None, message="FAKE_UNAUTHORIZED"))

    assert isinstance(failure, SendAccountBlocked)
    assert failure.kind == "UnauthorizedError"


@pytest.mark.parametrize("error", ACCOUNT_LEVEL_ERRORS, ids=lambda error: type(error).__name__)
def test_an_account_error_keeps_its_own_class_name_as_the_kind(error: Exception) -> None:
    failure = classify_failure(error)

    assert isinstance(failure, SendAccountBlocked)
    assert failure.kind == type(error).__name__


def test_every_installed_unauthorized_subclass_is_an_account_block() -> None:
    unauthorized = [
        error_class
        for error_class in vars(telethon_errors).values()
        if isinstance(error_class, type) and issubclass(error_class, UnauthorizedError)
    ]

    assert len(unauthorized) > 1
    assert all(issubclass(error_class, ACCOUNT_BLOCKS) for error_class in unauthorized)


@pytest.mark.parametrize(
    "error",
    [
        PremiumAccountRequiredError(request=None),
        SensitiveChangeForbiddenError(request=None),
        NotAllowedError(request=None),
    ],
    ids=lambda error: type(error).__name__,
)
def test_the_three_unmapped_forbidden_subclasses_stay_recipient_level(error: Exception) -> None:
    failure = classify_failure(error)

    assert isinstance(failure, SendRejected)
    assert failure.reason is RejectionReason.RECIPIENT


def test_the_account_check_comes_before_the_refusal_checks() -> None:
    checked = re.findall(r"isinstance\(error, (\w+)\)", inspect.getsource(classify_failure))

    assert checked == ["FLOOD_WAITS", "ACCOUNT_BLOCKS", "CONTENT_REFUSALS", "RECIPIENT_REFUSALS"]


def test_only_user_restricted_of_the_account_errors_is_also_a_refusal() -> None:
    overlapping = {
        error_class
        for error_class in ACCOUNT_BLOCKS
        if issubclass(error_class, RECIPIENT_REFUSALS + CONTENT_REFUSALS + FLOOD_WAITS)
    }

    assert overlapping == {UserRestrictedError}


@pytest.mark.parametrize(
    "error",
    [
        UserPrivacyRestrictedError(request=None),
        ChatWriteForbiddenError(request=None),
        UserIsBlockedError(request=None),
    ],
)
def test_an_ordinary_refusal_stays_about_the_recipient(error: Exception) -> None:
    failure = classify_failure(error)

    assert isinstance(failure, SendRejected)
    assert failure.reason is RejectionReason.RECIPIENT


@pytest.mark.parametrize("error", ACCOUNT_LEVEL_ERRORS)
async def test_account_level_errors_block_with_their_kind(error: Exception) -> None:
    with pytest.raises(SendAccountBlocked) as raised:
        await TelethonAdapter(_RawTelethon(send_error=error)).send_text(FAKE_TELEGRAM_ID, TEXT)

    assert raised.value.kind == type(error).__name__
    assert raised.value.error_class == type(error).__name__
    assert raised.value.__suppress_context__ is True


@pytest.mark.parametrize("error", ACCOUNT_LEVEL_ERRORS)
async def test_an_account_level_error_while_resolving_the_peer_blocks_and_nothing_is_sent(
    error: Exception,
) -> None:
    raw = _RawTelethon(peer_error=error)

    with pytest.raises(SendAccountBlocked):
        await TelethonAdapter(raw).send_text(FAKE_TELEGRAM_ID, TEXT)

    assert raw.send_calls == []


async def test_a_peer_that_cannot_be_resolved_is_rejected_and_nothing_is_sent() -> None:
    raw = _RawTelethon(peer_error=ValueError(f"no entity for {FAKE_TELEGRAM_ID}"))

    with pytest.raises(SendRejected) as raised:
        await TelethonAdapter(raw).send_text(FAKE_TELEGRAM_ID, TEXT)

    assert raised.value.error_class == PEER_NOT_FOUND
    assert raised.value.reason is RejectionReason.RECIPIENT
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


class _ActionContext:
    def __init__(self, raw: _WriteRaw) -> None:
        self._raw = raw

    async def __aenter__(self) -> None:
        self._raw.events.append("typing started")
        self._raw.typing_started.set()
        if self._raw.enter_error is not None:
            raise self._raw.enter_error

    async def __aexit__(self, *_: object) -> None:
        self._raw.events.append("typing stopped")
        if self._raw.exit_error is not None:
            raise self._raw.exit_error


class _WriteRaw(_RawTelethon):
    def __init__(
        self,
        *,
        enter_error: Exception | None = None,
        exit_error: Exception | None = None,
        read_error: Exception | None = None,
        peer_error: Exception | None = None,
    ) -> None:
        super().__init__(peer_error=peer_error)
        self.events: list[str] = []
        self.typing_started = asyncio.Event()
        self.action_calls: list[tuple[object, str, dict[str, object]]] = []
        self.read_calls: list[object] = []
        self.enter_error = enter_error
        self.exit_error = exit_error
        self._read_error = read_error

    def action(self, peer: object, action: str, **kwargs: object) -> _ActionContext:
        self.action_calls.append((peer, action, kwargs))
        return _ActionContext(self)

    async def send_read_acknowledge(self, peer: object) -> bool:
        self.read_calls.append(peer)
        if self._read_error is not None:
            raise self._read_error
        return True


def _adapter(raw: _WriteRaw, slept: list[float] | None = None) -> TelethonAdapter:
    sleeps = slept if slept is not None else []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        raw.events.append("held")

    return TelethonAdapter(raw, sleep=sleep)


FLOOD_IN_WRITE = FloodWaitError(request=None, capture=33)
WRITE_FAILURES = [
    (FLOOD_IN_WRITE, SendFloodWait, "FloodWaitError"),
    (PeerFloodError(request=None), SendAccountBlocked, "PeerFloodError"),
    (UserDeactivatedBanError(request=None), SendAccountBlocked, "UserDeactivatedBanError"),
    (FrozenMethodInvalidError(request=None), SendAccountBlocked, "FrozenMethodInvalidError"),
    (AuthKeyUnregisteredError(request=None), SendAccountBlocked, "AuthKeyUnregisteredError"),
    (UserRestrictedError(request=None), SendAccountBlocked, "UserRestrictedError"),
    (UserPrivacyRestrictedError(request=None), SendRejected, "UserPrivacyRestrictedError"),
    (UserIsBlockedError(request=None), SendRejected, "UserIsBlockedError"),
    (ServerError(request=None, message="x", code=500), SendUncertain, "ServerError"),
    (TimeoutError(), SendUncertain, "TimeoutError"),
    (ValueError("marker"), SendUncertain, "ValueError"),
]
WRITE_FAILURE_IDS = [case[2] for case in WRITE_FAILURES]


async def test_typing_is_held_for_the_given_seconds_on_the_resolved_peer() -> None:
    raw = _WriteRaw()
    slept: list[float] = []

    await _adapter(raw, slept).show_typing(FAKE_TELEGRAM_ID, 4.5)

    assert raw.peer_lookups == [FAKE_TELEGRAM_ID]
    assert raw.action_calls == [(FAKE_PEER, "typing", {"auto_cancel": False})]
    assert slept == [4.5]
    assert raw.events == ["typing started", "held", "typing stopped"]
    assert raw.send_calls == []


async def test_a_read_mark_goes_to_the_resolved_peer_and_sends_nothing() -> None:
    raw = _WriteRaw()

    await _adapter(raw).mark_read(FAKE_TELEGRAM_ID)

    assert raw.peer_lookups == [FAKE_TELEGRAM_ID]
    assert raw.read_calls == [FAKE_PEER]
    assert raw.send_calls == []


@pytest.mark.parametrize(("error", "kind", "name"), WRITE_FAILURES, ids=WRITE_FAILURE_IDS)
async def test_a_failure_at_the_start_of_typing_is_mapped_like_a_send(
    error: Exception, kind: type[SendFailure], name: str
) -> None:
    with pytest.raises(kind) as caught:
        await _adapter(_WriteRaw(enter_error=error)).show_typing(FAKE_TELEGRAM_ID, 1)

    assert type(caught.value) is type(classify_failure(error))
    assert caught.value.error_class == name
    assert caught.value.__cause__ is None


@pytest.mark.parametrize(("error", "kind", "name"), WRITE_FAILURES, ids=WRITE_FAILURE_IDS)
async def test_a_failure_of_the_background_typing_request_surfaces_at_the_end_and_is_mapped(
    error: Exception, kind: type[SendFailure], name: str
) -> None:
    with pytest.raises(kind) as caught:
        await _adapter(_WriteRaw(exit_error=error)).show_typing(FAKE_TELEGRAM_ID, 1)

    assert caught.value.error_class == name


@pytest.mark.parametrize(("error", "kind", "name"), WRITE_FAILURES, ids=WRITE_FAILURE_IDS)
async def test_a_read_mark_failure_is_mapped_like_a_send(
    error: Exception, kind: type[SendFailure], name: str
) -> None:
    with pytest.raises(kind) as caught:
        await _adapter(_WriteRaw(read_error=error)).mark_read(FAKE_TELEGRAM_ID)

    assert caught.value.error_class == name
    assert caught.value.__cause__ is None


async def test_a_flood_wait_in_a_write_carries_its_seconds() -> None:
    with pytest.raises(SendFloodWait) as caught:
        await _adapter(_WriteRaw(read_error=FLOOD_IN_WRITE)).mark_read(FAKE_TELEGRAM_ID)

    assert caught.value.seconds == 33


@pytest.mark.parametrize("method", ["typing", "read"])
async def test_an_unknown_peer_is_a_recipient_refusal_as_on_a_send(method: str) -> None:
    raw = _WriteRaw(peer_error=ValueError("no such peer"))
    adapter = _adapter(raw)

    with pytest.raises(SendRejected) as caught:
        if method == "typing":
            await adapter.show_typing(FAKE_TELEGRAM_ID, 1)
        else:
            await adapter.mark_read(FAKE_TELEGRAM_ID)

    assert caught.value.error_class == PEER_NOT_FOUND
    assert caught.value.reason is RejectionReason.RECIPIENT
    assert raw.action_calls == []
    assert raw.read_calls == []


async def test_a_flood_wait_while_resolving_the_peer_of_a_read_mark_is_a_flood_wait() -> None:
    raw = _WriteRaw(peer_error=FLOOD_IN_WRITE)

    with pytest.raises(SendFloodWait):
        await _adapter(raw).mark_read(FAKE_TELEGRAM_ID)


async def test_cancelling_a_held_typing_ends_it_and_is_not_swallowed() -> None:
    raw = _WriteRaw()
    adapter = TelethonAdapter(raw)

    task = asyncio.create_task(adapter.show_typing(FAKE_TELEGRAM_ID, 3600))
    await raw.typing_started.wait()
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert raw.events == ["typing started", "typing stopped"]


async def test_the_new_writes_log_nothing(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    adapter = _adapter(_WriteRaw(read_error=ValueError("marker-value-zq")))

    with pytest.raises(SendUncertain):
        await adapter.mark_read(FAKE_TELEGRAM_ID)
    await _adapter(_WriteRaw()).show_typing(FAKE_TELEGRAM_ID, 1)

    assert caplog.text == ""


def test_the_adapter_method_names_are_exactly_the_transport_methods() -> None:
    assert set(typing.get_protocol_members(MessageTransport)) <= set(vars(TelethonAdapter))


REPLY_TARGET = 700555


async def test_a_reply_target_is_passed_to_telethon_as_reply_to() -> None:
    raw = _RawTelethon()

    sent = await TelethonAdapter(raw).send_text(FAKE_TELEGRAM_ID, TEXT, REPLY_TARGET)

    assert raw.send_calls == [
        (FAKE_PEER, TEXT, {"parse_mode": None, "link_preview": False, "reply_to": REPLY_TARGET})
    ]
    assert sent.tg_message_id == TG_MESSAGE_ID


async def test_without_a_target_the_call_is_exactly_the_plain_one() -> None:
    raw = _RawTelethon()

    await TelethonAdapter(raw).send_text(FAKE_TELEGRAM_ID, TEXT, None)

    assert raw.send_calls == [(FAKE_PEER, TEXT, {"parse_mode": None, "link_preview": False})]


def test_exactly_the_two_message_id_errors_mean_a_gone_target() -> None:
    assert (MessageIdInvalidError, MsgIdInvalidError) == REPLY_TARGET_REFUSALS


@pytest.mark.parametrize(
    "error", [MessageIdInvalidError(request=None), MsgIdInvalidError(request=None)]
)
async def test_a_message_id_error_with_a_target_is_a_gone_target(error: Exception) -> None:
    raw = _RawTelethon(send_error=error)

    with pytest.raises(SendReplyTargetGone) as raised:
        await TelethonAdapter(raw).send_text(FAKE_TELEGRAM_ID, TEXT, REPLY_TARGET)

    assert raised.value.error_class == type(error).__name__
    assert raised.value.__suppress_context__ is True
    assert str(REPLY_TARGET) not in str(raised.value)


@pytest.mark.parametrize(
    "error", [MessageIdInvalidError(request=None), MsgIdInvalidError(request=None)]
)
async def test_the_same_error_without_a_target_keeps_its_current_mapping(error: Exception) -> None:
    raw = _RawTelethon(send_error=error)

    with pytest.raises(SendFailure) as raised:
        await TelethonAdapter(raw).send_text(FAKE_TELEGRAM_ID, TEXT)

    assert not isinstance(raised.value, SendReplyTargetGone)
    assert type(raised.value) is type(classify_failure(error))
    assert isinstance(raised.value, SendUncertain)


@pytest.mark.parametrize(
    "error",
    [
        MessageIdsEmptyError(request=None),
        UserIsBlockedError(request=None),
        FloodWaitError(request=None, capture=90),
        MessageTooLongError(request=None),
        RPCError(request=None, message="REPLY_MESSAGE_ID_INVALID", code=400),
    ],
)
async def test_other_errors_with_a_target_keep_their_usual_mapping(error: Exception) -> None:
    raw = _RawTelethon(send_error=error)

    with pytest.raises(SendFailure) as raised:
        await TelethonAdapter(raw).send_text(FAKE_TELEGRAM_ID, TEXT, REPLY_TARGET)

    assert not isinstance(raised.value, SendReplyTargetGone)
    assert type(raised.value) is type(classify_failure(error))
