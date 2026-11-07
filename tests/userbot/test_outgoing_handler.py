from __future__ import annotations

import logging
from datetime import UTC, datetime

import pytest
from telethon import events

from cyrano.domain.enums import ContactStatus, OutboxSource, PauseReason
from cyrano.domain.errors import ContactNotFound
from cyrano.domain.models import Message, MessageCreate, OutgoingMessage
from cyrano.services.manual_intervention import ManualMessageDetector
from cyrano.userbot.filters import TELEGRAM_SERVICE_ACCOUNT_ID, Rejection
from cyrano.userbot.outgoing_handler import DETECTION_RETRY_DELAYS_SECONDS, OutgoingHandler
from cyrano.userbot.protocols import UserbotClient
from tests.privacy import FAKE_STRANGER_TELEGRAM_ID, assert_absent
from tests.services.outbox_fakes import FAKE_NOW, InMemoryOutbox, RecordingManualPauseNotifier
from tests.userbot.fakes import (
    FAKE_FIRST_NAME,
    FAKE_LAST_NAME,
    FAKE_SENDER_ID,
    FAKE_SENT_AT,
    FAKE_TEXT,
    FAKE_USERNAME,
    FakeClient,
    FakeDetector,
    FakeEvent,
    FakeSender,
    InMemoryContacts,
    InMemoryMessages,
    RecordingSleep,
)

OWN_ID = 424242424242


def _outgoing(**overrides: object) -> FakeEvent:
    fields: dict[str, object] = {
        "out": True,
        "sender_id": OWN_ID,
        "chat_id": FAKE_SENDER_ID,
        "chat": FakeSender(id=FAKE_SENDER_ID),
        "sender": FakeSender(id=OWN_ID, is_self=True),
    }
    return FakeEvent(**{**fields, **overrides})  # type: ignore[arg-type]


async def test_an_outgoing_private_message_is_converted_and_passed_to_the_detector() -> None:
    detector = FakeDetector()
    handler = OutgoingHandler(detector)

    await handler.handle(_outgoing(id=91))

    assert detector.received == [
        OutgoingMessage(
            tg_id=FAKE_SENDER_ID,
            tg_message_id=91,
            text=FAKE_TEXT,
            sent_at=FAKE_SENT_AT,
        )
    ]
    assert (handler.handled, handler.rejected, handler.failed) == (1, 0, 0)


async def test_the_peer_is_the_chat_and_never_the_account_itself() -> None:
    detector = FakeDetector()

    await OutgoingHandler(detector).handle(
        _outgoing(chat=FakeSender(id=FAKE_SENDER_ID + 5), sender_id=OWN_ID)
    )

    assert [m.tg_id for m in detector.received] == [FAKE_SENDER_ID + 5]


async def test_a_media_only_message_has_empty_text() -> None:
    detector = FakeDetector()
    handler = OutgoingHandler(detector)

    await handler.handle(_outgoing(raw_text=None))
    await handler.handle(_outgoing(id=78, raw_text=""))

    assert [m.text for m in detector.received] == ["", ""]


async def test_the_event_time_is_kept_in_utc() -> None:
    detector = FakeDetector()

    await OutgoingHandler(detector).handle(_outgoing(date=datetime(2025, 5, 5, 5, 5, tzinfo=UTC)))

    assert detector.received[0].sent_at == datetime(2025, 5, 5, 5, 5, tzinfo=UTC)


async def test_a_naive_or_missing_event_time_reaches_the_detector_as_a_missing_time() -> None:
    detector = FakeDetector()
    handler = OutgoingHandler(detector)

    await handler.handle(_outgoing(id=11, date=datetime(2025, 1, 1)))
    await handler.handle(_outgoing(id=12, date=None))

    assert [(m.tg_message_id, m.sent_at) for m in detector.received] == [(11, None), (12, None)]
    assert (handler.handled, handler.failed) == (2, 0)


