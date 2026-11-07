from __future__ import annotations

import logging
import random
from datetime import timedelta

import pytest

from cyrano.bot.notifier import OUTBOX_NOTICES
from cyrano.db.repositories import SqlSenderStateRepository
from cyrano.domain.enums import (
    ContactStatus,
    MessageDirection,
    OutboxSource,
    OutboxState,
    PauseReason,
)
from cyrano.domain.errors import SendUncertain
from cyrano.domain.models import Contact, IncomingMessage, Message, OutgoingMessage
from cyrano.domain.outbox import (
    CANCEL_REASON_MANUAL_MESSAGE,
    CANCEL_REASON_PREDECESSOR,
    CANCEL_REASON_STALE,
    CANCEL_REASON_STOP_TRIGGER,
    CANCEL_REASON_SUPERSEDED,
    ERROR_OUTSIDE_HOURS,
    ERROR_RATE_LIMITED,
    ERROR_UNCERTAIN,
    REFUSED_CONTACT_NOT_ACTIVE,
    ROW_STALE_AFTER_SECONDS,
    ScheduledMessage,
    SentMessage,
)
from cyrano.domain.schedule import ScheduleProfile
from cyrano.services.contact_events import InMemoryContactEvents
from cyrano.services.humanization import ScheduleService, typing_seconds
from cyrano.services.intake import IntakeService
from cyrano.services.manual_intervention import ManualMessageDetector
from cyrano.services.outbox_sender import STEP_LOG_MESSAGE, OutboxSender
from cyrano.services.send_guard import SendGuard
from cyrano.services.send_limiter import SendLimiter
from tests.privacy import assert_absent
from tests.services.autopilot_rig import (
    CONTACT_NAME_ZQ,
    CONTACT_USERNAME_ZQ,
    NOW,
    AutopilotRig,
)
from tests.services.outbox_fakes import (
    ALWAYS_FREE_PROFILE,
    FREE_TIMEZONE,
    FakeLimits,
    InMemoryScheduleProfiles,
    RecordingManualPauseNotifier,
    RecordingNotifier,
    ScriptedTransport,
    free_schedule,
)

SEED = 11
READ = "mark_read"
PAUSE = "pause"
TYPING = "show_typing"
SEND = "send_text"
SENDER_LOGGER = "cyrano.services.outbox_sender"
TWO_PARTS = ("invented first part", "the invented second part is a bit longer")
THREE_PARTS = ("invented one", "invented two two", "invented three three three")
TRIGGER_AGO = 30
SEND_TAKES = 2.0
WIDE_SPLIT_PAUSE = (10, 20)
MANUAL_TG_MESSAGE_ID = 991_000_222
HAND_OVER_TG_MESSAGE_ID = 991_000_333
MEETING_TEXT = "давай встретимся завтра вечером"
BERLIN = "Europe/Berlin"
BUSY_WEDNESDAY_NOON = ScheduleProfile.model_validate(
    {
        "version": 1,
        "timezone": FREE_TIMEZONE,
        "busy": [{"days": ["wed"], "from": "12:00", "to": "13:00"}],
        "read_pause_seconds": [0, 0],
    }
)


class _FailingSecondSend(ScriptedTransport):
    async def send_text(self, tg_id: int, text: str) -> SentMessage:
        sent = await super().send_text(tg_id, text)
        if len(self.calls) == 2:
            raise SendUncertain("FakeError")
        return sent


class _SlowSendTransport(ScriptedTransport):
    def __init__(self, rig: AutopilotRig) -> None:
        super().__init__(clock=rig.clock)
        self._rig = rig

    async def send_text(self, tg_id: int, text: str) -> SentMessage:
        self._rig.clock.advance(SEND_TAKES)
        return await super().send_text(tg_id, text)


def _sender(
    rig: AutopilotRig,
    transport: ScriptedTransport,
    *,
    limits: FakeLimits | None = None,
    schedule: ScheduleService | None = None,
    notifier: RecordingNotifier | None = None,
) -> OutboxSender:
    async def pause(seconds: float) -> None:
        transport.steps.append(PAUSE)
        rig.clock.advance(seconds)

    return OutboxSender(
        rig.outbox,
        rig.contacts,
        rig.messages,
        SqlSenderStateRepository(rig.session_factory),
        SendLimiter(rig.messages, limits if limits is not None else FakeLimits()),
        schedule if schedule is not None else free_schedule(),
        transport,
        notifier if notifier is not None else RecordingNotifier(),
        SendGuard(rig.switch),
        OUTBOX_NOTICES,
        clock=rig.clock,
        sleep=pause,
        rng=random.Random(SEED),
    )


