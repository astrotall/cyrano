from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from cyrano.bot.notifier import OUTBOX_NOTICES
from cyrano.db.repositories import SqlSenderStateRepository
from cyrano.domain.enums import ContactStatus, MessageDirection, OutboxSource, OutboxState
from cyrano.domain.models import Contact, Message
from cyrano.domain.outbox import (
    CANCEL_REASON_STALE,
    CANCEL_REASON_SUPERSEDED,
    ERROR_OUTSIDE_HOURS,
    ERROR_OUTSIDE_WINDOW,
    ERROR_RATE_LIMITED,
    REFUSED_AUTOPILOT_DISABLED,
    REFUSED_CONTACT_NOT_ACTIVE,
    REFUSED_PROACTIVE_DISABLED,
    ScheduledMessage,
)
from cyrano.domain.schedule import ScheduleProfile
from cyrano.services.humanization import ScheduleService, in_proactive_window
from cyrano.services.outbox_sender import OutboxSender
from cyrano.services.send_guard import SendGuard, check
from cyrano.services.send_limiter import SendLimiter
from tests.services.autopilot_rig import AutopilotRig
from tests.services.outbox_fakes import (
    ALWAYS_FREE_PROFILE,
    MARK_READ_STEP,
    SEND_TEXT_STEP,
    SHOW_TYPING_STEP,
    FakeLimits,
    InMemoryScheduleProfiles,
    RecordingNotifier,
    ScriptedTransport,
)
from tests.services.proactive_data import contact as plain_contact
from tests.services.proactive_rig import FOLLOW_UP_TEXT_ZQ, OUR_TEXT_ZQ

SEED = 7
PAUSE_STEP = "pause"
WEDNESDAY_LATE = datetime(2026, 11, 4, 23, 0, tzinfo=UTC)
CLASS_PROFILE = ScheduleProfile.model_validate(
    {
        "version": 1,
        "timezone": "UTC",
        "night": {
            "start": ["00:00", "00:00"],
            "end_weekday": ["00:01", "00:01"],
            "end_weekend": ["00:01", "00:01"],
        },
        "busy": [{"days": ["wed"], "from": "11:30", "to": "13:00"}],
        "read_pause_seconds": [0, 0],
    }
)


@dataclass(frozen=True)
class _Switch:
    autopilot_enabled: bool = True
    proactive_enabled: bool = True


class _WritingDuringTyping(ScriptedTransport):
    def __init__(self, rig: AutopilotRig, contact: Contact) -> None:
        super().__init__(clock=rig.clock)
        self._rig = rig
        self._contact = contact

    async def show_typing(self, tg_id: int, seconds: float) -> None:
        await super().show_typing(tg_id, seconds)
        await self._rig.say(self._contact, ago=0)


class _Sender:
    def __init__(
        self,
        rig: AutopilotRig,
        *,
        profile: ScheduleProfile = ALWAYS_FREE_PROFILE,
        limits: FakeLimits | None = None,
        switch: _Switch | None = None,
    ) -> None:
        self.rig = rig
        self.profile = profile
        self.limits = limits if limits is not None else FakeLimits()
        self.switch = switch if switch is not None else _Switch()

    def build(self, transport: ScriptedTransport) -> OutboxSender:
        async def pause(seconds: float) -> None:
            transport.steps.append(PAUSE_STEP)

        return OutboxSender(
            self.rig.outbox,
            self.rig.contacts,
            self.rig.messages,
            SqlSenderStateRepository(self.rig.session_factory),
            SendLimiter(self.rig.messages, self.limits),
            ScheduleService(InMemoryScheduleProfiles(self.profile), "UTC"),
            transport,
            RecordingNotifier(),
            SendGuard(self.switch),
            OUTBOX_NOTICES,
            clock=self.rig.clock,
            sleep=pause,
            rng=random.Random(SEED),
        )


async def _silent(rig: AutopilotRig) -> tuple[Contact, Message]:
    contact = await rig.contact()
    await rig.say(contact, ago=8 * 3600)
    last = await rig.say(
        contact, ago=7 * 3600, text=OUR_TEXT_ZQ, direction=MessageDirection.OUTGOING
    )
    return contact, last


async def _row(
    rig: AutopilotRig,
    contact: Contact,
    trigger: Message | None,
    *,
    source: OutboxSource = OutboxSource.PROACTIVE,
    parts: int = 1,
    due: datetime | None = None,
) -> list[ScheduledMessage]:
    return await rig.outbox.enqueue_sequence(
        contact.id,
        [FOLLOW_UP_TEXT_ZQ] * parts,
        due if due is not None else rig.clock(),
        source,
        None if trigger is None else trigger.id,
    )


