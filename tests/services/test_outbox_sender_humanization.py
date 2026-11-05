from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import pytest

from cyrano.bot.notifier import OUTBOX_NOTICES
from cyrano.db.repositories import (
    SqlContactRepository,
    SqlMessageRepository,
    SqlModeRepository,
    SqlOutboxRepository,
    SqlScheduleProfileRepository,
    SqlSenderStateRepository,
)
from cyrano.domain.enums import ContactStatus, OutboxSource, OutboxState, RejectionReason
from cyrano.domain.errors import (
    SendAccountBlocked,
    SendFloodWait,
    SendRejected,
    SendUncertain,
)
from cyrano.domain.models import Contact
from cyrano.domain.outbox import (
    CANCEL_REASON_STALE,
    ERROR_INTERRUPTED,
    ERROR_OUTSIDE_HOURS,
    ERROR_RATE_LIMITED,
    REFUSED_AUTOPILOT_DISABLED,
    REFUSED_CONTACT_NOT_ACTIVE,
    ScheduledMessage,
    SentMessage,
)
from cyrano.domain.schedule import MAX_READ_PAUSE_SECONDS, MAX_TYPING_SECONDS, ScheduleProfile
from cyrano.logging import JsonFormatter
from cyrano.services.humanization import ScheduleService, typing_seconds
from cyrano.services.outbox_sender import (
    FLOOD_WAIT_MARGIN,
    LONGEST_PRE_SEND,
    PRE_SEND_DEADLINE_MARGIN,
    SENDER_SEND_DEADLINE,
    SENDING_STALE_AFTER,
    SENDING_STALE_MARGIN,
    STEP_DURATION_UNIT,
    STEP_LOG_MESSAGE,
    OutboxSender,
    SenderTimings,
    SendStep,
)
from cyrano.services.send_guard import SendGuard
from cyrano.services.send_limiter import SendLimiter
from tests.privacy import FAKE_TELEGRAM_ID, assert_absent
from tests.services.outbox_fakes import (
    MARK_READ_STEP,
    SEND_TEXT_STEP,
    SHOW_TYPING_STEP,
    FakeClock,
    FakeLimits,
    InMemoryScheduleProfiles,
    RecordingNotifier,
    ScriptedTransport,
    eventually,
    stored_contact,
)
from tests.services.schedule_data import MONDAY, MOSCOW, TUESDAY, at_local, profile_with

TEXT = "humanize-text-marker-zq, long enough to show a typing pause of a few seconds"
USERNAME = "humanize_username_marker_zq"
DISPLAY_NAME = "HumanizeNameMarkerZq"
PAUSE_STEP = "pause"
NOON = at_local(TUESDAY, 12)
AT_NIGHT = at_local(TUESDAY, 5)
IN_BUSY = at_local(MONDAY, 13)
PROFILE = profile_with(
    busy=[{"days": ["mon"], "from": "12:45", "to": "14:45"}],
)
SEED = 23
FLOOD_SHORT = 30
FLOOD_LONG = 3600
PRE_SEND_STEPS = [MARK_READ_STEP, PAUSE_STEP, SHOW_TYPING_STEP]
ALL_STEPS = [*PRE_SEND_STEPS, SEND_TEXT_STEP]
COSMETIC_ERRORS = [
    SendRejected("FakeBlockedError", RejectionReason.RECIPIENT),
    SendRejected("FakeTooLongError", RejectionReason.CONTENT),
    SendUncertain("FakeNetworkError"),
    RuntimeError("cosmetic-detail-marker-zq"),
]
TIGHT_DEADLINE = 0.01
NEARLY_MINUS_ONE = -0.95
TIGHT_PROFILE = profile_with(
    typing={"chars_per_second": 5, "min_seconds": 0, "max_seconds": 0},
)
SENDER_LOGGER = "cyrano.services.outbox_sender"
SENT_LOG_MESSAGE = "outbox row sent"
STEP_FIELDS = (
    "step",
    "outcome",
    "error_class",
    "duration_ms",
    "row_id",
    "contact_id",
    "planned_seconds",
)
READ_TOOK = 0.25
SEND_TOOK = 0.5
DETAIL_MARKER = "cosmetic-detail-marker-zq"
LOG_STEPS = [SendStep.MARK_READ, SendStep.PAUSE, SendStep.TYPING, SendStep.SEND]


@dataclass(frozen=True)
class _Switch:
    autopilot_enabled: bool


class _RecordingSleep:
    def __init__(self, transport: ScriptedTransport) -> None:
        self.seconds: list[float] = []
        self._transport = transport

    async def __call__(self, seconds: float) -> None:
        self.seconds.append(seconds)
        self._transport.steps.append(PAUSE_STEP)


class _Hanging(ScriptedTransport):
    def __init__(self, *, hang_read: bool = False, hang_typing: bool = False) -> None:
        super().__init__()
        self.interrupted: list[str] = []
        self.hanging = asyncio.Event()
        self._hang_read = hang_read
        self._hang_typing = hang_typing

    async def _hang(self, step: str) -> None:
        self.hanging.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            self.interrupted.append(step)
            raise

    async def mark_read(self, tg_id: int) -> None:
        await super().mark_read(tg_id)
        if self._hang_read:
            await self._hang(MARK_READ_STEP)

    async def show_typing(self, tg_id: int, seconds: float) -> None:
        await super().show_typing(tg_id, seconds)
        if self._hang_typing:
            await self._hang(SHOW_TYPING_STEP)