def _schedule(profile: ScheduleProfile) -> ScheduleService:
    return ScheduleService(InMemoryScheduleProfiles(profile), FREE_TIMEZONE)


async def _chain(
    rig: AutopilotRig, contact: Contact, texts: tuple[str, ...], trigger: Message
) -> list[ScheduledMessage]:
    return await rig.outbox.enqueue_sequence(
        contact.id, texts, rig.clock(), OutboxSource.AUTOPILOT, trigger.id
    )


async def _stored(rig: AutopilotRig, row: ScheduledMessage) -> ScheduledMessage:
    stored = await rig.outbox.get(row.id)
    assert stored is not None
    return stored


async def _state(rig: AutopilotRig, row: ScheduledMessage) -> tuple[OutboxState, str | None]:
    stored = await _stored(rig, row)
    return stored.state, stored.cancel_reason


async def _reach(rig: AutopilotRig, row: ScheduledMessage) -> None:
    stored = await _stored(rig, row)
    if stored.send_at > rig.clock():
        rig.clock.now = stored.send_at


async def _ready(
    rig: AutopilotRig, texts: tuple[str, ...]
) -> tuple[Contact, list[ScheduledMessage]]:
    contact = await rig.contact()
    trigger = await rig.say(contact, ago=TRIGGER_AGO)
    return contact, await _chain(rig, contact, texts, trigger)


def _steps_of(caplog: pytest.LogCaptureFixture, row: ScheduledMessage) -> list[tuple[str, str]]:
    return [
        (record.__dict__["step"], record.__dict__["outcome"])
        for record in caplog.records
        if record.name == SENDER_LOGGER
        and record.getMessage() == STEP_LOG_MESSAGE
        and record.__dict__["row_id"] == row.id
    ]


async def test_a_two_part_reply_reads_once_then_types_and_sends_each_part_in_order(
    rig: AutopilotRig, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=SENDER_LOGGER)
    contact, (first, second) = await _ready(rig, TWO_PARTS)
    transport = ScriptedTransport(clock=rig.clock)
    sender = _sender(rig, transport)

    assert await sender.send_next() is True
    assert transport.steps == [READ, PAUSE, TYPING, SEND]
    assert await sender.send_next() is False
    assert await _state(rig, second) == (OutboxState.SCHEDULED, None)

    await _reach(rig, second)
    assert await sender.send_next() is True

    assert transport.steps == [READ, PAUSE, TYPING, SEND, TYPING, SEND]
    assert transport.calls == [(contact.tg_id, TWO_PARTS[0]), (contact.tg_id, TWO_PARTS[1])]
    assert transport.read_ids == [contact.tg_id]
    profile = ALWAYS_FREE_PROFILE
    assert transport.typing_seconds == [typing_seconds(text, profile) for text in TWO_PARTS]
    assert await _state(rig, first) == (OutboxState.SENT, None)
    assert await _state(rig, second) == (OutboxState.SENT, None)
    assert _steps_of(caplog, first) == [
        ("mark_read", "ok"),
        ("pause", "ok"),
        ("typing", "ok"),
        ("send", "ok"),
    ]
    assert _steps_of(caplog, second) == [("typing", "ok"), ("send", "ok")]
    outgoing = [
        message.text
        for message in await rig.messages.list_last(contact.id, 10)
        if message.direction is MessageDirection.OUTGOING
    ]
    assert outgoing == list(TWO_PARTS)


async def test_a_three_part_reply_goes_out_part_after_part(rig: AutopilotRig) -> None:
    contact, chain = await _ready(rig, THREE_PARTS)
    transport = ScriptedTransport(clock=rig.clock)
    sender = _sender(rig, transport)

    for row in chain:
        await _reach(rig, row)
        assert await sender.send_next() is True

    assert transport.calls == [(contact.tg_id, text) for text in THREE_PARTS]
    assert transport.steps.count(READ) == 1
    assert transport.steps.count(PAUSE) == 1
    assert transport.steps.count(TYPING) == len(THREE_PARTS)
    for row in chain:
        assert await _state(rig, row) == (OutboxState.SENT, None)


