from __future__ import annotations

import logging
import random
from datetime import timedelta

import pytest

from cyrano.domain.enums import (
    ContactStatus,
    EarlyReadReason,
    MessageDirection,
)
from cyrano.domain.errors import StoredScheduleInvalid
from cyrano.domain.models import Contact, IncomingMessage
from cyrano.domain.schedule import ScheduleProfile
from cyrano.logging import JsonFormatter
from cyrano.services.autopilot import AutopilotScheduler
from cyrano.services.contact_events import InMemoryContactEvents
from cyrano.services.early_read import EarlyReadScheduler
from cyrano.services.intake import IntakeResult, IntakeService
from tests.privacy import FAKE_TELEGRAM_ID, assert_absent
from tests.services.autopilot_fakes import RecordingQueue
from tests.services.early_read_rig import (
    BREAKS,
    CLASS_PROFILE,
    CONTACT_NAME_ZQ,
    INCOMING_TEXT_ZQ,
    MEETING_TEXT,
    NIGHT_NOW,
    NIGHT_PROFILE,
    NOW,
    SEED,
    Break,
    EarlyReadRig,
    free_profile_with,
)


@pytest.mark.parametrize(("reason", "apply"), BREAKS, ids=[reason.value for reason, _ in BREAKS])
async def test_one_failing_switch_or_state_registers_nothing(
    reason: EarlyReadReason, apply: Break
) -> None:
    rig = EarlyReadRig()
    contact = await rig.contact()
    message = await rig.ongoing_message(contact)
    await apply(rig)

    assert await rig.scheduler.on_incoming(contact.id, message.id) is False

    assert rig.queue.pending() == []


@pytest.mark.parametrize(
    "status",
    [ContactStatus.NEW, ContactStatus.PENDING, ContactStatus.PAUSED, ContactStatus.IGNORED],
)
async def test_a_contact_that_is_not_active_registers_nothing(status: ContactStatus) -> None:
    rig = EarlyReadRig()
    contact = await rig.contact(status)
    message = await rig.ongoing_message(contact)

    assert await rig.scheduler.on_incoming(contact.id, message.id) is False

    assert rig.queue.pending() == []


async def test_an_unknown_contact_registers_nothing() -> None:
    rig = EarlyReadRig()

    assert await rig.scheduler.on_incoming(404, 1) is False

    assert rig.queue.pending() == []


async def test_a_conversation_that_is_not_ongoing_registers_nothing() -> None:
    rig = EarlyReadRig()
    contact = await rig.contact()
    message = await rig.fresh_message(contact)

    assert await rig.scheduler.on_incoming(contact.id, message.id) is False

    assert rig.queue.pending() == []


async def test_a_conversation_that_went_quiet_long_ago_registers_nothing() -> None:
    rig = EarlyReadRig()
    contact = await rig.contact()
    window = timedelta(minutes=rig.profiles.profile.reaction.ongoing_window_minutes)  # type: ignore[union-attr]
    await rig.say(contact, window + timedelta(minutes=1))
    message = await rig.say(contact)

    assert await rig.scheduler.on_incoming(contact.id, message.id) is False


async def test_a_moment_inside_the_night_window_registers_nothing() -> None:
    rig = EarlyReadRig(NIGHT_PROFILE, NIGHT_NOW)
    contact = await rig.contact()
    message = await rig.ongoing_message(contact)

    assert await rig.scheduler.on_incoming(contact.id, message.id) is False

    assert rig.queue.pending() == []


async def test_a_moment_inside_a_class_window_registers_nothing() -> None:
    rig = EarlyReadRig(CLASS_PROFILE)
    contact = await rig.contact()
    message = await rig.ongoing_message(contact)

    assert await rig.scheduler.on_incoming(contact.id, message.id) is False

    assert rig.queue.pending() == []


async def test_the_same_moment_outside_the_windows_registers() -> None:
    rig = EarlyReadRig(NIGHT_PROFILE)
    contact = await rig.contact()
    message = await rig.ongoing_message(contact)

    assert await rig.scheduler.on_incoming(contact.id, message.id) is True


async def test_every_condition_true_registers_exactly_one_request_inside_the_range() -> None:
    rig = EarlyReadRig()
    contact = await rig.contact()
    message = await rig.ongoing_message(contact)

    assert await rig.scheduler.on_incoming(contact.id, message.id) is True

    [request] = rig.queue.pending()
    assert request.contact_id == contact.id
    assert NOW <= request.due_at <= NOW + timedelta(seconds=1)


async def test_the_delay_is_drawn_uniformly_from_the_range_with_the_injected_generator() -> None:
    rig = EarlyReadRig(free_profile_with(early_read_seconds=[2, 6]))
    contact = await rig.contact()
    message = await rig.ongoing_message(contact)

    await rig.scheduler.on_incoming(contact.id, message.id)

    [request] = rig.queue.pending()
    expected = random.Random(SEED).uniform(2, 6)
    assert request.due_at == NOW + timedelta(seconds=expected)
    assert expected != round(expected)