class _Concurrency(ScriptedTransport):
    def __init__(self) -> None:
        super().__init__()
        self.running = 0
        self.peak = 0

    async def _occupy(self) -> None:
        self.running += 1
        self.peak = max(self.peak, self.running)
        await asyncio.sleep(0)
        self.running -= 1

    async def mark_read(self, tg_id: int) -> None:
        await super().mark_read(tg_id)
        await self._occupy()

    async def show_typing(self, tg_id: int, seconds: float) -> None:
        await super().show_typing(tg_id, seconds)
        await self._occupy()

    async def send_text(self, tg_id: int, text: str) -> SentMessage:
        await self._occupy()
        return await super().send_text(tg_id, text)


class _PausesDuringTyping(ScriptedTransport):
    def __init__(self, contacts: SqlContactRepository, contact_id: int) -> None:
        super().__init__()
        self._contacts = contacts
        self._contact_id = contact_id

    async def show_typing(self, tg_id: int, seconds: float) -> None:
        await super().show_typing(tg_id, seconds)
        await self._contacts.set_status(self._contact_id, ContactStatus.PAUSED)


class _Timed(ScriptedTransport):
    def __init__(
        self,
        clock: FakeClock,
        script: list[Exception] | None = None,
        *,
        read_errors: list[BaseException] | None = None,
        typing_errors: list[BaseException] | None = None,
    ) -> None:
        super().__init__(
            script or [],
            clock=clock,
            read_errors=read_errors or [],
            typing_errors=typing_errors or [],
        )
        self.fake_clock = clock

    async def mark_read(self, tg_id: int) -> None:
        self.fake_clock.advance(READ_TOOK)
        await super().mark_read(tg_id)

    async def show_typing(self, tg_id: int, seconds: float) -> None:
        self.fake_clock.advance(seconds)
        await super().show_typing(tg_id, seconds)

    async def send_text(self, tg_id: int, text: str) -> SentMessage:
        self.fake_clock.advance(SEND_TOOK)
        return await super().send_text(tg_id, text)


class _TimedSleep(_RecordingSleep):
    def __init__(self, transport: ScriptedTransport, clock: FakeClock) -> None:
        super().__init__(transport)
        self._fake_clock = clock

    async def __call__(self, seconds: float) -> None:
        self._fake_clock.advance(seconds)
        await super().__call__(seconds)


class _HangingSend(ScriptedTransport):
    async def send_text(self, tg_id: int, text: str) -> SentMessage:
        self.steps.append(SEND_TEXT_STEP)
        await asyncio.sleep(3600)
        raise AssertionError


def _sender_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.name == SENDER_LOGGER]


def _step_lines(caplog: pytest.LogCaptureFixture) -> list[dict[str, object]]:
    return [
        {key: getattr(record, key) for key in STEP_FIELDS if hasattr(record, key)}
        for record in _sender_records(caplog)
        if record.getMessage() == STEP_LOG_MESSAGE
    ]


def _outcomes(caplog: pytest.LogCaptureFixture) -> list[tuple[object, object, object]]:
    return [
        (line["step"], line["outcome"], line.get("error_class")) for line in _step_lines(caplog)
    ]


def _ms(seconds: float) -> int:
    return round(timedelta(seconds=seconds) / STEP_DURATION_UNIT)


def _json_output(caplog: pytest.LogCaptureFixture) -> str:
    formatter = JsonFormatter()
    return "\n".join(formatter.format(record) for record in caplog.records)


class _FixedUniform(random.Random):
    def __init__(self, value: float) -> None:
        super().__init__()
        self._value = value

    def uniform(self, a: float, b: float) -> float:
        return self._value


@dataclass
class Rig:
    outbox: SqlOutboxRepository
    contacts: SqlContactRepository
    messages: SqlMessageRepository
    state: SqlSenderStateRepository
    contact: Contact
    clock: FakeClock = field(default_factory=lambda: FakeClock(NOON))
    notifier: RecordingNotifier = field(default_factory=RecordingNotifier)

    def sender(
        self,
        transport: ScriptedTransport,
        *,
        profile: ScheduleProfile = PROFILE,
        switch: bool = True,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        timings: SenderTimings | None = None,
        limits: FakeLimits | None = None,
        schedule: ScheduleService | None = None,
        rng: random.Random | None = None,
    ) -> OutboxSender:
        return OutboxSender(
            self.outbox,
            self.contacts,
            self.state,
            SendLimiter(self.messages, limits if limits is not None else FakeLimits()),
            schedule
            if schedule is not None
            else ScheduleService(InMemoryScheduleProfiles(profile), MOSCOW),
            transport,
            self.notifier,
            SendGuard(_Switch(switch)),
            OUTBOX_NOTICES,
            clock=self.clock,
            timings=timings,
            sleep=sleep if sleep is not None else _RecordingSleep(transport),
            rng=rng if rng is not None else random.Random(SEED),
        )

    async def row(
        self, source: OutboxSource = OutboxSource.AUTOPILOT, *, due: datetime | None = None
    ) -> ScheduledMessage:
        return await self.outbox.enqueue(
            self.contact.id, TEXT, due if due is not None else self.clock(), source
        )

    async def stored(self, row_id: int) -> ScheduledMessage:
        row = await self.outbox.get(row_id)
        assert row is not None
        return row


