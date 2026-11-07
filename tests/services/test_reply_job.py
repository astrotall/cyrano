from __future__ import annotations

import random
from datetime import datetime, timedelta

import pytest
from sqlalchemy import text

from cyrano.config import MAX_AUTOPILOT_MAX_BURST_WAIT_SECONDS
from cyrano.domain.autopilot import (
    JOB_MAX_TRIES,
    PROVIDER_COOLDOWN_SECONDS,
    REPLY_STALE_AFTER_SECONDS,
    RETRY_DELAYS_SECONDS,
    TRUNCATION_MAX_TOKENS,
    ReplyJobResult,
)
from cyrano.domain.enums import (
    AutopilotPauseReason,
    AutopilotStopReason,
    ContactStatus,
    HandOverOutcome,
    MessageDirection,
    OutboxSource,
    OutboxState,
    PauseReason,
    ReplyJobOutcome,
    ReplyJobReason,
    TriggerClass,
    TriggerSource,
)
from cyrano.domain.errors import (
    LLMAuthError,
    LLMInvalidResponseError,
    LLMRateLimitError,
    LLMRequestError,
    LLMUnavailableError,
    PlanNotPossible,
    RetryJob,
)
from cyrano.domain.models import Contact, Message
from cyrano.domain.outbox import ScheduledMessage
from cyrano.domain.schedule import SendPlan
from cyrano.services.humanization import HumanizationPlanner
from cyrano.services.reply_job import burst_texts
from tests.privacy import assert_absent
from tests.services.autopilot_rig import (
    BASE_MAX_TOKENS,
    CONTACT_NAME_ZQ,
    CONTACT_USERNAME_ZQ,
    FIRST_TG_ID,
    INCOMING_TEXT_ZQ,
    INVALID_REPLY,
    NEEDS_OWNER_REPLY,
    NOW,
    PERSONA_TEXT_ZQ,
    REPLY_TEXT_ZQ,
    AutopilotRig,
    SequenceLLMClient,
    json_logs,
    llm_result,
)
from tests.services.outbox_fakes import RecordingNotifier

MEETING_TEXT = "давай встретимся завтра вечером"
LONG_MAX_WAIT = MAX_AUTOPILOT_MAX_BURST_WAIT_SECONDS
OTHER_REPLY_ZQ = "other-reply-autopilot-marker-zq"
TRANSIENT_ERRORS = (
    LLMRateLimitError("rate limited", status_code=429),
    LLMUnavailableError("unavailable", status_code=503),
    LLMInvalidResponseError("invalid"),
)


def _queued(result: ReplyJobResult) -> int:
    assert (result.outcome, result.reason) == (ReplyJobOutcome.QUEUED, ReplyJobReason.QUEUED)
    assert result.row_id is not None
    return result.row_id


def _skipped(result: ReplyJobResult, reason: ReplyJobReason) -> None:
    assert (result.outcome, result.reason) == (ReplyJobOutcome.SKIPPED, reason)


async def _rows(rig: AutopilotRig, contact: Contact) -> list[ScheduledMessage]:
    return [row for row in await rig.outbox.list_rows(None, 100) if row.contact_id == contact.id]


async def _ready(rig: AutopilotRig) -> tuple[Contact, Message]:
    contact = await rig.contact()
    return contact, await rig.say(contact)


async def test_a_normal_run_writes_one_autopilot_row_at_the_planned_time(rig: AutopilotRig) -> None:
    contact, trigger = await _ready(rig)
    client = SequenceLLMClient(llm_result())

    result = await rig.runner(client).run(contact.id, trigger.id)

    row_id = _queued(result)
    expected = await rig.planner().plan(contact.id, trigger.id, rig.clock())
    [row] = await _rows(rig, contact)
    assert row.id == row_id
    assert (row.source, row.state, row.trigger_message_id) == (
        OutboxSource.AUTOPILOT,
        OutboxState.SCHEDULED,
        trigger.id,
    )
    assert row.text == REPLY_TEXT_ZQ
    assert row.send_at == expected.send_at
    assert row.due_at == row.send_at
    assert result.detail == expected.reason.value
    assert client.max_tokens == [None]