@pytest.mark.parametrize(
    ("event", "reason"),
    [
        (_outgoing(out=False), Rejection.NOT_OUTGOING),
        (_outgoing(is_group=True, is_private=False), Rejection.GROUP),
        (_outgoing(is_channel=True, is_private=False), Rejection.CHANNEL),
        (_outgoing(is_private=False), Rejection.NOT_PRIVATE),
        (_outgoing(chat_id=None), Rejection.NO_PEER),
        (_outgoing(chat_id=-100500), Rejection.NO_PEER),
        (_outgoing(chat_id=TELEGRAM_SERVICE_ACCOUNT_ID), Rejection.SERVICE_ACCOUNT),
        (_outgoing(chat=None), Rejection.NO_PEER),
        (_outgoing(chat=FakeSender(bot=True)), Rejection.BOT),
        (_outgoing(chat=FakeSender(deleted=True)), Rejection.DELETED),
        (_outgoing(chat=FakeSender(is_self=True)), Rejection.SELF),
        (_outgoing(chat=FakeSender(id=TELEGRAM_SERVICE_ACCOUNT_ID)), Rejection.SERVICE_ACCOUNT),
    ],
)
async def test_rejected_events_reach_nothing_and_are_only_counted_at_debug(
    event: FakeEvent, reason: Rejection, caplog: pytest.LogCaptureFixture
) -> None:
    detector = FakeDetector()
    handler = OutgoingHandler(detector)

    with caplog.at_level(logging.DEBUG):
        await handler.handle(event)

    assert detector.received == []
    assert (handler.rejected, handler.handled, handler.failed) == (1, 0, 0)
    rejections = [r for r in caplog.records if r.getMessage() == "outgoing event rejected"]
    assert [r.levelno for r in rejections] == [logging.DEBUG]
    assert rejections[0].__dict__["reason"] == reason.value
    assert [r for r in caplog.records if r.levelno >= logging.INFO] == []


async def test_a_group_event_does_not_even_fetch_the_chat() -> None:
    class _Counting(FakeEvent):
        fetched = 0

        async def get_chat(self) -> FakeSender | None:
            type(self).fetched += 1
            return FakeSender()

    await OutgoingHandler(FakeDetector()).handle(
        _Counting(out=True, is_group=True, is_private=False)
    )

    assert _Counting.fetched == 0


async def test_a_failing_detector_does_not_propagate_and_logs_only_the_error_class(
    caplog: pytest.LogCaptureFixture,
) -> None:
    detector = FakeDetector(error=RuntimeError(f"boom {FAKE_TEXT} {FAKE_USERNAME}"))
    handler = OutgoingHandler(detector, sleep=RecordingSleep())

    with caplog.at_level(logging.DEBUG):
        await handler.handle(_outgoing())
        await handler.handle(_outgoing(id=78))

    assert (handler.failed, handler.handled) == (2, 0)
    assert detector.calls == 2 * (len(DETECTION_RETRY_DELAYS_SECONDS) + 1)
    failures = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(failures) == 2
    assert failures[-1].__dict__["error"] == "RuntimeError"
    assert failures[-1].exc_info is None
    logged = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    for personal in (FAKE_TEXT, FAKE_USERNAME, FAKE_FIRST_NAME, FAKE_LAST_NAME, FAKE_SENDER_ID):
        assert_absent(personal, logged)


async def test_a_detector_that_fails_once_is_retried_after_the_first_delay() -> None:
    sleep = RecordingSleep()
    detector = FakeDetector(error=RuntimeError("fake transient"), failures=1)
    handler = OutgoingHandler(detector, sleep=sleep)

    await handler.handle(_outgoing(id=31))

    assert detector.calls == 2
    assert [m.tg_message_id for m in detector.received] == [31]
    assert sleep.delays == [DETECTION_RETRY_DELAYS_SECONDS[0]]
    assert (handler.handled, handler.failed) == (1, 0)


async def test_a_chat_lookup_that_fails_is_retried_then_counted_without_reaching_the_detector() -> (
    None
):
    detector = FakeDetector()
    sleep = RecordingSleep()
    handler = OutgoingHandler(detector, sleep=sleep)

    await handler.handle(_outgoing(chat_error=ConnectionError("fake")))

    assert detector.received == []
    assert sleep.delays == list(DETECTION_RETRY_DELAYS_SECONDS)
    assert handler.failed == 1