@pytest.fixture
async def rig(
    outbox: SqlOutboxRepository,
    contacts: SqlContactRepository,
    messages: SqlMessageRepository,
    modes: SqlModeRepository,
    sender_state: SqlSenderStateRepository,
) -> Rig:
    contact = await stored_contact(
        contacts,
        modes,
        ContactStatus.ACTIVE,
        tg_id=FAKE_TELEGRAM_ID,
        username=USERNAME,
        display_name=DISPLAY_NAME,
    )
    return Rig(outbox, contacts, messages, sender_state, contact)


@pytest.mark.parametrize("source", list(OutboxSource))
async def test_the_exact_order_is_read_pause_typing_send(rig: Rig, source: OutboxSource) -> None:
    transport = ScriptedTransport(clock=rig.clock)
    sender = rig.sender(transport)
    row = await rig.row(source)

    assert await sender.send_next() is True

    assert transport.steps == ALL_STEPS
    assert transport.read_ids == [FAKE_TELEGRAM_ID]
    assert transport.calls == [(FAKE_TELEGRAM_ID, TEXT)]
    stored = await rig.stored(row.id)
    assert stored.state is OutboxState.SENT
    assert stored.error_class is None


async def test_the_pause_and_the_typing_use_the_computed_seconds(rig: Rig) -> None:
    transport = ScriptedTransport(clock=rig.clock)
    sleep = _RecordingSleep(transport)
    sender = rig.sender(transport, sleep=sleep)
    await rig.row()

    await sender.send_next()

    assert sleep.seconds == [random.Random(SEED).uniform(1, 3)]
    assert transport.typing_seconds == [typing_seconds(TEXT, PROFILE)]
    assert 2 < transport.typing_seconds[0] < 25


async def test_a_manual_row_gets_read_and_typing_and_skips_the_availability_check(
    rig: Rig,
) -> None:
    rig.clock.now = AT_NIGHT
    transport = ScriptedTransport(clock=rig.clock)
    sender = rig.sender(transport)
    row = await rig.row(OutboxSource.MANUAL_TEST)

    await sender.send_next()

    assert transport.steps == ALL_STEPS
    assert (await rig.stored(row.id)).state is OutboxState.SENT


@pytest.mark.parametrize(
    ("moment", "end"),
    [(AT_NIGHT, at_local(TUESDAY, 9)), (IN_BUSY, at_local(MONDAY, 14, 45))],
    ids=["night", "busy"],
)
async def test_an_autopilot_row_in_a_window_is_rescheduled_and_then_sent(
    rig: Rig, moment: datetime, end: datetime
) -> None:
    rig.clock.now = moment
    transport = ScriptedTransport(clock=rig.clock)
    sender = rig.sender(transport)
    row = await rig.row()

    assert await sender.send_next() is True

    stored = await rig.stored(row.id)
    assert transport.steps == []
    assert stored.state is OutboxState.SCHEDULED
    assert stored.error_class == ERROR_OUTSIDE_HOURS
    assert stored.due_at == row.due_at
    assert stored.claimed_at is None
    assert end + timedelta(minutes=1) <= stored.send_at <= end + timedelta(minutes=15)
    assert rig.notifier.texts == []

    rig.clock.now = stored.send_at - timedelta(seconds=1)
    assert await sender.send_next() is False
    assert transport.steps == []

    rig.clock.now = stored.send_at
    assert await sender.send_next() is True
    assert transport.steps == ALL_STEPS
    sent = await rig.stored(row.id)
    assert sent.state is OutboxState.SENT
    assert sent.error_class is None
    assert sent.due_at == row.due_at
    assert rig.notifier.texts == []


async def test_a_spread_that_lands_in_the_next_window_is_held_again_at_the_next_claim(
    rig: Rig,
) -> None:
    rig.clock.now = AT_NIGHT
    profile = profile_with(
        busy=[{"days": ["tue"], "from": "09:05", "to": "10:00"}],
        postponed_spread_minutes=[10, 10],
    )
    transport = ScriptedTransport(clock=rig.clock)
    sender = rig.sender(transport, profile=profile)
    row = await rig.row()

    await sender.send_next()
    first = await rig.stored(row.id)
    assert first.send_at == at_local(TUESDAY, 9, 10)

    rig.clock.now = first.send_at
    await sender.send_next()
    second = await rig.stored(row.id)
    assert second.send_at == at_local(TUESDAY, 10, 10)
    assert second.state is OutboxState.SCHEDULED
    assert transport.steps == []

    rig.clock.now = second.send_at
    await sender.send_next()
    assert (await rig.stored(row.id)).state is OutboxState.SENT


async def test_a_row_due_before_a_long_night_is_stale_when_it_comes_back(rig: Rig) -> None:
    rig.clock.now = at_local(TUESDAY, 2, 30)
    transport = ScriptedTransport(clock=rig.clock)
    sender = rig.sender(transport)
    row = await rig.row()

    await sender.send_next()
    held = await rig.stored(row.id)
    assert held.error_class == ERROR_OUTSIDE_HOURS
    assert rig.notifier.texts == []

    rig.clock.now = held.send_at
    await sender.send_next()

    stored = await rig.stored(row.id)
    assert stored.state is OutboxState.CANCELLED
    assert stored.cancel_reason == CANCEL_REASON_STALE
    assert transport.steps == []
    assert len(rig.notifier.texts) == 1