async def test_a_zero_range_means_strictly_immediate() -> None:
    rig = EarlyReadRig(free_profile_with(early_read_seconds=[0, 0]))
    contact = await rig.contact()
    message = await rig.ongoing_message(contact)

    await rig.scheduler.on_incoming(contact.id, message.id)

    [request] = rig.queue.pending()
    assert request.due_at == NOW


async def test_a_second_message_of_the_same_burst_does_not_add_or_move_a_request() -> None:
    rig = EarlyReadRig(free_profile_with(early_read_seconds=[3, 9]))
    contact = await rig.contact()
    first = await rig.ongoing_message(contact)
    await rig.scheduler.on_incoming(contact.id, first.id)
    [before] = rig.queue.pending()
    rig.clock.advance(1)
    second = await rig.say(contact)

    assert await rig.scheduler.on_incoming(contact.id, second.id) is False

    assert rig.queue.pending() == [before]


async def test_contacts_get_their_own_requests() -> None:
    rig = EarlyReadRig()
    first = await rig.contact()
    second = await rig.contact()
    await rig.scheduler.on_incoming(first.id, (await rig.ongoing_message(first)).id)
    await rig.scheduler.on_incoming(second.id, (await rig.ongoing_message(second)).id)

    assert [request.contact_id for request in rig.queue.pending()] == [first.id, second.id]


class _BrokenContacts:
    def __init__(self, rig: EarlyReadRig) -> None:
        self._rig = rig

    async def get_by_id(self, contact_id: int) -> Contact | None:
        raise ConnectionError(f"{INCOMING_TEXT_ZQ} {CONTACT_NAME_ZQ}")


async def test_a_repository_error_is_caught_logged_by_class_and_registers_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = EarlyReadRig()
    contact = await rig.contact()
    message = await rig.ongoing_message(contact)
    rig.gate._contacts = _BrokenContacts(rig)  # type: ignore[assignment]
    caplog.set_level(logging.INFO)

    assert await rig.scheduler.on_incoming(contact.id, message.id) is False

    [record] = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert record.__dict__["error"] == "ConnectionError"
    assert record.__dict__["contact_id"] == contact.id
    output = "\n".join(JsonFormatter().format(r) for r in caplog.records)
    assert INCOMING_TEXT_ZQ not in output
    assert CONTACT_NAME_ZQ not in output
    assert_absent(FAKE_TELEGRAM_ID, output)
    assert rig.queue.pending() == []


class _BrokenProfiles:
    async def get(self) -> ScheduleProfile | None:
        raise StoredScheduleInvalid

    async def replace(self, profile: ScheduleProfile) -> ScheduleProfile:
        return profile


async def test_an_unreadable_profile_is_caught_and_registers_nothing() -> None:
    rig = EarlyReadRig()
    contact = await rig.contact()
    message = await rig.ongoing_message(contact)
    rig.profiles.get = _BrokenProfiles().get  # type: ignore[method-assign]

    assert await rig.scheduler.on_incoming(contact.id, message.id) is False

    assert rig.queue.pending() == []


async def test_a_message_that_is_not_in_the_history_is_caught_and_registers_nothing() -> None:
    rig = EarlyReadRig()
    contact = await rig.contact()
    await rig.ongoing_message(contact)

    assert await rig.scheduler.on_incoming(contact.id, 9999) is False

    assert rig.queue.pending() == []


async def test_the_gate_names_the_reason_of_each_refusal() -> None:
    rig = EarlyReadRig()
    contact = await rig.contact(ContactStatus.PAUSED)

    verdict = await rig.gate.check(contact.id)

    assert verdict.reason is EarlyReadReason.CONTACT_NOT_ACTIVE
    assert verdict.contact is None
    assert verdict.allowed is False


async def test_the_gate_hands_back_the_contact_when_everything_holds() -> None:
    rig = EarlyReadRig()
    contact = await rig.contact()

    verdict = await rig.gate.check(contact.id)

    assert verdict.allowed is True
    assert verdict.contact is not None
    assert verdict.contact.id == contact.id


class _OrderedEarlyRead(EarlyReadScheduler):
    calls: list[str]

    async def on_incoming(self, contact_id: int, message_id: int) -> bool:
        self.calls.append("early read")
        return await super().on_incoming(contact_id, message_id)


class _OrderedAutopilot(AutopilotScheduler):
    calls: list[str]

    async def on_incoming(self, contact_id: int, message_id: int) -> bool:
        self.calls.append("autopilot")
        return await super().on_incoming(contact_id, message_id)


def _ordered_early_read(rig: EarlyReadRig, calls: list[str]) -> _OrderedEarlyRead:
    scheduler = _OrderedEarlyRead(
        rig.gate,
        rig.planner,
        rig.schedule,
        rig.queue,
        rng=random.Random(SEED),
        clock=rig.clock,
    )
    scheduler.calls = calls
    return scheduler