async def test_a_second_run_for_the_same_trigger_writes_nothing(rig: AutopilotRig) -> None:
    contact, trigger = await _ready(rig)
    client = SequenceLLMClient(llm_result())
    runner = rig.runner(client)
    await runner.run(contact.id, trigger.id)

    again = await runner.run(contact.id, trigger.id)

    _skipped(again, ReplyJobReason.ROW_EXISTS)
    assert len(await _rows(rig, contact)) == 1
    assert client.calls == 1


async def test_a_burst_of_three_produces_one_row_from_the_job_of_the_newest(
    rig: AutopilotRig,
) -> None:
    contact = await rig.contact()
    first = await rig.say(contact, ago=30, text="first invented line")
    second = await rig.say(contact, ago=20, text="second invented line")
    third = await rig.say(contact, ago=10, text="third invented line")
    client = SequenceLLMClient(llm_result())
    runner = rig.runner(client, max_burst_wait_seconds=LONG_MAX_WAIT)

    results = [await runner.run(contact.id, message.id) for message in (first, second, third)]

    _skipped(results[0], ReplyJobReason.SUPERSEDED)
    _skipped(results[1], ReplyJobReason.SUPERSEDED)
    _queued(results[2])
    [row] = await _rows(rig, contact)
    assert row.trigger_message_id == third.id
    assert client.calls == 1


async def test_the_switch_off_skips_before_reading_anything(rig: AutopilotRig) -> None:
    contact, trigger = await _ready(rig)
    rig.switch.autopilot_enabled = False
    client = SequenceLLMClient(llm_result())

    _skipped(await rig.runner(client).run(contact.id, trigger.id), ReplyJobReason.SWITCH_OFF)

    assert client.calls == 0
    assert await _rows(rig, contact) == []


async def test_a_stopped_autopilot_skips(rig: AutopilotRig) -> None:
    contact, trigger = await _ready(rig)
    await rig.state.stop(AutopilotStopReason.PROVIDER_AUTH)
    client = SequenceLLMClient(llm_result())

    result = await rig.runner(client).run(contact.id, trigger.id)

    _skipped(result, ReplyJobReason.AUTOPILOT_STOPPED)
    assert client.calls == 0


async def test_a_cooldown_in_the_future_skips_and_one_in_the_past_does_not(
    rig: AutopilotRig,
) -> None:
    contact, trigger = await _ready(rig)
    now = rig.clock()
    await rig.state.pause_until(
        now + timedelta(seconds=1), AutopilotPauseReason.PROVIDER_UNAVAILABLE, now=now
    )
    client = SequenceLLMClient(llm_result())
    runner = rig.runner(client)

    _skipped(await runner.run(contact.id, trigger.id), ReplyJobReason.AUTOPILOT_PAUSED)
    rig.clock.advance(1)
    _queued(await runner.run(contact.id, trigger.id))


async def test_a_missing_contact_skips(rig: AutopilotRig) -> None:
    contact, trigger = await _ready(rig)

    result = await rig.runner(SequenceLLMClient(llm_result())).run(contact.id + 1000, trigger.id)

    _skipped(result, ReplyJobReason.CONTACT_MISSING)


@pytest.mark.parametrize(
    "status",
    [ContactStatus.NEW, ContactStatus.PENDING, ContactStatus.PAUSED, ContactStatus.IGNORED],
)
async def test_a_contact_that_is_not_active_skips(rig: AutopilotRig, status: ContactStatus) -> None:
    contact = await rig.contact(status)
    trigger = await rig.say(contact)
    client = SequenceLLMClient(llm_result())

    result = await rig.runner(client).run(contact.id, trigger.id)

    _skipped(result, ReplyJobReason.CONTACT_NOT_ACTIVE)
    assert client.calls == 0


