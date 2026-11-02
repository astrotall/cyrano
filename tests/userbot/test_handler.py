from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

import pytest
from telethon import events

from cyrano.domain.models import IncomingMessage
from cyrano.services.contact_events import InMemoryContactEvents
from cyrano.services.intake import IntakeService
from cyrano.userbot.filters import Rejection
from cyrano.userbot.handler import (
    INTAKE_RETRY_DELAYS_SECONDS,
    IncomingHandler,
    display_name_of,
    to_incoming_message,
)
from cyrano.userbot.protocols import UserbotClient
from tests.userbot.fakes import (
    FAKE_FIRST_NAME,
    FAKE_LAST_NAME,
    FAKE_SENDER_ID,
    FAKE_SENT_AT,
    FAKE_TEXT,
    FAKE_USERNAME,
    FakeClient,
    FakeEvent,
    FakeIntake,
    FakeSender,
    InMemoryContacts,
    InMemoryMessages,
    RecordingSleep,
)


async def test_private_message_is_converted_and_passed_to_the_intake() -> None:
    intake = FakeIntake()
    handler = IncomingHandler(intake)

    await handler.handle(FakeEvent(id=91))

    assert intake.received == [
        IncomingMessage(
            tg_id=FAKE_SENDER_ID,
            username=FAKE_USERNAME,
            display_name=f"{FAKE_FIRST_NAME} {FAKE_LAST_NAME}",
            tg_message_id=91,
            text=FAKE_TEXT,
            sent_at=FAKE_SENT_AT,
        )
    ]
    assert handler.handled == 1
    assert handler.rejected == 0
    assert handler.failed == 0


async def test_missing_username_and_name_are_converted_to_none() -> None:
    intake = FakeIntake()
    sender = FakeSender(username=None, first_name=None, last_name=None)

    await IncomingHandler(intake).handle(FakeEvent(sender=sender))

    assert intake.received[0].username is None
    assert intake.received[0].display_name is None


@pytest.mark.parametrize(
    ("first", "last", "expected"),
    [
        ("Fakefirst", None, "Fakefirst"),
        (None, "Fakelast", "Fakelast"),
        ("  Fakefirst ", " Fakelast  ", "Fakefirst Fakelast"),
        ("", "", None),
        ("   ", None, None),
    ],
)
def test_display_name_joins_the_parts_that_exist(
    first: str | None, last: str | None, expected: str | None
) -> None:
    assert display_name_of(FakeSender(first_name=first, last_name=last)) == expected


def test_empty_username_counts_as_missing() -> None:
    converted = to_incoming_message(FakeEvent(), FakeSender(username=""))

    assert converted.username is None


async def test_media_only_message_has_empty_text() -> None:
    intake = FakeIntake()
    handler = IncomingHandler(intake)

    await handler.handle(FakeEvent(raw_text=None))
    await handler.handle(FakeEvent(id=78, raw_text=""))

    assert [m.text for m in intake.received] == ["", ""]


async def test_event_time_is_kept_in_utc() -> None:
    intake = FakeIntake()

    await IncomingHandler(intake).handle(FakeEvent(date=datetime(2025, 5, 5, 5, 5, tzinfo=UTC)))

    assert intake.received[0].sent_at == datetime(2025, 5, 5, 5, 5, tzinfo=UTC)


@pytest.mark.parametrize(
    ("event", "reason"),
    [
        (FakeEvent(out=True), Rejection.OUTGOING),
        (FakeEvent(is_group=True, is_private=False), Rejection.GROUP),
        (FakeEvent(is_channel=True, is_private=False), Rejection.CHANNEL),
        (FakeEvent(sender=None), Rejection.NO_SENDER),
        (FakeEvent(sender=FakeSender(bot=True)), Rejection.BOT),
        (FakeEvent(sender=FakeSender(deleted=True)), Rejection.DELETED),
        (FakeEvent(sender=FakeSender(is_self=True)), Rejection.SELF),
        (FakeEvent(sender_id=777000), Rejection.SERVICE_ACCOUNT),
    ],
)
async def test_rejected_events_reach_nothing_and_are_only_counted(
    event: FakeEvent, reason: Rejection, caplog: pytest.LogCaptureFixture
) -> None:
    intake = FakeIntake()
    handler = IncomingHandler(intake)

    with caplog.at_level(logging.DEBUG):
        await handler.handle(event)

    assert intake.received == []
    assert handler.rejected == 1
    assert handler.handled == 0
    rejection_logs = [r for r in caplog.records if r.getMessage() == "incoming event rejected"]
    assert [r.levelno for r in rejection_logs] == [logging.DEBUG]
    assert rejection_logs[0].__dict__["reason"] == reason.value