async def test_a_row_stale_when_claimed_is_cancelled_before_the_availability_check(
    rig: Rig,
) -> None:
    rig.clock.now = AT_NIGHT
    transport = ScriptedTransport(clock=rig.clock)
    sender = rig.sender(transport)
    row = await rig.row(due=AT_NIGHT - timedelta(hours=7))

    await sender.send_next()

    stored = await rig.stored(row.id)
    assert stored.state is OutboxState.CANCELLED
    assert stored.cancel_reason == CANCEL_REASON_STALE


async def test_the_guard_runs_before_the_availability_check(rig: Rig) -> None:
    rig.clock.now = AT_NIGHT
    transport = ScriptedTransport(clock=rig.clock)
    sender = rig.sender(transport, switch=False)
    row = await rig.row()

    await sender.send_next()

    stored = await rig.stored(row.id)
    assert stored.state is OutboxState.CANCELLED
    assert stored.cancel_reason == REFUSED_AUTOPILOT_DISABLED
    assert transport.steps == []


async def test_the_limiter_runs_before_the_availability_check(rig: Rig) -> None:
    rig.clock.now = AT_NIGHT
    transport = ScriptedTransport(clock=rig.clock)
    limits = FakeLimits(send_limit_minute=1, send_limit_contact_minute=1)
    sender = rig.sender(transport, limits=limits)
    await rig.row(OutboxSource.MANUAL_TEST)
    await sender.send_next()
    transport.steps.clear()
    limited = await rig.row()

    await sender.send_next()

    assert (await rig.stored(limited.id)).error_class == ERROR_RATE_LIMITED
    assert transport.steps == []


async def test_a_limited_row_is_not_read_or_typed(rig: Rig) -> None:
    transport = ScriptedTransport(clock=rig.clock)
    limits = FakeLimits(send_limit_minute=1, send_limit_contact_minute=1)
    sender = rig.sender(transport, limits=limits)
    first = await rig.row(OutboxSource.MANUAL_TEST)
    await sender.send_next()
    assert (await rig.stored(first.id)).state is OutboxState.SENT
    transport.steps.clear()
    second = await rig.row(OutboxSource.MANUAL_TEST)

    await sender.send_next()

    assert (await rig.stored(second.id)).error_class == ERROR_RATE_LIMITED
    assert transport.steps == []


async def test_a_contact_refused_by_the_guard_is_never_marked_as_read(rig: Rig) -> None:
    transport = ScriptedTransport(clock=rig.clock)
    sender = rig.sender(transport)
    row = await rig.row(OutboxSource.MANUAL_TEST)
    await rig.contacts.set_status(rig.contact.id, ContactStatus.PAUSED)

    await sender.send_next()

    assert (await rig.stored(row.id)).cancel_reason == REFUSED_CONTACT_NOT_ACTIVE
    assert transport.steps == []


async def test_a_status_change_during_the_pre_send_part_cancels_the_send(rig: Rig) -> None:
    transport = _PausesDuringTyping(rig.contacts, rig.contact.id)
    sender = rig.sender(transport)
    row = await rig.row()

    await sender.send_next()

    stored = await rig.stored(row.id)
    assert stored.state is OutboxState.CANCELLED
    assert stored.cancel_reason == REFUSED_CONTACT_NOT_ACTIVE
    assert transport.calls == []
    assert transport.steps == PRE_SEND_STEPS