async def test_a_missing_trigger_or_one_of_another_contact_skips(rig: AutopilotRig) -> None:
    contact, trigger = await _ready(rig)
    other = await rig.contact()
    foreign = await rig.say(other)
    runner = rig.runner(SequenceLLMClient(llm_result()))

    _skipped(await runner.run(contact.id, trigger.id + 1000), ReplyJobReason.TRIGGER_MISSING)
    _skipped(await runner.run(contact.id, foreign.id), ReplyJobReason.TRIGGER_MISSING)


async def test_an_outgoing_trigger_skips(rig: AutopilotRig) -> None:
    contact = await rig.contact()
    outgoing = await rig.say(contact, direction=MessageDirection.OUTGOING)

    result = await rig.runner(SequenceLLMClient(llm_result())).run(contact.id, outgoing.id)

    _skipped(result, ReplyJobReason.TRIGGER_NOT_INCOMING)


async def test_a_trigger_at_or_before_reply_after_skips_and_one_after_it_is_answered(
    rig: AutopilotRig,
) -> None:
    contact = await rig.contact(ContactStatus.PAUSED)
    before = await rig.say(contact, ago=120)
    at = await rig.say(contact, ago=60)
    await rig.contacts.set_status(contact.id, ContactStatus.ACTIVE, reply_after=at.created_at)
    runner = rig.runner(SequenceLLMClient(llm_result()))

    _skipped(await runner.run(contact.id, before.id), ReplyJobReason.TRIGGER_BEFORE_RESUME)
    _skipped(await runner.run(contact.id, at.id), ReplyJobReason.TRIGGER_BEFORE_RESUME)
    after = await rig.say(contact, ago=10)
    _queued(await runner.run(contact.id, after.id))


async def test_a_trigger_older_than_the_stale_age_skips_and_exactly_that_age_is_answered(
    rig: AutopilotRig,
) -> None:
    contact = await rig.contact()
    trigger = await rig.say(contact, ago=REPLY_STALE_AFTER_SECONDS)
    runner = rig.runner(SequenceLLMClient(llm_result()))
    rig.clock.advance(1)

    _skipped(await runner.run(contact.id, trigger.id), ReplyJobReason.TRIGGER_STALE)
    rig.clock.advance(-1)
    _queued(await runner.run(contact.id, trigger.id))


async def test_a_newer_incoming_message_supersedes_the_trigger(rig: AutopilotRig) -> None:
    contact, trigger = await _ready(rig)
    await rig.say(contact, ago=5)
    client = SequenceLLMClient(llm_result())

    result = await rig.runner(client, max_burst_wait_seconds=LONG_MAX_WAIT).run(
        contact.id, trigger.id
    )

    _skipped(result, ReplyJobReason.SUPERSEDED)
    assert client.calls == 0


async def test_an_outgoing_message_after_the_trigger_means_it_was_answered(
    rig: AutopilotRig,
) -> None:
    contact, trigger = await _ready(rig)
    await rig.say(contact, ago=5, direction=MessageDirection.OUTGOING)
    client = SequenceLLMClient(llm_result())

    result = await rig.runner(client).run(contact.id, trigger.id)

    _skipped(result, ReplyJobReason.ANSWERED)
    assert client.calls == 0


async def test_an_older_message_stored_after_the_trigger_does_not_supersede_it(
    rig: AutopilotRig,
) -> None:
    contact = await rig.contact()
    trigger = await rig.say(contact, ago=30, text="newer by time")
    caught_up = await rig.say(contact, ago=90, text="older by time")
    client = SequenceLLMClient(llm_result())

    result = await rig.runner(client).run(contact.id, trigger.id)

    assert caught_up.id > trigger.id
    _queued(result)
    history = await rig.messages.list_last(contact.id, 10)
    assert [message.text for message in history] == ["older by time", "newer by time"]
    assert burst_texts(history, None) == ["older by time", "newer by time"]


