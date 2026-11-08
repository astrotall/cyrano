from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from datetime import datetime, timedelta

import pytest

from cyrano.bot import messages as bot_messages
from cyrano.domain.enums import (
    ContactStatus,
    EarlyReadOutcome,
    EarlyReadReason,
    OutboxSource,
    OutboxState,
    PauseReason,
    RejectionReason,
)
from cyrano.domain.errors import SendAccountBlocked, SendFloodWait, SendRejected
from cyrano.logging import JsonFormatter
from cyrano.services.outbox_sender import (
    EARLY_READ_DEADLINE,
    EARLY_READ_LOG_MESSAGE,
    EARLY_READ_POLL_INTERVAL,
    FLOOD_WAIT_MARGIN,
    SenderTimings,
)
from tests.capped_loop import CappedLoop
from tests.privacy import FAKE_TELEGRAM_ID, assert_absent
from tests.services.early_read_rig import (
    BREAKS,
    CLASS_PROFILE,
    CONTACT_NAME_ZQ,
    CONTACT_USERNAME_ZQ,
    INCOMING_TEXT_ZQ,
    NOW,
    Break,
    EarlyReadRig,
)
from tests.services.outbox_fakes import (
    MARK_READ_STEP,
    SEND_TEXT_STEP,
    SHOW_TYPING_STEP,
    ScriptedTransport,
)

SENDER_LOGGER = "cyrano.services.outbox_sender"
DETAIL_MARKER = "early-detail-marker-zq"
REPLY_TEXT = "early-reply-marker-zq a short invented reply"
FLOOD_LONG = 120
FLOOD_SHORT = 30
WAIT_LIMIT = 2


async def _contact_with_request(rig: EarlyReadRig, delay: float = 0) -> int:
    contact = await rig.contact()
    rig.queue.register(contact.id, rig.clock() + timedelta(seconds=delay))
    return contact.id


def _early_lines(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.name == SENDER_LOGGER and record.getMessage() == EARLY_READ_LOG_MESSAGE
    ]


def _json_output(caplog: pytest.LogCaptureFixture) -> str:
    formatter = JsonFormatter()
    return "\n".join(formatter.format(record) for record in caplog.records)


async def test_a_due_request_marks_the_chat_read_exactly_once() -> None:
    rig = EarlyReadRig()
    contact_id = await _contact_with_request(rig)
    contact = await rig.contacts.get_by_id(contact_id)
    assert contact is not None
    sender = rig.sender()

    assert await sender.execute_early_reads() == 1
    assert await sender.execute_early_reads() == 0

    assert rig.transport.read_ids == [contact.tg_id]
    assert rig.transport.steps == [MARK_READ_STEP]
    assert rig.queue.pending() == []
    assert sender.early_read_counts[EarlyReadOutcome.OK] == 1


async def test_a_request_that_is_not_due_yet_is_left_alone() -> None:
    rig = EarlyReadRig()
    await _contact_with_request(rig, delay=3)
    sender = rig.sender()

    assert await sender.execute_early_reads() == 0

    assert rig.transport.read_ids == []
    assert len(rig.queue.pending()) == 1
    rig.clock.advance(3)
    assert await sender.execute_early_reads() == 1


async def test_each_request_is_executed_in_due_order() -> None:
    rig = EarlyReadRig()
    first = await rig.contact()
    second = await rig.contact()
    rig.queue.register(second.id, NOW + timedelta(seconds=1))
    rig.queue.register(first.id, NOW + timedelta(seconds=2))
    rig.clock.advance(5)

    assert await rig.sender().execute_early_reads() == 2

    assert rig.transport.read_ids == [second.tg_id, first.tg_id]


async def _pause_contact(rig: EarlyReadRig) -> None:
    [request] = rig.queue.pending()
    await rig.contacts.set_status(request.contact_id, ContactStatus.PAUSED)


async def _hand_the_contact_over(rig: EarlyReadRig) -> None:
    [request] = rig.queue.pending()
    await rig.contacts.set_status(
        request.contact_id, ContactStatus.PAUSED, pause_reason=PauseReason.STOP_TRIGGER
    )