async def test_group_event_does_not_even_fetch_the_sender() -> None:
    class _Counting(FakeEvent):
        fetched = 0

        async def get_sender(self) -> FakeSender | None:
            type(self).fetched += 1
            return FakeSender()

    await IncomingHandler(FakeIntake()).handle(_Counting(is_group=True, is_private=False))

    assert _Counting.fetched == 0


async def test_failing_intake_does_not_propagate_and_logs_only_the_error_class(
    caplog: pytest.LogCaptureFixture,
) -> None:
    intake = FakeIntake(error=RuntimeError(f"boom {FAKE_TEXT} {FAKE_USERNAME}"))
    handler = IncomingHandler(intake, sleep=RecordingSleep())

    with caplog.at_level(logging.DEBUG):
        await handler.handle(FakeEvent())
        await handler.handle(FakeEvent(id=78))

    assert handler.failed == 2
    assert handler.handled == 0
    assert intake.calls == 2 * (len(INTAKE_RETRY_DELAYS_SECONDS) + 1)
    failures = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(failures) == 2
    assert failures[-1].__dict__["error"] == "RuntimeError"
    assert failures[-1].__dict__["failed"] == 2
    assert failures[-1].exc_info is None
    logged = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    for personal in (
        FAKE_TEXT,
        FAKE_USERNAME,
        FAKE_FIRST_NAME,
        FAKE_LAST_NAME,
        str(FAKE_SENDER_ID),
    ):
        assert personal not in logged


async def test_malformed_event_does_not_propagate() -> None:
    intake = FakeIntake()
    handler = IncomingHandler(intake)

    await handler.handle(FakeEvent(date=datetime(2025, 1, 1)))
    await handler.handle(FakeEvent(id=0))
    await handler.handle(FakeEvent(sender=FakeSender(id=-5)))

    assert handler.failed == 3
    assert intake.received == []


async def test_one_failure_does_not_stop_later_messages() -> None:
    intake = FakeIntake()
    handler = IncomingHandler(intake)

    await handler.handle(FakeEvent(date=datetime(2025, 1, 1)))
    await handler.handle(FakeEvent(id=79))

    assert [m.tg_message_id for m in intake.received] == [79]


def test_register_adds_one_incoming_new_message_handler_and_unregister_removes_it() -> None:
    client = FakeClient()
    typed_client: UserbotClient = client
    handler = IncomingHandler(FakeIntake())

    handler.register(typed_client)

    assert len(client.handlers) == 1
    _, builder = client.handlers[0]
    assert isinstance(builder, events.NewMessage)
    assert builder.incoming is True
    assert builder.outgoing is False

    handler.unregister(typed_client)

    assert client.handlers == []


async def test_no_write_method_is_called_for_any_kind_of_event() -> None:
    client = FakeClient()
    handler = IncomingHandler(FakeIntake())
    handler.register(client)
    sent = [
        FakeEvent(),
        FakeEvent(id=2, raw_text=None),
        FakeEvent(id=3, out=True),
        FakeEvent(id=4, is_group=True, is_private=False),
        FakeEvent(id=5, sender=FakeSender(bot=True)),
        FakeEvent(id=6, sender=None),
    ]

    for event in sent:
        await client.deliver(event)

    assert client.write_attempts == []
    assert [attempt for event in sent for attempt in event.write_attempts] == []
    assert handler.failed == 0


async def test_the_retry_delays_are_short_and_growing() -> None:
    assert INTAKE_RETRY_DELAYS_SECONDS == (1.0, 2.0)


async def test_intake_that_fails_once_is_retried_after_the_first_delay() -> None:
    sleep = RecordingSleep()
    intake = FakeIntake(error=RuntimeError("fake transient"), failures=1)
    handler = IncomingHandler(intake, sleep=sleep)

    await handler.handle(FakeEvent(id=31))

    assert intake.calls == 2
    assert [m.tg_message_id for m in intake.received] == [31]
    assert sleep.delays == [INTAKE_RETRY_DELAYS_SECONDS[0]]
    assert handler.handled == 1
    assert handler.failed == 0