async def test_a_malformed_event_does_not_propagate() -> None:
    detector = FakeDetector()
    handler = OutgoingHandler(detector)

    await handler.handle(_outgoing(id=0))
    await handler.handle(_outgoing(chat=FakeSender(id=-5)))

    assert handler.failed == 2
    assert detector.received == []


async def test_one_failure_does_not_stop_later_messages() -> None:
    detector = FakeDetector()
    handler = OutgoingHandler(detector)

    await handler.handle(_outgoing(id=0))
    await handler.handle(_outgoing(id=79))

    assert [m.tg_message_id for m in detector.received] == [79]


def test_the_retry_delays_are_short_and_growing() -> None:
    assert DETECTION_RETRY_DELAYS_SECONDS == (1.0, 2.0)


def test_register_adds_one_outgoing_new_message_handler_and_unregister_removes_it() -> None:
    client = FakeClient()
    typed_client: UserbotClient = client
    handler = OutgoingHandler(FakeDetector())

    handler.register(typed_client)

    assert len(client.handlers) == 1
    _, builder = client.handlers[0]
    assert isinstance(builder, events.NewMessage)
    assert builder.outgoing is True
    assert builder.incoming is False

    handler.unregister(typed_client)

    assert client.handlers == []


async def test_no_write_method_is_called_for_any_kind_of_event() -> None:
    client = FakeClient()
    handler = OutgoingHandler(FakeDetector())
    handler.register(client)
    sent = [
        _outgoing(),
        _outgoing(id=2, raw_text=None),
        _outgoing(id=3, out=False),
        _outgoing(id=4, is_group=True, is_private=False),
        _outgoing(id=5, chat=FakeSender(bot=True)),
        _outgoing(id=6, chat=None),
    ]

    for event in sent:
        await client.deliver(event)

    assert client.write_attempts == []
    assert [attempt for event in sent for attempt in event.write_attempts] == []
    assert handler.failed == 0


class _StoreFailsAfterThePause(InMemoryMessages):
    async def append(self, data: MessageCreate) -> Message:
        raise ContactNotFound(data.contact_id)


async def test_a_detection_that_already_paused_the_contact_is_never_retried() -> None:
    contacts = InMemoryContacts()
    contact = await contacts.seed(FAKE_SENDER_ID, status=ContactStatus.ACTIVE, mode_id=1)
    notifier = RecordingManualPauseNotifier()
    sleep = RecordingSleep()
    detector = ManualMessageDetector(
        contacts, _StoreFailsAfterThePause(), InMemoryOutbox(), notifier, sleep=sleep
    )
    queue = detector._outbox
    assert isinstance(queue, InMemoryOutbox)
    await queue.enqueue(contact.id, "fake", FAKE_NOW, OutboxSource.MANUAL_TEST)
    handler = OutgoingHandler(detector, sleep=sleep)

    await handler.handle(_outgoing())

    assert contacts.status_changes == [(contact.id, ContactStatus.PAUSED)]
    assert notifier.contact_ids == [contact.id]
    assert sleep.delays == []
    assert (handler.handled, handler.failed) == (1, 0)
    paused = await contacts.get_by_id(contact.id)
    assert paused is not None
    assert paused.pause_reason is PauseReason.MANUAL_MESSAGE


async def test_the_listener_stays_alive_after_a_detector_failure() -> None:
    client = FakeClient()
    detector = FakeDetector(error=RuntimeError("fake"), failures=3)
    handler = OutgoingHandler(detector, sleep=RecordingSleep())
    handler.register(client)

    await client.deliver(_outgoing(id=1))
    await client.deliver(_outgoing(id=2))

    assert [m.tg_message_id for m in detector.received] == [2]
    assert handler.failed == 1


async def test_a_message_to_a_stranger_reaches_the_detector_which_ignores_it() -> None:
    contacts = InMemoryContacts()
    detector = ManualMessageDetector(
        contacts,
        InMemoryMessages(),
        InMemoryOutbox(),
        RecordingManualPauseNotifier(),
        sleep=RecordingSleep(),
    )
    handler = OutgoingHandler(detector)

    await handler.handle(_outgoing(chat=FakeSender(id=FAKE_STRANGER_TELEGRAM_ID)))

    assert (handler.handled, handler.failed) == (1, 0)
    assert contacts.stored == {}