async def _ignore_the_contact(rig: EarlyReadRig) -> None:
    [request] = rig.queue.pending()
    await rig.contacts.set_status(request.contact_id, ContactStatus.IGNORED)


async def _start_a_class(rig: EarlyReadRig) -> None:
    rig.profiles.profile = CLASS_PROFILE


RECHECKS: list[tuple[EarlyReadReason, Break]] = [
    *BREAKS,
    (EarlyReadReason.CONTACT_NOT_ACTIVE, _pause_contact),
    (EarlyReadReason.CONTACT_NOT_ACTIVE, _hand_the_contact_over),
    (EarlyReadReason.CONTACT_NOT_ACTIVE, _ignore_the_contact),
    (EarlyReadReason.BLOCKED_WINDOW, _start_a_class),
]


@pytest.mark.parametrize(
    ("reason", "apply"),
    RECHECKS,
    ids=[f"{reason.value}-{apply.__name__.strip('_')}" for reason, apply in RECHECKS],
)
async def test_a_failed_recheck_skips_the_request_with_its_reason(
    reason: EarlyReadReason, apply: Break, caplog: pytest.LogCaptureFixture
) -> None:
    rig = EarlyReadRig()
    await _contact_with_request(rig)
    await apply(rig)
    caplog.set_level(logging.INFO)
    sender = rig.sender()

    assert await sender.execute_early_reads() == 1

    assert rig.transport.steps == []
    assert sender.early_read_counts[EarlyReadOutcome.SKIPPED] == 1
    assert sender.early_read_counts[EarlyReadOutcome.OK] == 0
    [line] = _early_lines(caplog)
    assert line.__dict__["outcome"] == "skipped"
    assert line.__dict__["reason"] == reason.value
    assert rig.queue.pending() == []


async def test_a_skipped_request_is_not_retried_when_the_conditions_return() -> None:
    rig = EarlyReadRig()
    await _contact_with_request(rig)
    rig.switch.autopilot_enabled = False
    sender = rig.sender()
    await sender.execute_early_reads()
    rig.switch.autopilot_enabled = True

    assert await sender.execute_early_reads() == 0

    assert rig.transport.steps == []