async def test_the_job_of_a_caught_up_older_message_is_superseded_by_the_newer_time(
    rig: AutopilotRig,
) -> None:
    contact = await rig.contact()
    await rig.say(contact, ago=30)
    caught_up = await rig.say(contact, ago=90)
    client = SequenceLLMClient(llm_result())

    result = await rig.runner(client, max_burst_wait_seconds=LONG_MAX_WAIT).run(
        contact.id, caught_up.id
    )

    _skipped(result, ReplyJobReason.SUPERSEDED)
    assert client.calls == 0


async def test_an_outgoing_message_stored_last_but_between_two_incoming_is_ordered_by_time(
    rig: AutopilotRig,
) -> None:
    contact = await rig.contact()
    first = await rig.say(contact, ago=300, text="first incoming")
    last = await rig.say(contact, ago=60, text="last incoming")
    await rig.say(contact, ago=120, text="owner between", direction=MessageDirection.OUTGOING)
    runner = rig.runner(SequenceLLMClient(llm_result()), max_burst_wait_seconds=LONG_MAX_WAIT)

    history = await rig.messages.list_last(contact.id, 10)
    _skipped(await runner.run(contact.id, first.id), ReplyJobReason.SUPERSEDED)
    _queued(await runner.run(contact.id, last.id))

    assert [message.text for message in history] == [
        "first incoming",
        "owner between",
        "last incoming",
    ]
    assert burst_texts(history, None) == ["last incoming"]


async def test_an_outgoing_message_newer_by_time_but_stored_earlier_still_answers_the_trigger(
    rig: AutopilotRig,
) -> None:
    contact = await rig.contact()
    await rig.say(contact, ago=30, direction=MessageDirection.OUTGOING)
    trigger = await rig.say(contact, ago=60)
    client = SequenceLLMClient(llm_result())

    result = await rig.runner(client).run(contact.id, trigger.id)

    assert trigger.id > (await rig.messages.list_last(contact.id, 10))[-1].id
    _skipped(result, ReplyJobReason.ANSWERED)
    assert client.calls == 0


async def test_a_message_that_arrived_eight_hours_ago_is_stale_even_if_stored_just_now(
    rig: AutopilotRig,
) -> None:
    contact = await rig.contact()
    trigger = await rig.say(contact, ago=8 * 3600)
    client = SequenceLLMClient(llm_result())

    result = await rig.runner(client).run(contact.id, trigger.id)

    _skipped(result, ReplyJobReason.TRIGGER_STALE)
    assert client.calls == 0


@pytest.mark.parametrize("state", [OutboxState.SCHEDULED, OutboxState.CANCELLED])
async def test_an_existing_outbox_row_for_the_trigger_skips(
    rig: AutopilotRig, state: OutboxState
) -> None:
    contact, trigger = await _ready(rig)
    row = await rig.outbox.enqueue(
        contact.id, OTHER_REPLY_ZQ, rig.clock(), OutboxSource.AUTOPILOT, trigger.id
    )
    if state is OutboxState.CANCELLED:
        await rig.outbox.cancel_scheduled(row.id, "cancelled_by_owner")
    client = SequenceLLMClient(llm_result())

    result = await rig.runner(client).run(contact.id, trigger.id)

    _skipped(result, ReplyJobReason.ROW_EXISTS)
    assert client.calls == 0