@pytest.mark.parametrize("error", COSMETIC_ERRORS, ids=lambda error: type(error).__name__)
async def test_a_failed_read_mark_never_blocks_the_send(
    rig: Rig, error: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    transport = ScriptedTransport(clock=rig.clock, read_errors=[error])
    sender = rig.sender(transport)
    row = await rig.row()

    await sender.send_next()

    assert transport.steps == ALL_STEPS
    assert (await rig.stored(row.id)).state is OutboxState.SENT
    assert rig.notifier.texts == []
    assert type(error).__name__ in [getattr(record, "error", None) for record in caplog.records]
    assert_absent("cosmetic-detail-marker-zq", caplog.text)


@pytest.mark.parametrize("error", COSMETIC_ERRORS, ids=lambda error: type(error).__name__)
async def test_a_failed_typing_never_blocks_the_send(
    rig: Rig, error: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    transport = ScriptedTransport(clock=rig.clock, typing_errors=[error])
    sender = rig.sender(transport)
    row = await rig.row()

    await sender.send_next()

    assert transport.steps == ALL_STEPS
    assert (await rig.stored(row.id)).state is OutboxState.SENT
    assert rig.notifier.texts == []
    assert_absent("cosmetic-detail-marker-zq", caplog.text)


async def test_both_cosmetic_steps_failing_still_sends(rig: Rig) -> None:
    transport = ScriptedTransport(
        clock=rig.clock,
        read_errors=[SendUncertain("FakeNetworkError")],
        typing_errors=[SendUncertain("FakeNetworkError")],
    )
    sender = rig.sender(transport)
    row = await rig.row()

    await sender.send_next()

    assert (await rig.stored(row.id)).state is OutboxState.SENT


@pytest.mark.parametrize("where", ["read", "typing"], ids=["flood_in_read_mark", "flood_in_typing"])
async def test_a_flood_wait_before_the_send_pauses_the_sender_and_reschedules_without_sending(
    rig: Rig, where: str
) -> None:
    flood = SendFloodWait("FakeFloodWait", FLOOD_SHORT)
    transport = ScriptedTransport(
        clock=rig.clock,
        read_errors=[flood] if where == "read" else [],
        typing_errors=[flood] if where == "typing" else [],
    )
    sender = rig.sender(transport)
    row = await rig.row()
    until = rig.clock() + timedelta(seconds=FLOOD_SHORT + FLOOD_WAIT_MARGIN)

    await sender.send_next()

    stored = await rig.stored(row.id)
    state = await rig.state.get()
    assert transport.calls == []
    assert stored.state is OutboxState.SCHEDULED
    assert stored.send_at == until
    assert stored.due_at == row.due_at
    assert stored.error_class == "FakeFloodWait"
    assert state.paused_until == until
    assert rig.notifier.texts == []

    other = await rig.row(OutboxSource.MANUAL_TEST)
    assert await sender.send_next() is False
    assert (await rig.stored(other.id)).state is OutboxState.SCHEDULED


async def test_a_long_flood_wait_in_typing_notifies_once_like_on_a_send(rig: Rig) -> None:
    flood = SendFloodWait("FakeFloodWait", FLOOD_LONG)
    transport = ScriptedTransport(clock=rig.clock, typing_errors=[flood])
    sender = rig.sender(transport)
    await rig.row()

    await sender.send_next()

    assert len(rig.notifier.texts) == 1
    assert str(FLOOD_LONG) in rig.notifier.texts[0]


async def test_a_flooded_row_is_sent_after_the_pause(rig: Rig) -> None:
    flood = SendFloodWait("FakeFloodWait", FLOOD_SHORT)
    transport = ScriptedTransport(clock=rig.clock, typing_errors=[flood])
    sender = rig.sender(transport)
    row = await rig.row()

    await sender.send_next()
    rig.clock.advance(FLOOD_SHORT + FLOOD_WAIT_MARGIN)
    await sender.send_next()

    assert (await rig.stored(row.id)).state is OutboxState.SENT
    assert len(transport.calls) == 1


@pytest.mark.parametrize("where", ["read", "typing"], ids=["block_in_read_mark", "block_in_typing"])
async def test_an_account_error_before_the_send_stops_the_sender_and_fails_the_row(
    rig: Rig, where: str
) -> None:
    error = SendAccountBlocked("FakeKind")
    transport = ScriptedTransport(
        clock=rig.clock,
        read_errors=[error] if where == "read" else [],
        typing_errors=[error] if where == "typing" else [],
    )
    sender = rig.sender(transport)
    row = await rig.row()

    await sender.send_next()

    stored = await rig.stored(row.id)
    state = await rig.state.get()
    assert transport.calls == []
    assert stored.state is OutboxState.FAILED
    assert stored.error_class == "FakeKind"
    assert state.stopped is True
    assert state.stop_reason == "FakeKind"
    assert len(rig.notifier.texts) == 1
    assert "FakeKind" in rig.notifier.texts[0]
    contact = await rig.contacts.get_by_id(rig.contact.id)
    assert contact is not None
    assert contact.status is ContactStatus.ACTIVE


async def test_an_account_error_in_the_read_mark_skips_the_typing(rig: Rig) -> None:
    transport = ScriptedTransport(clock=rig.clock, read_errors=[SendAccountBlocked("FakeKind")])
    sender = rig.sender(transport)
    await rig.row()

    await sender.send_next()

    assert transport.steps == [MARK_READ_STEP]


async def test_a_hanging_typing_runs_into_the_deadline_and_the_row_goes_on_to_send(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    transport = _Hanging(hang_typing=True)
    sender = rig.sender(
        transport,
        profile=TIGHT_PROFILE,
        timings=SenderTimings(pre_send_margin=TIGHT_DEADLINE),
    )
    row = await rig.row()

    await sender.send_next()

    assert transport.interrupted == [SHOW_TYPING_STEP]
    assert transport.steps == ALL_STEPS
    assert (await rig.stored(row.id)).state is OutboxState.SENT
    assert "the pre-send part ran out of time" in caplog.text


async def test_a_hanging_read_mark_skips_the_rest_of_the_pre_send_part(rig: Rig) -> None:
    transport = _Hanging(hang_read=True)
    sender = rig.sender(
        transport,
        profile=TIGHT_PROFILE,
        timings=SenderTimings(pre_send_margin=TIGHT_DEADLINE),
    )
    row = await rig.row()

    await sender.send_next()

    assert transport.interrupted == [MARK_READ_STEP]
    assert transport.steps == [MARK_READ_STEP, SEND_TEXT_STEP]
    assert (await rig.stored(row.id)).state is OutboxState.SENT


async def test_a_hanging_pause_skips_the_typing(rig: Rig) -> None:
    transport = ScriptedTransport(clock=rig.clock)
    sender = rig.sender(
        transport,
        profile=TIGHT_PROFILE,
        timings=SenderTimings(pre_send_margin=TIGHT_DEADLINE),
        sleep=asyncio.sleep,
        rng=_FixedUniform(3600.0),
    )
    row = await rig.row()

    await sender.send_next()

    assert transport.steps == [MARK_READ_STEP, SEND_TEXT_STEP]
    assert (await rig.stored(row.id)).state is OutboxState.SENT


async def test_the_deadline_follows_the_profile_typing_maximum(rig: Rig) -> None:
    profile = profile_with(typing={"chars_per_second": 5, "min_seconds": 0, "max_seconds": 1})
    transport = _Hanging(hang_typing=True)
    sender = rig.sender(
        transport, profile=profile, timings=SenderTimings(pre_send_margin=NEARLY_MINUS_ONE)
    )
    row = await rig.row()
    loop = asyncio.get_running_loop()
    started = loop.time()

    await sender.send_next()

    assert loop.time() - started >= 1 + NEARLY_MINUS_ONE
    assert (await rig.stored(row.id)).state is OutboxState.SENT


@pytest.mark.parametrize(
    ("hang_read", "hang_typing"), [(True, False), (False, True)], ids=["in_read", "in_typing"]
)
async def test_a_cancellation_in_the_pre_send_part_leaves_the_row_in_sending(
    rig: Rig, hang_read: bool, hang_typing: bool
) -> None:
    transport = _Hanging(hang_read=hang_read, hang_typing=hang_typing)
    sender = rig.sender(transport)
    row = await rig.row()

    task = asyncio.create_task(sender.send_next())
    await transport.hanging.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    stored = await rig.stored(row.id)
    assert stored.state is OutboxState.SENDING
    assert transport.calls == []

    next_start = rig.sender(ScriptedTransport(clock=rig.clock))
    await next_start.sweep_interrupted()
    assert (await rig.stored(row.id)).error_class == ERROR_INTERRUPTED
    assert (await rig.stored(row.id)).state is OutboxState.FAILED


async def test_the_sender_stays_strictly_sequential_over_the_whole_sequence(rig: Rig) -> None:
    transport = _Concurrency()
    sender = rig.sender(
        transport,
        profile=profile_with(read_pause_seconds=[0, 0]),
        sleep=asyncio.sleep,
        timings=SenderTimings(poll_interval=0.001),
    )
    rows = [await rig.row(OutboxSource.MANUAL_TEST) for _ in range(3)]

    async def all_sent() -> bool:
        return len(transport.calls) == len(rows)

    task = asyncio.create_task(sender.poll_forever())
    try:
        await eventually(all_sent)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert transport.peak == 1
    assert transport.steps == [MARK_READ_STEP, SHOW_TYPING_STEP, SEND_TEXT_STEP] * len(rows)


async def test_one_claim_is_one_row_when_two_are_due(rig: Rig) -> None:
    transport = ScriptedTransport(clock=rig.clock)
    sender = rig.sender(transport)
    await rig.row()
    await rig.row()

    await sender.send_next()

    assert transport.steps == ALL_STEPS


async def test_a_profile_that_cannot_be_read_sends_nothing_and_leaves_the_row_in_sending(
    rig: Rig,
) -> None:
    class _Broken:
        async def get(self) -> ScheduleProfile | None:
            raise OSError("profile-detail-marker-zq")

        async def replace(self, profile: ScheduleProfile) -> ScheduleProfile:
            raise AssertionError

    transport = ScriptedTransport(clock=rig.clock)
    sender = rig.sender(transport, schedule=ScheduleService(_Broken(), MOSCOW))
    row = await rig.row(OutboxSource.MANUAL_TEST)

    with pytest.raises(OSError):
        await sender.send_next()

    assert transport.steps == []
    assert (await rig.stored(row.id)).state is OutboxState.SENDING


async def test_the_stored_profile_decides_through_the_real_repository(
    rig: Rig, schedule_profiles: SqlScheduleProfileRepository
) -> None:
    await schedule_profiles.replace(
        profile_with(busy=[{"days": ["tue"], "from": "11:00", "to": "13:00"}])
    )
    transport = ScriptedTransport(clock=rig.clock)
    sender = rig.sender(transport, schedule=ScheduleService(schedule_profiles, MOSCOW))
    row = await rig.row()

    await sender.send_next()

    stored = await rig.stored(row.id)
    assert stored.error_class == ERROR_OUTSIDE_HOURS
    assert stored.send_at > at_local(TUESDAY, 13)


async def test_nothing_stored_means_the_default_profile_through_the_real_repository(
    rig: Rig, schedule_profiles: SqlScheduleProfileRepository
) -> None:
    rig.clock.now = AT_NIGHT
    transport = ScriptedTransport(clock=rig.clock)
    sender = rig.sender(transport, schedule=ScheduleService(schedule_profiles, MOSCOW))
    row = await rig.row()

    await sender.send_next()

    assert (await rig.stored(row.id)).error_class == ERROR_OUTSIDE_HOURS


def test_the_stale_sending_age_exceeds_the_longest_pre_send_part_and_the_send_deadline() -> None:
    assert LONGEST_PRE_SEND == MAX_TYPING_SECONDS + PRE_SEND_DEADLINE_MARGIN
    assert SENDING_STALE_MARGIN > 0
    assert SENDING_STALE_AFTER >= LONGEST_PRE_SEND + SENDER_SEND_DEADLINE + SENDING_STALE_MARGIN
    assert SENDING_STALE_AFTER > LONGEST_PRE_SEND + SENDER_SEND_DEADLINE
    assert SenderTimings().stale_after == SENDING_STALE_AFTER
    assert SenderTimings().pre_send_margin == PRE_SEND_DEADLINE_MARGIN


def test_the_longest_read_pause_fits_inside_the_deadline_margin() -> None:
    assert MAX_READ_PAUSE_SECONDS < PRE_SEND_DEADLINE_MARGIN


async def test_no_path_leaks_a_text_a_name_or_a_telegram_id(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    scripts: list[ScriptedTransport] = [
        ScriptedTransport(clock=rig.clock),
        ScriptedTransport(clock=rig.clock, read_errors=[RuntimeError(TEXT)]),
        ScriptedTransport(clock=rig.clock, typing_errors=[SendUncertain("FakeNetworkError")]),
        ScriptedTransport(clock=rig.clock, typing_errors=[SendFloodWait("FakeFlood", FLOOD_LONG)]),
    ]
    for transport in scripts:
        await rig.state.resume()
        sender = rig.sender(transport)
        await rig.row()
        await sender.send_next()
    await rig.state.resume()
    rig.clock.now = AT_NIGHT
    held = rig.sender(ScriptedTransport(clock=rig.clock))
    await rig.row()
    await held.send_next()

    combined = caplog.text + "\n".join(rig.notifier.texts)
    for marker in (TEXT, USERNAME, DISPLAY_NAME, FAKE_TELEGRAM_ID):
        assert_absent(marker, combined)


@pytest.mark.parametrize("source", list(OutboxSource))
async def test_a_normal_send_logs_each_step_in_order_with_durations_from_the_clock(
    rig: Rig, source: OutboxSource, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    transport = _Timed(rig.clock)
    sender = rig.sender(transport, sleep=_TimedSleep(transport, rig.clock))
    row = await rig.row(source)
    pause = random.Random(SEED).uniform(1, 3)
    typing = typing_seconds(TEXT, PROFILE)
    ids = {"row_id": row.id, "contact_id": rig.contact.id}

    await sender.send_next()

    assert _step_lines(caplog) == [
        {"step": "mark_read", "outcome": "ok", "duration_ms": _ms(READ_TOOK), **ids},
        {
            "step": "pause",
            "outcome": "ok",
            "duration_ms": _ms(pause),
            "planned_seconds": pause,
            **ids,
        },
        {
            "step": "typing",
            "outcome": "ok",
            "duration_ms": _ms(typing),
            "planned_seconds": typing,
            **ids,
        },
        {"step": "send", "outcome": "ok", "duration_ms": _ms(SEND_TOOK), **ids},
    ]
    messages = [record.getMessage() for record in _sender_records(caplog)]
    assert messages == [*[STEP_LOG_MESSAGE] * len(LOG_STEPS), SENT_LOG_MESSAGE]
    step_records = [
        record for record in _sender_records(caplog) if record.getMessage() == STEP_LOG_MESSAGE
    ]
    assert {record.levelno for record in step_records} == {logging.INFO}
    assert all(isinstance(line["duration_ms"], int) for line in _step_lines(caplog))


@pytest.mark.parametrize("error", COSMETIC_ERRORS, ids=lambda error: type(error).__name__)
async def test_a_failed_read_mark_is_logged_as_failed_and_the_other_steps_still_run(
    rig: Rig, error: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    transport = _Timed(rig.clock, read_errors=[error])
    sender = rig.sender(transport, sleep=_TimedSleep(transport, rig.clock))
    row = await rig.row()

    await sender.send_next()

    assert _outcomes(caplog) == [
        ("mark_read", "failed", type(error).__name__),
        ("pause", "ok", None),
        ("typing", "ok", None),
        ("send", "ok", None),
    ]
    assert _step_lines(caplog)[0]["duration_ms"] == _ms(READ_TOOK)
    assert (await rig.stored(row.id)).state is OutboxState.SENT
    assert_absent(DETAIL_MARKER, _json_output(caplog))


@pytest.mark.parametrize("error", COSMETIC_ERRORS, ids=lambda error: type(error).__name__)
async def test_a_failed_typing_is_logged_as_failed_and_the_send_still_runs(
    rig: Rig, error: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    transport = _Timed(rig.clock, typing_errors=[error])
    sender = rig.sender(transport, sleep=_TimedSleep(transport, rig.clock))
    row = await rig.row()

    await sender.send_next()

    assert _outcomes(caplog) == [
        ("mark_read", "ok", None),
        ("pause", "ok", None),
        ("typing", "failed", type(error).__name__),
        ("send", "ok", None),
    ]
    assert (await rig.stored(row.id)).state is OutboxState.SENT
    assert_absent(DETAIL_MARKER, _json_output(caplog))


@pytest.mark.parametrize(
    ("hang", "expected"),
    [
        (
            "read",
            [
                ("mark_read", "timeout", None),
                ("pause", "skipped", None),
                ("typing", "skipped", None),
                ("send", "ok", None),
            ],
        ),
        (
            "pause",
            [
                ("mark_read", "ok", None),
                ("pause", "timeout", None),
                ("typing", "skipped", None),
                ("send", "ok", None),
            ],
        ),
        (
            "typing",
            [
                ("mark_read", "ok", None),
                ("pause", "ok", None),
                ("typing", "timeout", None),
                ("send", "ok", None),
            ],
        ),
    ],
    ids=["in_read", "in_pause", "in_typing"],
)
async def test_a_deadline_overrun_logs_timeout_for_that_step_and_skipped_for_the_rest(
    rig: Rig,
    hang: str,
    expected: list[tuple[str, str, None]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    transport: ScriptedTransport
    if hang == "pause":
        transport = ScriptedTransport(clock=rig.clock)
        sender = rig.sender(
            transport,
            profile=TIGHT_PROFILE,
            timings=SenderTimings(pre_send_margin=TIGHT_DEADLINE),
            sleep=asyncio.sleep,
            rng=_FixedUniform(3600.0),
        )
    else:
        transport = _Hanging(hang_read=hang == "read", hang_typing=hang == "typing")
        sender = rig.sender(
            transport,
            profile=TIGHT_PROFILE,
            timings=SenderTimings(pre_send_margin=TIGHT_DEADLINE),
        )
    row = await rig.row()

    await sender.send_next()

    assert _outcomes(caplog) == expected
    skipped = [line for line in _step_lines(caplog) if line["outcome"] == "skipped"]
    assert all(line["duration_ms"] == 0 for line in skipped)
    assert (await rig.stored(row.id)).state is OutboxState.SENT


@pytest.mark.parametrize(
    ("where", "error", "expected"),
    [
        (
            "typing",
            SendFloodWait("FakeFloodWait", FLOOD_SHORT),
            [
                ("mark_read", "ok", None),
                ("pause", "ok", None),
                ("typing", "failed", "SendFloodWait"),
                ("send", "skipped", None),
            ],
        ),
        (
            "read",
            SendFloodWait("FakeFloodWait", FLOOD_SHORT),
            [
                ("mark_read", "failed", "SendFloodWait"),
                ("pause", "skipped", None),
                ("typing", "skipped", None),
                ("send", "skipped", None),
            ],
        ),
        (
            "typing",
            SendAccountBlocked("FakeKind"),
            [
                ("mark_read", "ok", None),
                ("pause", "ok", None),
                ("typing", "failed", "SendAccountBlocked"),
                ("send", "skipped", None),
            ],
        ),
    ],
    ids=["flood_in_typing", "flood_in_read_mark", "block_in_typing"],
)
async def test_a_flood_wait_or_an_account_error_before_the_send_is_logged_as_failed(
    rig: Rig,
    where: str,
    error: Exception,
    expected: list[tuple[str, str, str | None]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    transport = _Timed(
        rig.clock,
        read_errors=[error] if where == "read" else [],
        typing_errors=[error] if where == "typing" else [],
    )
    sender = rig.sender(transport, sleep=_TimedSleep(transport, rig.clock))
    row = await rig.row()

    await sender.send_next()

    assert _outcomes(caplog) == expected
    assert transport.calls == []
    stored = await rig.stored(row.id)
    if isinstance(error, SendFloodWait):
        assert stored.state is OutboxState.SCHEDULED
        assert stored.error_class == "FakeFloodWait"
    else:
        assert stored.state is OutboxState.FAILED
        assert stored.error_class == "FakeKind"
    messages = [record.getMessage() for record in _sender_records(caplog)]
    assert messages[: len(LOG_STEPS)] == [STEP_LOG_MESSAGE] * len(LOG_STEPS)
    assert SENT_LOG_MESSAGE not in messages


async def test_a_failed_send_is_logged_as_failed_with_its_class(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    transport = _Timed(rig.clock, [SendUncertain("FakeNetworkError")])
    sender = rig.sender(transport, sleep=_TimedSleep(transport, rig.clock))
    row = await rig.row()

    await sender.send_next()

    assert _outcomes(caplog)[-1] == ("send", "failed", "SendUncertain")
    assert _step_lines(caplog)[-1]["duration_ms"] == _ms(SEND_TOOK)
    assert (await rig.stored(row.id)).state is OutboxState.FAILED
    assert SENT_LOG_MESSAGE not in [record.getMessage() for record in _sender_records(caplog)]


async def test_a_send_past_its_deadline_is_logged_as_timeout(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    transport = _HangingSend(clock=rig.clock)
    sender = rig.sender(transport, timings=SenderTimings(send_deadline=TIGHT_DEADLINE))
    row = await rig.row()

    await sender.send_next()

    assert _outcomes(caplog)[-1] == ("send", "timeout", None)
    assert (await rig.stored(row.id)).state is OutboxState.FAILED


async def test_a_refusal_after_the_pre_send_part_logs_the_send_as_skipped(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    transport = _PausesDuringTyping(rig.contacts, rig.contact.id)
    sender = rig.sender(transport)
    row = await rig.row()

    await sender.send_next()

    assert _outcomes(caplog) == [
        ("mark_read", "ok", None),
        ("pause", "ok", None),
        ("typing", "ok", None),
        ("send", "skipped", None),
    ]
    assert (await rig.stored(row.id)).state is OutboxState.CANCELLED


async def test_a_row_that_never_reaches_the_pre_send_part_logs_no_step_lines(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    rig.clock.now = AT_NIGHT
    sender = rig.sender(ScriptedTransport(clock=rig.clock))
    await rig.row()

    await sender.send_next()

    assert _step_lines(caplog) == []


async def test_the_step_lines_carry_no_text_name_or_telegram_id(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    scripts: list[ScriptedTransport] = [
        _Timed(rig.clock),
        _Timed(rig.clock, read_errors=[RuntimeError(TEXT)]),
        _Timed(rig.clock, typing_errors=[RuntimeError(DETAIL_MARKER)]),
        _Timed(rig.clock, [RuntimeError(TEXT)]),
        _Timed(rig.clock, typing_errors=[SendFloodWait("FakeFlood", FLOOD_SHORT)]),
    ]
    for source in OutboxSource:
        for transport in scripts:
            await rig.state.resume()
            sender = rig.sender(transport)
            await rig.row(source)
            await sender.send_next()

    output = _json_output(caplog)
    assert STEP_LOG_MESSAGE in output
    for marker in (TEXT, USERNAME, DISPLAY_NAME, DETAIL_MARKER, FAKE_TELEGRAM_ID):
        assert_absent(marker, output)