async def _stored(rig: AutopilotRig, row: ScheduledMessage) -> ScheduledMessage:
    stored = await rig.outbox.get(row.id)
    assert stored is not None
    return stored


@pytest.mark.parametrize(
    ("autopilot", "proactive", "reason"),
    [
        (True, True, None),
        (True, False, REFUSED_PROACTIVE_DISABLED),
        (False, True, REFUSED_AUTOPILOT_DISABLED),
        (False, False, REFUSED_AUTOPILOT_DISABLED),
    ],
)
def test_the_guard_needs_both_switches_for_a_proactive_row(
    autopilot: bool, proactive: bool, reason: str | None
) -> None:
    decision = check(plain_contact(), OutboxSource.PROACTIVE, _Switch(autopilot, proactive))

    assert decision.allowed is (reason is None)
    if reason is not None:
        assert decision.reason == reason


@pytest.mark.parametrize("status", [s for s in ContactStatus if s is not ContactStatus.ACTIVE])
def test_the_guard_refuses_a_proactive_row_for_a_contact_that_is_not_active(
    status: ContactStatus,
) -> None:
    decision = check(plain_contact(status), OutboxSource.PROACTIVE, _Switch())

    assert decision.reason == REFUSED_CONTACT_NOT_ACTIVE


def test_the_proactive_switch_never_touches_the_other_sources() -> None:
    off = _Switch(proactive_enabled=False)

    assert check(plain_contact(), OutboxSource.AUTOPILOT, off).allowed is True
    assert check(plain_contact(), OutboxSource.MANUAL_TEST, off).allowed is True


async def test_a_proactive_row_goes_out_in_the_human_order(rig: AutopilotRig) -> None:
    contact, last = await _silent(rig)
    [row] = await _row(rig, contact, last)
    transport = ScriptedTransport(clock=rig.clock)

    assert await _Sender(rig).build(transport).send_next() is True

    assert transport.steps == [MARK_READ_STEP, PAUSE_STEP, SHOW_TYPING_STEP, SEND_TEXT_STEP]
    assert transport.calls == [(contact.tg_id, FOLLOW_UP_TEXT_ZQ)]
    assert (await _stored(rig, row)).state is OutboxState.SENT


@pytest.mark.parametrize("direction", [MessageDirection.INCOMING, MessageDirection.OUTGOING])
async def test_a_newer_message_in_either_direction_supersedes_a_proactive_row(
    rig: AutopilotRig, direction: MessageDirection
) -> None:
    contact, last = await _silent(rig)
    [row] = await _row(rig, contact, last)
    await rig.say(contact, ago=10, direction=direction)
    transport = ScriptedTransport(clock=rig.clock)

    await _Sender(rig).build(transport).send_next()

    stored = await _stored(rig, row)
    assert stored.state is OutboxState.CANCELLED
    assert stored.cancel_reason == CANCEL_REASON_SUPERSEDED
    assert transport.steps == []


async def test_an_older_message_stored_late_does_not_supersede(rig: AutopilotRig) -> None:
    contact, last = await _silent(rig)
    [row] = await _row(rig, contact, last)
    await rig.say(contact, ago=9 * 3600)
    transport = ScriptedTransport(clock=rig.clock)

    await _Sender(rig).build(transport).send_next()

    assert (await _stored(rig, row)).state is OutboxState.SENT


async def test_a_message_after_the_start_does_not_cancel_the_proactive_row(
    rig: AutopilotRig,
) -> None:
    contact, last = await _silent(rig)
    [row] = await _row(rig, contact, last)
    transport = _WritingDuringTyping(rig, contact)

    await _Sender(rig).build(transport).send_next()

    assert (await _stored(rig, row)).state is OutboxState.SENT
    assert transport.calls == [(contact.tg_id, FOLLOW_UP_TEXT_ZQ)]


async def test_a_later_part_of_a_started_proactive_reply_is_never_superseded(
    rig: AutopilotRig,
) -> None:
    contact, last = await _silent(rig)
    first, second = await _row(rig, contact, last, parts=2)
    sender = _Sender(rig).build(ScriptedTransport(clock=rig.clock))
    await sender.send_next()
    await rig.say(contact, ago=0)
    rig.clock.advance(60)

    await sender.send_next()

    assert (await _stored(rig, first)).state is OutboxState.SENT
    assert (await _stored(rig, second)).state is OutboxState.SENT