async def test_the_intake_registers_an_early_read_for_a_lively_active_contact() -> None:
    rig = EarlyReadRig()
    contact = await rig.contact()
    await rig.say(contact, timedelta(minutes=1), direction=MessageDirection.OUTGOING)

    result = await rig.intake().handle(rig.incoming(contact))

    assert result.contact_id == contact.id
    assert [request.contact_id for request in rig.queue.pending()] == [contact.id]


async def test_the_intake_calls_the_scheduler_after_the_gate_and_the_autopilot() -> None:
    rig = EarlyReadRig()
    contact = await rig.contact()
    await rig.say(contact, timedelta(minutes=1), direction=MessageDirection.OUTGOING)
    calls: list[str] = []
    autopilot = _OrderedAutopilot(
        rig.contacts,
        rig.autopilot,
        RecordingQueue(),
        rig.switch,
        rig.planner,
        debounce_seconds=12,
        ongoing_debounce_seconds=4,
        clock=rig.clock,
    )
    autopilot.calls = calls
    intake = IntakeService(
        rig.contacts,
        rig.messages,
        InMemoryContactEvents(),
        hand_over=None,
        scheduler=autopilot,
        early_read=_ordered_early_read(rig, calls),
        clock=rig.clock,
    )

    await intake.handle(rig.incoming(contact))

    assert calls == ["autopilot", "early read"]


async def test_a_hand_over_prevents_the_request() -> None:
    rig = EarlyReadRig()
    contact = await rig.contact()
    await rig.say(contact, timedelta(minutes=1), direction=MessageDirection.OUTGOING)

    await rig.intake(hand_over=True).handle(rig.incoming(contact, MEETING_TEXT))

    assert (await rig.contacts.get_by_id(contact.id)).status is ContactStatus.PAUSED  # type: ignore[union-attr]
    assert rig.queue.pending() == []


async def test_intake_does_not_ask_for_a_contact_that_is_not_active() -> None:
    rig = EarlyReadRig()
    contact = await rig.contact(ContactStatus.PENDING)
    calls: list[str] = []

    await rig.intake(early_read=_ordered_early_read(rig, calls)).handle(rig.incoming(contact))

    assert calls == []
    assert rig.queue.pending() == []


class _FailingAutopilotState:
    async def get(self) -> object:
        raise ConnectionError(INCOMING_TEXT_ZQ)


async def test_a_scheduler_failure_does_not_break_the_intake_and_its_result_is_unchanged() -> None:
    rig = EarlyReadRig()
    contact = await rig.contact()
    await rig.say(contact, timedelta(minutes=1), direction=MessageDirection.OUTGOING)
    plain = IntakeService(rig.contacts, rig.messages, InMemoryContactEvents(), clock=rig.clock)
    first = await plain.handle(rig.incoming(contact))
    rig.gate._autopilot_state = _FailingAutopilotState()  # type: ignore[assignment]

    second = await rig.intake().handle(rig.incoming(contact))

    assert second == IntakeResult(
        contact_id=contact.id, message_id=first.message_id + 1, contact_created=False
    )
    assert rig.queue.pending() == []


async def test_the_intake_result_is_the_same_with_and_without_the_scheduler() -> None:
    rig = EarlyReadRig()
    contact = await rig.contact()
    plain = IntakeService(rig.contacts, rig.messages, InMemoryContactEvents(), clock=rig.clock)

    first = await plain.handle(rig.incoming(contact))
    second = await rig.intake().handle(rig.incoming(contact))

    assert first == IntakeResult(contact_id=contact.id, message_id=1, contact_created=False)
    assert second == IntakeResult(contact_id=contact.id, message_id=2, contact_created=False)


async def test_a_stranger_is_registered_as_a_new_contact_and_gets_no_request() -> None:
    rig = EarlyReadRig()
    message = IncomingMessage(
        tg_id=FAKE_TELEGRAM_ID, tg_message_id=1, text=INCOMING_TEXT_ZQ, sent_at=rig.clock()
    )

    result = await rig.intake().handle(message)

    assert result.contact_created is True
    assert rig.queue.pending() == []


async def test_a_registration_logs_internal_ids_only(caplog: pytest.LogCaptureFixture) -> None:
    rig = EarlyReadRig()
    contact = await rig.contact()
    message = await rig.ongoing_message(contact)
    caplog.set_level(logging.INFO)

    await rig.scheduler.on_incoming(contact.id, message.id)

    output = "\n".join(JsonFormatter().format(record) for record in caplog.records)
    assert "early read requested" in output
    assert INCOMING_TEXT_ZQ not in output
    assert CONTACT_NAME_ZQ not in output
    assert_absent(FAKE_TELEGRAM_ID + 1, output)
    assert_absent(FAKE_TELEGRAM_ID, output)