async def test_a_stop_trigger_text_hands_over_without_a_row_or_a_model_call(
    rig: AutopilotRig,
) -> None:
    contact = await rig.contact()
    trigger = await rig.say(contact, text=MEETING_TEXT)
    client = SequenceLLMClient(llm_result())

    result = await rig.runner(client).run(contact.id, trigger.id)

    assert (result.outcome, result.reason, result.detail) == (
        ReplyJobOutcome.HANDED_OVER,
        ReplyJobReason.TEXT_TRIGGER,
        HandOverOutcome.HANDED_OVER.value,
    )
    current = await rig.reload(contact)
    assert (current.status, current.pause_reason) == (
        ContactStatus.PAUSED,
        PauseReason.STOP_TRIGGER,
    )
    [notice] = rig.hand_over_notifier.notices
    assert (notice.classes, notice.source) == ((TriggerClass.MEETING,), TriggerSource.TEXT)
    assert client.calls == 0
    assert await _rows(rig, contact) == []


async def test_a_stop_trigger_earlier_in_the_burst_hands_over(rig: AutopilotRig) -> None:
    contact = await rig.contact()
    await rig.say(contact, ago=30, text=MEETING_TEXT)
    trigger = await rig.say(contact, ago=10, text="and how are you")
    client = SequenceLLMClient(llm_result())

    result = await rig.runner(client).run(contact.id, trigger.id)

    assert result.reason is ReplyJobReason.TEXT_TRIGGER
    assert client.calls == 0


async def test_a_stop_trigger_before_reply_after_is_not_part_of_the_burst(
    rig: AutopilotRig,
) -> None:
    contact = await rig.contact(ContactStatus.PAUSED)
    old = await rig.say(contact, ago=60, text=MEETING_TEXT)
    await rig.contacts.set_status(contact.id, ContactStatus.ACTIVE, reply_after=old.created_at)
    trigger = await rig.say(contact, ago=10, text="and how are you")

    result = await rig.runner(SequenceLLMClient(llm_result())).run(contact.id, trigger.id)

    _queued(result)


def test_the_burst_is_the_run_of_incoming_messages_after_reply_after() -> None:
    def message(number: int, direction: MessageDirection) -> Message:
        return Message(
            id=number,
            contact_id=1,
            direction=direction,
            text=f"line {number}",
            tg_message_id=number,
            created_at=NOW + timedelta(seconds=number),
        )

    history = [
        message(1, MessageDirection.INCOMING),
        message(2, MessageDirection.OUTGOING),
        message(3, MessageDirection.INCOMING),
        message(4, MessageDirection.INCOMING),
    ]

    assert burst_texts(history, None) == ["line 3", "line 4"]
    assert burst_texts(history, history[2].created_at) == ["line 4"]
    assert burst_texts(history[:2], None) == []


async def test_the_model_signal_hands_over_by_the_model_without_classes(
    rig: AutopilotRig,
) -> None:
    contact, trigger = await _ready(rig)

    result = await rig.runner(SequenceLLMClient(llm_result(NEEDS_OWNER_REPLY))).run(
        contact.id, trigger.id
    )

    assert (result.outcome, result.reason) == (
        ReplyJobOutcome.HANDED_OVER,
        ReplyJobReason.NEEDS_OWNER,
    )
    [notice] = rig.hand_over_notifier.notices
    assert (notice.classes, notice.source) == ((), TriggerSource.MODEL)
    assert (await rig.reload(contact)).status is ContactStatus.PAUSED
    assert await _rows(rig, contact) == []


async def test_a_truncated_reply_is_retried_once_with_twice_the_tokens(rig: AutopilotRig) -> None:
    contact, trigger = await _ready(rig)
    client = SequenceLLMClient(llm_result(truncated=True), llm_result())

    _queued(await rig.runner(client).run(contact.id, trigger.id))

    assert client.max_tokens == [None, BASE_MAX_TOKENS * 2]


async def test_the_retried_token_limit_is_capped(rig: AutopilotRig) -> None:
    contact, trigger = await _ready(rig)
    client = SequenceLLMClient(llm_result(truncated=True), llm_result())

    await rig.runner(client, max_tokens=TRUNCATION_MAX_TOKENS).run(contact.id, trigger.id)

    assert client.max_tokens == [None, TRUNCATION_MAX_TOKENS]