async def test_intake_that_fails_twice_succeeds_on_the_third_attempt() -> None:
    sleep = RecordingSleep()
    intake = FakeIntake(error=RuntimeError("fake transient"), failures=2)
    handler = IncomingHandler(intake, sleep=sleep)

    await handler.handle(FakeEvent(id=32))

    assert intake.calls == 3
    assert [m.tg_message_id for m in intake.received] == [32]
    assert sleep.delays == list(INTAKE_RETRY_DELAYS_SECONDS)
    assert handler.failed == 0


async def test_intake_that_always_fails_is_tried_three_times_then_dropped_and_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sleep = RecordingSleep()
    intake = FakeIntake(error=RuntimeError(f"boom {FAKE_TEXT} {FAKE_USERNAME}"), failures=3)
    handler = IncomingHandler(intake, sleep=sleep)

    with caplog.at_level(logging.DEBUG):
        await handler.handle(FakeEvent(id=33))

    assert intake.calls == 3
    assert sleep.delays == list(INTAKE_RETRY_DELAYS_SECONDS)
    assert intake.received == []
    assert handler.failed == 1
    assert handler.handled == 0
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert errors[0].__dict__["error"] == "RuntimeError"
    assert errors[0].__dict__["failed"] == 1
    logged = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    for personal in (
        FAKE_TEXT,
        FAKE_USERNAME,
        FAKE_FIRST_NAME,
        FAKE_LAST_NAME,
        str(FAKE_SENDER_ID),
    ):
        assert personal not in logged

    await handler.handle(FakeEvent(id=34))

    assert [m.tg_message_id for m in intake.received] == [34]
    assert handler.handled == 1


async def test_the_listener_keeps_working_after_a_message_that_exhausted_its_attempts() -> None:
    intake = FakeIntake(error=RuntimeError("fake outage"), failures=3)
    client = FakeClient()
    handler = IncomingHandler(intake, sleep=RecordingSleep())
    handler.register(client)

    await client.deliver(FakeEvent(id=41))
    await client.deliver(FakeEvent(id=42))

    assert [m.tg_message_id for m in intake.received] == [42]
    assert handler.failed == 1
    assert handler.handled == 1
    assert client.write_attempts == []


async def test_cancellation_is_not_retried_and_propagates() -> None:
    sleep = RecordingSleep()
    intake = FakeIntake(error=asyncio.CancelledError())
    handler = IncomingHandler(intake, sleep=sleep)

    with pytest.raises(asyncio.CancelledError):
        await handler.handle(FakeEvent())

    assert intake.calls == 1
    assert sleep.delays == []
    assert handler.failed == 0


async def test_cancellation_during_the_retry_delay_propagates() -> None:
    started = asyncio.Event()

    async def blocked_sleep(_: float) -> None:
        started.set()
        await asyncio.sleep(3600)

    intake = FakeIntake(error=RuntimeError("fake transient"), failures=1)
    task = asyncio.create_task(IncomingHandler(intake, sleep=blocked_sleep).handle(FakeEvent()))
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert intake.calls == 1


async def test_a_malformed_event_is_not_retried() -> None:
    sleep = RecordingSleep()
    intake = FakeIntake()
    handler = IncomingHandler(intake, sleep=sleep)

    await handler.handle(FakeEvent(date=datetime(2025, 1, 1)))

    assert intake.calls == 0
    assert sleep.delays == []
    assert handler.failed == 1


async def test_a_transient_failure_then_success_stores_one_message_and_publishes_one_event() -> (
    None
):
    contacts = InMemoryContacts(lookup_failures=1)
    messages = InMemoryMessages()
    events = InMemoryContactEvents()
    handler = IncomingHandler(IntakeService(contacts, messages, events), sleep=RecordingSleep())

    await handler.handle(FakeEvent(id=51))

    assert len(contacts.stored) == 1
    assert [m.tg_message_id for m in messages.stored] == [51]
    assert events.published == 1
    assert handler.handled == 1
    assert handler.failed == 0