async def test_the_split_pause_is_drawn_from_the_profile_and_counted_from_the_actual_send(
    rig: AutopilotRig,
) -> None:
    profile = ALWAYS_FREE_PROFILE.model_copy(update={"split_pause_seconds": WIDE_SPLIT_PAUSE})
    _, (first, second) = await _ready(rig, TWO_PARTS)
    sender = _sender(rig, _SlowSendTransport(rig), schedule=_schedule(profile))
    expected_rng = random.Random(SEED)
    expected_rng.uniform(*profile.read_pause_seconds)
    expected_pause = expected_rng.uniform(*WIDE_SPLIT_PAUSE)
    before = rig.clock()

    await sender.send_next()

    sent_at = before + timedelta(seconds=SEND_TAKES)
    assert rig.clock() == sent_at
    follower = await _stored(rig, second)
    waited = (follower.send_at - sent_at).total_seconds()
    assert WIDE_SPLIT_PAUSE[0] <= waited <= WIDE_SPLIT_PAUSE[1]
    assert waited == pytest.approx(expected_pause, abs=1e-3)
    assert follower.due_at == first.due_at
    rig.clock.now = follower.send_at - timedelta(milliseconds=10)
    assert await sender.send_next() is False


async def test_a_failing_first_part_cancels_the_rest_as_predecessor(rig: AutopilotRig) -> None:
    _, (first, second, third) = await _ready(rig, THREE_PARTS)
    transport = ScriptedTransport([SendUncertain("FakeError")], clock=rig.clock)
    notifier = RecordingNotifier()
    sender = _sender(rig, transport, notifier=notifier)

    await sender.send_next()
    rig.clock.advance(3600)
    await sender.send_next()

    assert (await _stored(rig, first)).error_class == ERROR_UNCERTAIN
    assert await _state(rig, first) == (OutboxState.FAILED, None)
    assert await _state(rig, second) == (OutboxState.CANCELLED, CANCEL_REASON_PREDECESSOR)
    assert await _state(rig, third) == (OutboxState.CANCELLED, CANCEL_REASON_PREDECESSOR)
    assert len(transport.calls) == 1
    assert len(notifier.texts) == 1


async def test_a_failing_middle_part_keeps_the_first_sent_and_cancels_the_last(
    rig: AutopilotRig,
) -> None:
    _, (first, second, third) = await _ready(rig, THREE_PARTS)
    transport = _FailingSecondSend(clock=rig.clock)
    sender = _sender(rig, transport)
    await sender.send_next()
    await _reach(rig, second)

    await sender.send_next()
    rig.clock.advance(3600)
    await sender.send_next()

    assert await _state(rig, first) == (OutboxState.SENT, None)
    assert await _state(rig, second) == (OutboxState.FAILED, None)
    assert await _state(rig, third) == (OutboxState.CANCELLED, CANCEL_REASON_PREDECESSOR)
    assert len(transport.calls) == 2


async def test_a_limit_on_the_second_part_holds_it_while_the_first_stays_sent(
    rig: AutopilotRig,
) -> None:
    _, (first, second) = await _ready(rig, TWO_PARTS)
    transport = ScriptedTransport(clock=rig.clock)
    sender = _sender(rig, transport, limits=FakeLimits(send_limit_contact_minute=1))
    started = rig.clock()
    await sender.send_next()
    await _reach(rig, second)

    await sender.send_next()

    held = await _stored(rig, second)
    assert (held.state, held.error_class) == (OutboxState.SCHEDULED, ERROR_RATE_LIMITED)
    assert held.send_at == started + timedelta(seconds=60)
    assert await _state(rig, first) == (OutboxState.SENT, None)
    assert len(transport.calls) == 1

    await _reach(rig, second)
    await sender.send_next()

    assert await _state(rig, second) == (OutboxState.SENT, None)
    assert transport.steps[-2:] == [TYPING, SEND]
    assert transport.steps.count(READ) == 1


async def test_a_follower_held_by_a_limit_is_still_sent_after_newer_incoming_messages(
    rig: AutopilotRig,
) -> None:
    contact, (first, second) = await _ready(rig, TWO_PARTS)
    transport = ScriptedTransport(clock=rig.clock)
    sender = _sender(rig, transport, limits=FakeLimits(send_limit_contact_minute=1))
    await sender.send_next()
    await _reach(rig, second)
    await sender.send_next()
    await rig.say(contact, ago=0)
    await _reach(rig, second)
    await rig.say(contact, ago=0)

    await sender.send_next()

    assert await _state(rig, first) == (OutboxState.SENT, None)
    assert await _state(rig, second) == (OutboxState.SENT, None)
    assert transport.calls == [(contact.tg_id, TWO_PARTS[0]), (contact.tg_id, TWO_PARTS[1])]