async def test_a_second_truncation_hands_over_and_sends_nothing(rig: AutopilotRig) -> None:
    contact, trigger = await _ready(rig)
    client = SequenceLLMClient(llm_result(truncated=True))

    result = await rig.runner(client).run(contact.id, trigger.id)

    assert (result.outcome, result.reason) == (
        ReplyJobOutcome.HANDED_OVER,
        ReplyJobReason.TRUNCATED,
    )
    assert client.calls == 2
    assert await _rows(rig, contact) == []
    assert rig.hand_over_notifier.notices[0].source is TriggerSource.MODEL


async def test_an_invalid_reply_is_retried_once(rig: AutopilotRig) -> None:
    contact, trigger = await _ready(rig)
    client = SequenceLLMClient(llm_result(INVALID_REPLY), llm_result())

    _queued(await rig.runner(client).run(contact.id, trigger.id))

    assert client.calls == 2


async def test_a_second_invalid_reply_hands_over(rig: AutopilotRig) -> None:
    contact, trigger = await _ready(rig)
    client = SequenceLLMClient(llm_result(INVALID_REPLY))

    result = await rig.runner(client).run(contact.id, trigger.id)

    assert (result.outcome, result.reason) == (
        ReplyJobOutcome.HANDED_OVER,
        ReplyJobReason.REPLY_INVALID,
    )
    assert client.calls == 2
    assert await _rows(rig, contact) == []


async def test_a_prompt_too_large_hands_over_without_a_model_call(rig: AutopilotRig) -> None:
    contact, trigger = await _ready(rig)
    client = SequenceLLMClient(llm_result())

    result = await rig.runner(client, budget=10).run(contact.id, trigger.id)

    assert (result.outcome, result.reason) == (
        ReplyJobOutcome.HANDED_OVER,
        ReplyJobReason.PROMPT_TOO_LARGE,
    )
    assert client.calls == 0


async def test_a_missing_persona_stops_the_autopilot_with_one_notice(rig: AutopilotRig) -> None:
    contact, trigger = await _ready(rig)
    second = await rig.contact()
    other = await rig.say(second)
    async with rig.session_factory.begin() as session:
        await session.execute(text("DELETE FROM persona"))
    runner = rig.runner(SequenceLLMClient(llm_result()))

    result = await runner.run(contact.id, trigger.id)
    again = await runner.run(second.id, other.id)

    assert (result.outcome, result.reason) == (
        ReplyJobOutcome.STOPPED,
        ReplyJobReason.PERSONA_MISSING,
    )
    _skipped(again, ReplyJobReason.AUTOPILOT_STOPPED)
    state = await rig.state.get()
    assert (state.stopped, state.stop_reason) == (True, AutopilotStopReason.PERSONA_MISSING)
    assert len(rig.notifier.texts) == 1
    assert "autopilot resume --yes" in rig.notifier.texts[0]


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (LLMAuthError("auth", status_code=401), AutopilotStopReason.PROVIDER_AUTH),
        (LLMRequestError("request", status_code=400), AutopilotStopReason.PROVIDER_REQUEST),
        (LLMRequestError("balance", status_code=402), AutopilotStopReason.PROVIDER_REQUEST),
    ],
)
async def test_an_auth_or_request_error_stops_once_and_is_never_retried(
    rig: AutopilotRig, error: Exception, reason: AutopilotStopReason
) -> None:
    contact, trigger = await _ready(rig)
    client = SequenceLLMClient(error)

    runner = rig.runner(client)
    result = await runner.run(contact.id, trigger.id)
    again = await runner.run(contact.id, trigger.id)

    assert (result.outcome, result.reason.value, result.detail) == (
        ReplyJobOutcome.STOPPED,
        reason.value,
        type(error).__name__,
    )
    _skipped(again, ReplyJobReason.AUTOPILOT_STOPPED)
    assert client.calls == 1
    assert len(rig.notifier.texts) == 1
    assert type(error).__name__ in rig.notifier.texts[0]
    assert await _rows(rig, contact) == []