async def test_a_long_flood_wait_pauses_the_sender_tells_the_owner_and_drops_the_request(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = EarlyReadRig()
    rig.transport = ScriptedTransport(read_errors=[SendFloodWait("FakeFloodWaitError", FLOOD_LONG)])
    await _contact_with_request(rig)
    caplog.set_level(logging.INFO)
    sender = rig.sender()

    await sender.execute_early_reads()

    state = rig.sender_state.state
    assert state.paused_until == NOW + timedelta(seconds=FLOOD_LONG + FLOOD_WAIT_MARGIN)
    assert state.pause_reason == "FakeFloodWaitError"
    assert rig.notifier.texts == [
        bot_messages.OUTBOX_FLOOD_WAIT_EARLY_READ.format(seconds=FLOOD_LONG)
    ]
    assert rig.queue.pending() == []
    assert sender.early_read_counts[EarlyReadOutcome.DROPPED] == 1
    [line] = _early_lines(caplog)
    assert line.__dict__["outcome"] == "dropped"
    assert line.__dict__["error_class"] == "SendFloodWait"
    rig.clock.advance(FLOOD_LONG + FLOOD_WAIT_MARGIN + 60)
    assert await sender.execute_early_reads() == 0
    assert rig.outbox.rows == {}


async def test_a_short_flood_wait_pauses_the_sender_without_a_notice() -> None:
    rig = EarlyReadRig()
    rig.transport = ScriptedTransport(
        read_errors=[SendFloodWait("FakeFloodWaitError", FLOOD_SHORT)]
    )
    await _contact_with_request(rig)

    await rig.sender().execute_early_reads()

    assert rig.sender_state.state.paused_until == NOW + timedelta(
        seconds=FLOOD_SHORT + FLOOD_WAIT_MARGIN
    )
    assert rig.notifier.texts == []


async def test_after_a_flood_wait_the_next_request_is_skipped_while_the_sender_is_paused() -> None:
    rig = EarlyReadRig()
    rig.transport = ScriptedTransport(read_errors=[SendFloodWait("FakeFloodWaitError", FLOOD_LONG)])
    first = await _contact_with_request(rig)
    second = await rig.contact()
    rig.queue.register(second.id, NOW)
    sender = rig.sender()

    await sender.execute_early_reads()

    assert first != second.id
    assert len(rig.transport.read_ids) == 1
    assert sender.early_read_counts[EarlyReadOutcome.DROPPED] == 1
    assert sender.early_read_counts[EarlyReadOutcome.SKIPPED] == 1
    assert len(rig.notifier.texts) == 1


async def test_an_account_level_error_stops_the_sender_with_one_notice() -> None:
    rig = EarlyReadRig()
    rig.transport = ScriptedTransport(read_errors=[SendAccountBlocked("FakeBannedError")])
    await _contact_with_request(rig)
    sender = rig.sender()

    await sender.execute_early_reads()

    assert rig.sender_state.state.stopped is True
    assert rig.sender_state.state.stop_reason == "FakeBannedError"
    assert rig.notifier.texts == [
        bot_messages.OUTBOX_ACCOUNT_STOPPED.format(kind="FakeBannedError")
    ]
    assert sender.early_read_counts[EarlyReadOutcome.DROPPED] == 1
    assert rig.outbox.rows == {}


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError(DETAIL_MARKER),
        ConnectionError(DETAIL_MARKER),
        SendRejected("FakeBlockedError", RejectionReason.RECIPIENT),
        SendRejected("FakeTooLongError", RejectionReason.CONTENT),
    ],
    ids=["runtime", "connection", "recipient", "content"],
)
async def test_any_other_failure_is_cosmetic_logged_by_class_counted_and_dropped(
    error: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    rig = EarlyReadRig()
    rig.transport = ScriptedTransport(read_errors=[error])
    contact_id = await _contact_with_request(rig)
    second = await rig.contact()
    rig.queue.register(second.id, NOW)
    caplog.set_level(logging.INFO)
    sender = rig.sender()

    await sender.execute_early_reads()

    assert sender.early_read_counts[EarlyReadOutcome.FAILED] == 1
    assert sender.early_read_counts[EarlyReadOutcome.OK] == 1
    first_line, _ = _early_lines(caplog)
    assert first_line.__dict__["outcome"] == "failed"
    assert first_line.__dict__["error_class"] == type(error).__name__
    assert DETAIL_MARKER not in _json_output(caplog)
    assert rig.sender_state.state.paused_until is None
    assert rig.sender_state.state.stopped is False
    assert rig.notifier.texts == []
    assert (await rig.contacts.get_by_id(contact_id)).status is ContactStatus.ACTIVE  # type: ignore[union-attr]
    assert rig.queue.pending() == []


class _HangingRead(ScriptedTransport):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.cancelled = False

    async def mark_read(self, tg_id: int) -> None:
        await super().mark_read(tg_id)
        self.started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            self.cancelled = True
            raise


async def test_a_read_that_hangs_runs_into_the_deadline_and_is_cosmetic(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = EarlyReadRig()
    transport = _HangingRead()
    await _contact_with_request(rig)
    caplog.set_level(logging.INFO)
    sender = rig.sender(transport=transport, timings=SenderTimings(early_read_deadline=0.01))

    assert await asyncio.wait_for(sender.execute_early_reads(), WAIT_LIMIT) == 1

    assert transport.cancelled is True
    assert sender.early_read_counts[EarlyReadOutcome.FAILED] == 1
    [line] = _early_lines(caplog)
    assert line.__dict__["error_class"] == "TimeoutError"


def test_the_named_constants_have_their_values() -> None:
    assert EARLY_READ_POLL_INTERVAL == 0.5
    assert EARLY_READ_DEADLINE == 15.0
    assert SenderTimings().early_read_poll_interval == EARLY_READ_POLL_INTERVAL
    assert SenderTimings().early_read_deadline == EARLY_READ_DEADLINE


async def test_an_early_read_is_not_a_message_for_the_outbox_or_the_limits() -> None:
    rig = EarlyReadRig()
    await _contact_with_request(rig)
    sender = rig.sender()
    before = await rig.messages.outgoing_times(NOW - timedelta(days=1), 100)

    await sender.execute_early_reads()

    assert await rig.messages.outgoing_times(NOW - timedelta(days=1), 100) == before == []
    assert rig.outbox.rows == {}
    assert rig.outbox.completed == []
    assert rig.transport.calls == []


async def test_a_reply_still_marks_the_chat_read_again_in_its_pre_send_part() -> None:
    rig = EarlyReadRig()
    contact_id = await _contact_with_request(rig)
    row = await rig.outbox.enqueue(contact_id, REPLY_TEXT, NOW, OutboxSource.AUTOPILOT)
    sender = rig.sender()

    await sender.execute_early_reads()
    assert await sender.send_next() is True

    assert rig.transport.steps == [MARK_READ_STEP, MARK_READ_STEP, SHOW_TYPING_STEP, SEND_TEXT_STEP]
    assert rig.outbox.rows[row.id].state is OutboxState.SENT


async def test_two_early_reads_of_one_contact_are_at_least_the_interval_apart() -> None:
    rig = EarlyReadRig()
    contact_id = await _contact_with_request(rig)
    sender = rig.sender()
    await sender.execute_early_reads()
    rig.clock.advance(1)
    assert rig.queue.register(contact_id, rig.clock()) is True

    assert await sender.execute_early_reads() == 0
    rig.clock.advance(3.9)
    assert await sender.execute_early_reads() == 0
    assert len(rig.transport.read_ids) == 1
    rig.clock.advance(0.1)
    assert await sender.execute_early_reads() == 1

    assert len(rig.transport.read_ids) == 2


async def test_one_info_line_per_request_carries_ids_numbers_and_outcomes_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = EarlyReadRig()
    first = await _contact_with_request(rig)
    second = await rig.contact(ContactStatus.ACTIVE)
    rig.queue.register(second.id, NOW)
    await rig.contacts.set_status(second.id, ContactStatus.PAUSED)
    caplog.set_level(logging.INFO)

    await rig.sender().execute_early_reads()

    ok, skipped = _early_lines(caplog)
    assert ok.levelno == skipped.levelno == logging.INFO
    assert (ok.__dict__["contact_id"], ok.__dict__["outcome"]) == (first, "ok")
    assert (skipped.__dict__["contact_id"], skipped.__dict__["outcome"]) == (second.id, "skipped")
    for line in (ok, skipped):
        assert isinstance(line.__dict__["duration_ms"], int)
    extras = set(ok.__dict__) - set(logging.makeLogRecord({}).__dict__) - {"message"}
    assert extras == {"contact_id", "outcome", "duration_ms"}


async def test_the_logs_and_notices_hold_none_of_the_invented_data(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = EarlyReadRig()
    rig.transport = ScriptedTransport(
        read_errors=[RuntimeError(DETAIL_MARKER), SendFloodWait("FakeFloodWaitError", FLOOD_LONG)]
    )
    contact = await rig.contact()
    await rig.ongoing_message(contact)
    caplog.set_level(logging.DEBUG)
    sender = rig.sender()
    for _ in range(3):
        rig.queue.register(contact.id, rig.clock())
        await sender.execute_early_reads()
        rig.clock.advance(10)
    await rig.scheduler.on_incoming(contact.id, 2)

    output = _json_output(caplog) + "\n".join(rig.notifier.texts)
    for marker in (INCOMING_TEXT_ZQ, CONTACT_NAME_ZQ, CONTACT_USERNAME_ZQ, DETAIL_MARKER):
        assert marker not in output
    assert_absent(contact.tg_id, output)
    assert_absent(FAKE_TELEGRAM_ID, output)


async def test_a_sender_without_the_early_read_parts_returns_at_once() -> None:
    rig = EarlyReadRig()
    sender = rig.sender(with_early_reads=False)

    await asyncio.wait_for(sender.run_early_reads(), WAIT_LIMIT)

    assert await sender.execute_early_reads() == 0


async def test_the_loop_polls_with_the_injected_sleep_and_executes_when_due() -> None:
    rig = EarlyReadRig()
    contact_id = await _contact_with_request(rig, delay=2)
    slept: list[float] = []

    async def on_sleep(seconds: float) -> bool:
        slept.append(seconds)
        rig.clock.advance(seconds)
        return bool(rig.transport.read_ids)

    driver = CappedLoop(on_sleep)
    sender = rig.sender(sleep=driver.sleep)

    await driver.run(sender.run_early_reads())

    assert set(slept) == {EARLY_READ_POLL_INTERVAL}
    assert len(slept) >= 4
    contact = await rig.contacts.get_by_id(contact_id)
    assert contact is not None
    assert rig.transport.read_ids == [contact.tg_id]


async def test_the_loop_survives_a_failing_poll() -> None:
    rig = EarlyReadRig()
    await _contact_with_request(rig)
    sender = rig.sender()
    calls = 0
    original = rig.gate.check

    async def flaky(contact_id: int):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ConnectionError(DETAIL_MARKER)
        return await original(contact_id)

    rig.gate.check = flaky  # type: ignore[method-assign]
    rig.queue.register(99, NOW)

    await sender.execute_early_reads()

    assert sender.early_read_counts[EarlyReadOutcome.FAILED] == 1
    assert sender.early_read_counts[EarlyReadOutcome.SKIPPED] == 1


async def test_cancellation_leaves_nothing_half_done_and_nothing_is_retried() -> None:
    rig = EarlyReadRig()
    transport = _HangingRead()
    await _contact_with_request(rig)

    async def never(seconds: float) -> bool:
        return False

    driver = CappedLoop(never)
    sender = rig.sender(transport=transport, sleep=driver.sleep)
    task = asyncio.create_task(sender.run_early_reads())
    await asyncio.wait_for(transport.started.wait(), WAIT_LIMIT)

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert task.cancelled()
    assert transport.cancelled is True
    assert rig.queue.pending() == []
    assert len(transport.read_ids) == 1
    assert sum(sender.early_read_counts.values()) == 0
    assert rig.sender_state.state.paused_until is None
    assert rig.outbox.rows == {}


class _InterleavingTransport(ScriptedTransport):
    def __init__(self, clock: Callable[[], datetime]) -> None:
        super().__init__()
        self.events: list[tuple[str, datetime]] = []
        self.typing_started = asyncio.Event()
        self.early_read_done = asyncio.Event()
        self._timeline = clock

    async def mark_read(self, tg_id: int) -> None:
        await super().mark_read(tg_id)
        self.events.append((MARK_READ_STEP, self._timeline()))
        if self.typing_started.is_set():
            self.early_read_done.set()

    async def show_typing(self, tg_id: int, seconds: float) -> None:
        await super().show_typing(tg_id, seconds)
        self.events.append(("typing start", self._timeline()))
        self.typing_started.set()
        await asyncio.wait_for(self.early_read_done.wait(), WAIT_LIMIT)
        self.events.append(("typing end", self._timeline()))


async def test_an_early_read_is_executed_while_the_row_loop_is_typing() -> None:
    rig = EarlyReadRig()
    transport = _InterleavingTransport(rig.clock)
    contact = await rig.contact()
    row = await rig.outbox.enqueue(contact.id, REPLY_TEXT, NOW, OutboxSource.AUTOPILOT)
    registered = False

    async def on_sleep(seconds: float) -> bool:
        nonlocal registered
        await asyncio.sleep(0)
        if transport.typing_started.is_set() and not registered:
            registered = True
            rig.queue.register(contact.id, rig.clock())
        return transport.early_read_done.is_set()

    driver = CappedLoop(on_sleep)
    sender = rig.sender(transport=transport, sleep=driver.sleep)
    row_loop = asyncio.create_task(sender.send_next())

    await driver.run(sender.run_early_reads())
    await asyncio.wait_for(row_loop, WAIT_LIMIT)

    assert [name for name, _ in transport.events] == [
        MARK_READ_STEP,
        "typing start",
        MARK_READ_STEP,
        "typing end",
    ]
    assert rig.outbox.rows[row.id].state is OutboxState.SENT
    assert sender.early_read_counts[EarlyReadOutcome.OK] == 1