async def test_a_stop_trigger_after_the_first_part_cancels_the_waiting_parts(
    rig: AutopilotRig,
) -> None:
    contact, (first, second, third) = await _ready(rig, THREE_PARTS)
    transport = ScriptedTransport(clock=rig.clock)
    sender = _sender(rig, transport)
    await sender.send_next()
    intake = IntakeService(
        rig.contacts, rig.messages, InMemoryContactEvents(), hand_over=rig.hand_over()
    )
    await intake.handle(
        IncomingMessage(
            tg_id=contact.tg_id,
            tg_message_id=HAND_OVER_TG_MESSAGE_ID,
            text=MEETING_TEXT,
            sent_at=rig.clock(),
        )
    )
    await _reach(rig, second)

    await sender.send_next()
    await sender.send_next()

    reloaded = await rig.reload(contact)
    assert (reloaded.status, reloaded.pause_reason) == (
        ContactStatus.PAUSED,
        PauseReason.STOP_TRIGGER,
    )
    assert await _state(rig, first) == (OutboxState.SENT, None)
    for part in (second, third):
        assert await _state(rig, part) == (OutboxState.CANCELLED, CANCEL_REASON_STOP_TRIGGER)
    assert len(transport.calls) == 1


async def test_a_newer_incoming_message_before_the_first_part_cancels_the_whole_reply(
    rig: AutopilotRig,
) -> None:
    contact, (first, second) = await _ready(rig, TWO_PARTS)
    await rig.say(contact, ago=TRIGGER_AGO // 2)
    transport = ScriptedTransport(clock=rig.clock)
    sender = _sender(rig, transport)

    await sender.send_next()
    await sender.send_next()

    assert await _state(rig, first) == (OutboxState.CANCELLED, CANCEL_REASON_SUPERSEDED)
    assert await _state(rig, second) == (OutboxState.CANCELLED, CANCEL_REASON_PREDECESSOR)
    assert transport.steps == []


async def test_a_newer_incoming_message_after_the_first_part_never_cancels_the_rest(
    rig: AutopilotRig,
) -> None:
    contact, (first, second, third) = await _ready(rig, THREE_PARTS)
    transport = ScriptedTransport(clock=rig.clock)
    sender = _sender(rig, transport)
    await sender.send_next()
    await rig.say(contact, ago=0)
    await _reach(rig, second)
    await sender.send_next()
    await rig.say(contact, ago=0)
    await _reach(rig, third)

    await sender.send_next()

    for part in (first, second, third):
        assert await _state(rig, part) == (OutboxState.SENT, None)
    assert transport.steps == [READ, PAUSE, TYPING, SEND, TYPING, SEND, TYPING, SEND]


async def test_a_newer_outgoing_message_does_not_supersede_a_follower(rig: AutopilotRig) -> None:
    contact, (_, second) = await _ready(rig, TWO_PARTS)
    transport = ScriptedTransport(clock=rig.clock)
    sender = _sender(rig, transport)
    await sender.send_next()
    await rig.say(contact, ago=0, direction=MessageDirection.OUTGOING)
    await _reach(rig, second)

    await sender.send_next()

    assert await _state(rig, second) == (OutboxState.SENT, None)
    assert len(transport.calls) == 2


async def test_an_incoming_message_older_than_the_trigger_does_not_supersede_a_follower(
    rig: AutopilotRig,
) -> None:
    contact, (_, second) = await _ready(rig, TWO_PARTS)
    transport = ScriptedTransport(clock=rig.clock)
    sender = _sender(rig, transport)
    await sender.send_next()
    await rig.say(contact, ago=TRIGGER_AGO * 2)
    await _reach(rig, second)

    await sender.send_next()

    assert await _state(rig, second) == (OutboxState.SENT, None)


async def test_a_manual_owner_message_cancels_every_waiting_part(rig: AutopilotRig) -> None:
    contact, (first, second, third) = await _ready(rig, THREE_PARTS)
    transport = ScriptedTransport(clock=rig.clock)
    sender = _sender(rig, transport)
    await sender.send_next()

    async def no_wait(seconds: float) -> None:
        return None

    detector = ManualMessageDetector(
        rig.contacts,
        rig.messages,
        rig.outbox,
        RecordingManualPauseNotifier(),
        sleep=no_wait,
        clock=rig.clock,
    )
    await detector.handle(
        OutgoingMessage(
            tg_id=contact.tg_id,
            tg_message_id=MANUAL_TG_MESSAGE_ID,
            text="invented manual text",
            sent_at=rig.clock(),
        )
    )
    rig.clock.advance(3600)
    await sender.send_next()

    reloaded = await rig.reload(contact)
    assert (reloaded.status, reloaded.pause_reason) == (
        ContactStatus.PAUSED,
        PauseReason.MANUAL_MESSAGE,
    )
    assert await _state(rig, first) == (OutboxState.SENT, None)
    assert await _state(rig, second) == (OutboxState.CANCELLED, CANCEL_REASON_MANUAL_MESSAGE)
    assert await _state(rig, third) == (OutboxState.CANCELLED, CANCEL_REASON_MANUAL_MESSAGE)
    assert len(transport.calls) == 1


async def test_a_follower_due_inside_a_busy_window_is_sent_while_a_first_part_is_held(
    rig: AutopilotRig,
) -> None:
    rig.clock.now = NOW - timedelta(seconds=20)
    contact, (first, second) = await _ready(rig, TWO_PARTS)
    other = await rig.contact()
    transport = ScriptedTransport(clock=rig.clock)
    sender = _sender(rig, transport, schedule=_schedule(BUSY_WEDNESDAY_NOON))
    await sender.send_next()
    assert await _state(rig, first) == (OutboxState.SENT, None)
    rig.clock.now = NOW + timedelta(seconds=10)
    other_trigger = await rig.say(other, ago=5)
    other_first, _ = await _chain(rig, other, TWO_PARTS, other_trigger)

    await sender.send_next()
    await sender.send_next()

    assert await _state(rig, second) == (OutboxState.SENT, None)
    held = await _stored(rig, other_first)
    assert (held.state, held.error_class) == (OutboxState.SCHEDULED, ERROR_OUTSIDE_HOURS)
    assert transport.calls == [(contact.tg_id, TWO_PARTS[0]), (contact.tg_id, TWO_PARTS[1])]


async def test_a_follower_is_still_refused_by_the_guard_at_send_time(rig: AutopilotRig) -> None:
    contact, (_, second) = await _ready(rig, TWO_PARTS)
    transport = ScriptedTransport(clock=rig.clock)
    sender = _sender(rig, transport)
    await sender.send_next()
    await rig.contacts.set_status(contact.id, ContactStatus.PAUSED)
    await _reach(rig, second)

    await sender.send_next()

    assert await _state(rig, second) == (OutboxState.CANCELLED, REFUSED_CONTACT_NOT_ACTIVE)
    assert len(transport.calls) == 1


async def test_a_follower_past_the_stale_age_is_cancelled_as_stale(rig: AutopilotRig) -> None:
    _, (_, second) = await _ready(rig, TWO_PARTS)
    transport = ScriptedTransport(clock=rig.clock)
    sender = _sender(rig, transport)
    await sender.send_next()
    rig.clock.advance(ROW_STALE_AFTER_SECONDS + 60)

    await sender.send_next()

    assert await _state(rig, second) == (OutboxState.CANCELLED, CANCEL_REASON_STALE)
    assert len(transport.calls) == 1


async def test_a_limit_notice_gives_the_numbers_and_the_local_next_slot_in_the_profile_zone(
    rig: AutopilotRig, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    profile = ALWAYS_FREE_PROFILE.model_copy(update={"timezone": BERLIN})
    contact, (_, second) = await _ready(rig, TWO_PARTS)
    notifier = RecordingNotifier()
    sender = _sender(
        rig,
        ScriptedTransport(clock=rig.clock),
        limits=FakeLimits(send_limit_contact_hour=1),
        schedule=_schedule(profile),
        notifier=notifier,
    )
    await sender.send_next()
    await _reach(rig, second)

    await sender.send_next()

    [notice] = notifier.texts
    assert f"для контакта #{contact.id}" in notice
    assert "настройка SEND_LIMIT_CONTACT_HOUR)" in notice
    assert "Отправлено за окно: 1, лимит: 1." in notice
    assert f"в 04.11 14:00 ({BERLIN})" in notice
    assert "Ничего не потеряно" in notice
    assert "6 часов" in notice
    haystack = "\n".join([notice, caplog.text, *(str(r.__dict__) for r in caplog.records)])
    for marker in (*TWO_PARTS, CONTACT_NAME_ZQ, CONTACT_USERNAME_ZQ, contact.tg_id):
        assert_absent(marker, haystack)


async def test_a_global_limit_notice_carries_no_contact_id(rig: AutopilotRig) -> None:
    contact, (_, second) = await _ready(rig, TWO_PARTS)
    notifier = RecordingNotifier()
    sender = _sender(
        rig,
        ScriptedTransport(clock=rig.clock),
        limits=FakeLimits(send_limit_day=1),
        notifier=notifier,
    )
    await sender.send_next()
    await _reach(rig, second)

    await sender.send_next()

    [notice] = notifier.texts
    assert f"#{contact.id}" not in notice
    assert "настройка SEND_LIMIT_DAY)" in notice
    assert f"в 05.11 12:00 ({FREE_TIMEZONE})" in notice