async def test_a_stop_that_was_already_stored_sends_no_second_notice(rig: AutopilotRig) -> None:
    contact, trigger = await _ready(rig)
    runner = rig.runner(SequenceLLMClient(LLMAuthError("auth", status_code=401)))

    await runner.run(contact.id, trigger.id)
    rig.notifier.texts.clear()
    second = await rig.contact()
    other = await rig.say(second)
    state = await rig.state.get()
    assert state.stopped

    result = await runner.run(second.id, other.id)

    _skipped(result, ReplyJobReason.AUTOPILOT_STOPPED)
    assert rig.notifier.texts == []


@pytest.mark.parametrize("error", TRANSIENT_ERRORS, ids=lambda error: type(error).__name__)
async def test_a_transient_error_is_retried_with_the_schedule_then_pauses_once(
    rig: AutopilotRig, error: Exception
) -> None:
    contact, trigger = await _ready(rig)
    runner = rig.runner(SequenceLLMClient(error))
    delays: list[int] = []

    for attempt in range(1, JOB_MAX_TRIES):
        with pytest.raises(RetryJob) as raised:
            await runner.run(contact.id, trigger.id, attempt=attempt)
        delays.append(raised.value.defer_seconds)
        assert raised.value.error_class == type(error).__name__
    result = await runner.run(contact.id, trigger.id, attempt=JOB_MAX_TRIES)

    assert delays == [60, 300, 900]
    assert tuple(delays) == RETRY_DELAYS_SECONDS
    assert (result.outcome, result.reason) == (
        ReplyJobOutcome.PAUSED,
        ReplyJobReason.PROVIDER_UNAVAILABLE,
    )
    state = await rig.state.get()
    assert state.paused_until == rig.clock() + timedelta(seconds=PROVIDER_COOLDOWN_SECONDS)
    assert state.pause_reason is AutopilotPauseReason.PROVIDER_UNAVAILABLE
    assert len(rig.notifier.texts) == 1
    assert "15" in rig.notifier.texts[0]
    assert await _rows(rig, contact) == []


async def test_an_exhaustion_during_an_existing_pause_sends_no_second_notice(
    rig: AutopilotRig,
) -> None:
    contact, trigger = await _ready(rig)
    runner = rig.runner(SequenceLLMClient(LLMUnavailableError("down", status_code=503)))
    await runner.run(contact.id, trigger.id, attempt=JOB_MAX_TRIES)
    rig.clock.advance(PROVIDER_COOLDOWN_SECONDS - 1)
    now = rig.clock()

    newly = await rig.state.pause_until(
        now + timedelta(seconds=10), AutopilotPauseReason.PROVIDER_UNAVAILABLE, now=now
    )

    assert newly is False
    assert len(rig.notifier.texts) == 1


async def test_after_the_cooldown_the_same_message_is_answered(rig: AutopilotRig) -> None:
    contact, trigger = await _ready(rig)
    failing = rig.runner(SequenceLLMClient(LLMUnavailableError("down", status_code=503)))
    await failing.run(contact.id, trigger.id, attempt=JOB_MAX_TRIES)
    rig.clock.advance(PROVIDER_COOLDOWN_SECONDS)

    _queued(await rig.runner(SequenceLLMClient(llm_result())).run(contact.id, trigger.id))


async def test_an_unexpected_exception_is_transient(rig: AutopilotRig) -> None:
    contact, trigger = await _ready(rig)
    runner = rig.runner(SequenceLLMClient(RuntimeError("invented failure")))

    with pytest.raises(RetryJob) as raised:
        await runner.run(contact.id, trigger.id)
    result = await runner.run(contact.id, trigger.id, attempt=JOB_MAX_TRIES)

    assert (raised.value.defer_seconds, raised.value.error_class) == (60, "RuntimeError")
    assert result.outcome is ReplyJobOutcome.PAUSED