async def test_outside_the_window_a_due_proactive_row_is_held_for_the_next_window(
    rig: AutopilotRig,
) -> None:
    rig.clock.now = WEDNESDAY_LATE
    contact, last = await _silent(rig)
    [row] = await _row(rig, contact, last)
    transport = ScriptedTransport(clock=rig.clock)

    assert await _Sender(rig).build(transport).send_next() is True

    stored = await _stored(rig, row)
    assert stored.state is OutboxState.SCHEDULED
    assert stored.error_class == ERROR_OUTSIDE_WINDOW
    assert stored.due_at == row.due_at
    next_start = datetime(2026, 11, 5, 10, 0, tzinfo=UTC)
    assert next_start + timedelta(minutes=1) <= stored.send_at <= next_start + timedelta(minutes=15)
    assert in_proactive_window(stored.send_at, ALWAYS_FREE_PROFILE)
    assert transport.steps == []


async def test_the_window_does_not_hold_an_autopilot_or_a_manual_test_row(
    rig: AutopilotRig,
) -> None:
    rig.clock.now = WEDNESDAY_LATE
    contact, last = await _silent(rig)
    [manual] = await _row(rig, contact, last, source=OutboxSource.MANUAL_TEST)
    incoming = await rig.say(contact, ago=5)
    [autopilot] = await _row(
        rig,
        contact,
        incoming,
        source=OutboxSource.AUTOPILOT,
        due=rig.clock() - timedelta(seconds=1),
    )
    sender = _Sender(rig).build(ScriptedTransport(clock=rig.clock))

    await sender.send_next()
    await sender.send_next()

    assert (await _stored(rig, manual)).state is OutboxState.SENT
    assert (await _stored(rig, autopilot)).state is OutboxState.SENT


async def test_inside_a_class_window_a_proactive_row_is_held_like_an_autopilot_row(
    rig: AutopilotRig,
) -> None:
    contact, last = await _silent(rig)
    [row] = await _row(rig, contact, last)
    transport = ScriptedTransport(clock=rig.clock)

    await _Sender(rig, profile=CLASS_PROFILE).build(transport).send_next()

    stored = await _stored(rig, row)
    assert stored.state is OutboxState.SCHEDULED
    assert stored.error_class == ERROR_OUTSIDE_HOURS
    assert stored.send_at > datetime(2026, 11, 4, 13, 0, tzinfo=UTC)
    assert transport.steps == []


async def test_a_reached_limit_holds_a_proactive_row(rig: AutopilotRig) -> None:
    contact, last = await _silent(rig)
    [row] = await _row(rig, contact, last)
    await rig.say(contact, ago=5, direction=MessageDirection.OUTGOING)
    await rig.say(contact, ago=4, direction=MessageDirection.OUTGOING)
    transport = ScriptedTransport(clock=rig.clock)

    await _Sender(rig, limits=FakeLimits(send_limit_contact_minute=2)).build(transport).send_next()

    stored = await _stored(rig, row)
    assert stored.state is OutboxState.SCHEDULED
    assert stored.error_class == ERROR_RATE_LIMITED
    assert transport.steps == []


async def test_a_stale_proactive_row_is_cancelled(rig: AutopilotRig) -> None:
    contact, last = await _silent(rig)
    [row] = await _row(rig, contact, last, due=rig.clock() - timedelta(hours=6, seconds=1))

    await _Sender(rig).build(ScriptedTransport(clock=rig.clock)).send_next()

    stored = await _stored(rig, row)
    assert stored.state is OutboxState.CANCELLED
    assert stored.cancel_reason == CANCEL_REASON_STALE


async def test_the_sender_refuses_a_proactive_row_once_the_switch_is_off(
    rig: AutopilotRig,
) -> None:
    contact, last = await _silent(rig)
    [row] = await _row(rig, contact, last)
    transport = ScriptedTransport(clock=rig.clock)

    await _Sender(rig, switch=_Switch(proactive_enabled=False)).build(transport).send_next()

    stored = await _stored(rig, row)
    assert stored.cancel_reason == REFUSED_PROACTIVE_DISABLED
    assert transport.steps == []


async def test_a_manual_test_row_is_untouched_by_the_proactive_rules(rig: AutopilotRig) -> None:
    rig.clock.now = WEDNESDAY_LATE
    contact, last = await _silent(rig)
    [row] = await _row(rig, contact, last, source=OutboxSource.MANUAL_TEST)
    await rig.say(contact, ago=1)
    transport = ScriptedTransport(clock=rig.clock)

    await _Sender(rig, switch=_Switch(False, False)).build(transport).send_next()

    assert (await _stored(rig, row)).state is OutboxState.SENT
    assert transport.calls == [(contact.tg_id, FOLLOW_UP_TEXT_ZQ)]