async def test_an_enqueue_refusal_is_a_skip_with_its_reason(rig: AutopilotRig) -> None:
    contact, trigger = await _ready(rig)
    rig.guard_switch.autopilot_enabled = False

    result = await rig.runner(SequenceLLMClient(llm_result())).run(contact.id, trigger.id)

    _skipped(result, ReplyJobReason.ENQUEUE_REFUSED)
    assert result.detail == "autopilot_disabled"
    assert await _rows(rig, contact) == []


class _NoPlan(HumanizationPlanner):
    async def plan(
        self,
        contact_id: int,
        trigger_message_id: int,
        now: datetime,
        *,
        ongoing: bool | None = None,
    ) -> SendPlan:
        raise PlanNotPossible(contact_id, trigger_message_id)


async def test_a_plan_that_is_not_possible_is_a_skip(rig: AutopilotRig) -> None:
    contact, trigger = await _ready(rig)
    planner = _NoPlan(rig.messages, rig.schedule, random.Random(1))

    result = await rig.runner(SequenceLLMClient(llm_result()), planner=planner).run(
        contact.id, trigger.id
    )

    _skipped(result, ReplyJobReason.PLAN_NOT_POSSIBLE)
    assert await _rows(rig, contact) == []


async def test_a_failing_notifier_never_changes_the_stop(rig: AutopilotRig) -> None:
    contact, trigger = await _ready(rig)
    rig.notifier = RecordingNotifier(RuntimeError("invented notifier failure"))

    result = await rig.runner(SequenceLLMClient(LLMAuthError("auth", status_code=401))).run(
        contact.id, trigger.id
    )

    assert result.outcome is ReplyJobOutcome.STOPPED
    assert (await rig.state.get()).stopped


async def test_every_outcome_is_one_info_line_without_texts_names_or_ids(
    rig: AutopilotRig, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("DEBUG")
    contact, trigger = await _ready(rig)
    await rig.runner(SequenceLLMClient(llm_result())).run(contact.id, trigger.id)
    handed = await rig.contact()
    meeting = await rig.say(handed, text=MEETING_TEXT)
    await rig.runner(SequenceLLMClient(llm_result())).run(handed.id, meeting.id)
    stopped = await rig.contact()
    message = await rig.say(stopped)
    await rig.runner(SequenceLLMClient(LLMAuthError("auth", status_code=401))).run(
        stopped.id, message.id
    )

    output = json_logs(caplog)
    finished = [r for r in caplog.records if r.getMessage() == "reply job finished"]
    assert [r.__dict__["outcome"] for r in finished] == ["queued", "handed_over", "stopped"]
    for marker in (
        REPLY_TEXT_ZQ,
        INCOMING_TEXT_ZQ,
        MEETING_TEXT,
        CONTACT_NAME_ZQ,
        CONTACT_USERNAME_ZQ,
        PERSONA_TEXT_ZQ,
        FIRST_TG_ID + 1,
    ):
        assert_absent(marker, output)
    for notice in rig.notifier.texts:
        for marker in (REPLY_TEXT_ZQ, INCOMING_TEXT_ZQ, CONTACT_NAME_ZQ, CONTACT_USERNAME_ZQ):
            assert_absent(marker, notice)


async def test_messages_of_a_pending_contact_are_answered_after_the_first_activation(
    rig: AutopilotRig,
) -> None:
    contact = await rig.contact(ContactStatus.PENDING)
    trigger = await rig.say(contact, ago=REPLY_STALE_AFTER_SECONDS - 60)
    activated = await rig.activate(contact.id)

    result = await rig.runner(SequenceLLMClient(llm_result())).run(contact.id, trigger.id)

    assert activated.reply_after is None
    _queued(result)


def test_the_retry_schedule_fits_the_number_of_tries() -> None:
    assert len(RETRY_DELAYS_SECONDS) == JOB_MAX_TRIES - 1
