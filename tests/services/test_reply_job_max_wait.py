from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from datetime import timedelta

import pytest
from pydantic import BaseModel

from cyrano.config import DEFAULT_AUTOPILOT_MAX_BURST_WAIT_SECONDS
from cyrano.domain.autopilot import ADOPTED_NEWEST_DETAIL, ReplyJobResult
from cyrano.domain.enums import (
    ContactStatus,
    MessageDirection,
    OutboxSource,
    OutboxState,
    ReplyJobOutcome,
    ReplyJobReason,
)
from cyrano.domain.llm import LLMMessage, LLMResult
from cyrano.domain.models import Contact, Message
from cyrano.domain.outbox import CANCEL_REASON_SUPERSEDED, ScheduledMessage
from cyrano.domain.schedule import default_schedule_profile
from tests.services.autopilot_rig import (
    AutopilotRig,
    SequenceLLMClient,
    json_logs,
    llm_result,
)

MAX_WAIT = DEFAULT_AUTOPILOT_MAX_BURST_WAIT_SECONDS
JOB_LOGGER = "cyrano.services.reply_job"
MEETING_TEXT = "давай встретимся завтра вечером"
GAP_SECONDS = 10
JUST_UNDER = timedelta(microseconds=1)
BARRIER_TIMEOUT_SECONDS = 5
TWO_JOBS = 2
ONGOING_REACTION = default_schedule_profile("UTC").reaction.ongoing_seconds


class _BarrierLLMClient:
    def __init__(self, parties: int) -> None:
        self._parties = parties
        self.calls = 0
        self._everyone_arrived = asyncio.Event()

    async def complete(
        self,
        messages: Sequence[LLMMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResult:
        self.calls += 1
        if self.calls >= self._parties:
            self._everyone_arrived.set()
        async with asyncio.timeout(BARRIER_TIMEOUT_SECONDS):
            await self._everyone_arrived.wait()
        return llm_result()

    async def complete_json[T: BaseModel](
        self,
        messages: Sequence[LLMMessage],
        schema: type[T],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> T:
        raise NotImplementedError


async def _burst(rig: AutopilotRig, contact: Contact, first_ago: float) -> list[Message]:
    ages = [first_ago - GAP_SECONDS * step for step in range(3)]
    return [await rig.say(contact, ago=age, text=f"invented line {age}") for age in ages]


async def _rows(rig: AutopilotRig, contact: Contact) -> list[ScheduledMessage]:
    return [row for row in await rig.outbox.list_rows(None, 100) if row.contact_id == contact.id]


def _queued_for(result: ReplyJobResult) -> int:
    assert (result.outcome, result.reason) == (ReplyJobOutcome.QUEUED, ReplyJobReason.QUEUED)
    assert result.row_id is not None
    return result.row_id


async def test_a_newer_message_in_a_young_burst_skips_as_superseded(rig: AutopilotRig) -> None:
    contact = await rig.contact()
    first, _, _ = await _burst(rig, contact, first_ago=MAX_WAIT - 1)
    client = SequenceLLMClient(llm_result())

    result = await rig.runner(client).run(contact.id, first.id)

    assert (result.outcome, result.reason, result.detail) == (
        ReplyJobOutcome.SKIPPED,
        ReplyJobReason.SUPERSEDED,
        None,
    )
    assert client.calls == 0
    assert await _rows(rig, contact) == []


async def test_a_burst_just_under_the_maximum_wait_is_still_superseded(rig: AutopilotRig) -> None:
    contact = await rig.contact()
    first = await rig.say(contact, ago=MAX_WAIT)
    await rig.say(contact, ago=1)
    rig.clock.now -= JUST_UNDER
    client = SequenceLLMClient(llm_result())

    result = await rig.runner(client).run(contact.id, first.id)

    assert result.reason is ReplyJobReason.SUPERSEDED
    assert client.calls == 0


@pytest.mark.parametrize("first_ago", [MAX_WAIT, MAX_WAIT + 1, MAX_WAIT * 4])
async def test_a_burst_at_or_beyond_the_maximum_wait_answers_the_newest_message(
    rig: AutopilotRig, first_ago: float
) -> None:
    contact = await rig.contact()
    first, _, newest = await _burst(rig, contact, first_ago=first_ago)
    client = SequenceLLMClient(llm_result())

    result = await rig.runner(client).run(contact.id, first.id)

    row_id = _queued_for(result)
    assert result.detail == ADOPTED_NEWEST_DETAIL
    [row] = await _rows(rig, contact)
    assert (row.id, row.trigger_message_id, row.source, row.state) == (
        row_id,
        newest.id,
        OutboxSource.AUTOPILOT,
        OutboxState.SCHEDULED,
    )
    assert client.calls == 1


async def test_the_adopted_reply_is_planned_with_the_short_reaction(rig: AutopilotRig) -> None:
    contact = await rig.contact()
    first, _, newest = await _burst(rig, contact, first_ago=MAX_WAIT * 2)

    result = await rig.runner(SequenceLLMClient(llm_result())).run(contact.id, first.id)

    [row] = await _rows(rig, contact)
    assert row.id == _queued_for(result)
    latest_send_at = max(rig.clock(), newest.created_at + timedelta(seconds=ONGOING_REACTION[1]))
    assert rig.clock() <= row.send_at <= latest_send_at


async def test_the_adopted_newest_message_is_not_superseded_by_the_sender_check(
    rig: AutopilotRig,
) -> None:
    contact = await rig.contact()
    first, _, newest = await _burst(rig, contact, first_ago=MAX_WAIT * 2)

    await rig.runner(SequenceLLMClient(llm_result())).run(contact.id, first.id)

    [latest] = await rig.messages.list_last(contact.id, 1)
    [row] = await _rows(rig, contact)
    assert latest.id == newest.id == row.trigger_message_id


async def test_the_stop_trigger_gate_runs_on_the_burst_up_to_the_newest_message(
    rig: AutopilotRig,
) -> None:
    contact = await rig.contact()
    first = await rig.say(contact, ago=MAX_WAIT * 2, text="invented harmless line")
    await rig.say(contact, ago=MAX_WAIT, text=MEETING_TEXT)
    await rig.say(contact, ago=1, text="invented closing line")
    client = SequenceLLMClient(llm_result())

    result = await rig.runner(client).run(contact.id, first.id)

    assert (result.outcome, result.reason) == (
        ReplyJobOutcome.HANDED_OVER,
        ReplyJobReason.TEXT_TRIGGER,
    )
    assert client.calls == 0
    assert await _rows(rig, contact) == []
    assert (await rig.reload(contact)).status is ContactStatus.PAUSED


async def test_an_existing_row_for_the_newest_message_skips_the_adopting_job(
    rig: AutopilotRig,
) -> None:
    contact = await rig.contact()
    first, _, newest = await _burst(rig, contact, first_ago=MAX_WAIT * 2)
    existing = await rig.outbox.enqueue(
        contact.id, "invented stored row", rig.clock(), OutboxSource.AUTOPILOT, newest.id
    )
    client = SequenceLLMClient(llm_result())

    result = await rig.runner(client).run(contact.id, first.id)

    assert (result.outcome, result.reason, result.detail) == (
        ReplyJobOutcome.SKIPPED,
        ReplyJobReason.ROW_EXISTS,
        ADOPTED_NEWEST_DETAIL,
    )
    assert client.calls == 0
    assert [row.id for row in await _rows(rig, contact)] == [existing.id]


async def test_an_outgoing_message_after_the_newest_incoming_one_means_answered(
    rig: AutopilotRig,
) -> None:
    contact = await rig.contact()
    first, _, _ = await _burst(rig, contact, first_ago=MAX_WAIT * 2)
    await rig.say(contact, ago=0, direction=MessageDirection.OUTGOING)
    client = SequenceLLMClient(llm_result())

    result = await rig.runner(client).run(contact.id, first.id)

    assert result.reason is ReplyJobReason.ANSWERED
    assert client.calls == 0


async def test_the_burst_age_counts_from_the_first_message_after_the_last_outgoing_one(
    rig: AutopilotRig,
) -> None:
    contact = await rig.contact()
    first = await rig.say(contact, ago=MAX_WAIT * 4)
    await rig.say(contact, ago=MAX_WAIT * 3, direction=MessageDirection.OUTGOING)
    await rig.say(contact, ago=MAX_WAIT - 1)
    client = SequenceLLMClient(llm_result())

    result = await rig.runner(client).run(contact.id, first.id)

    assert result.reason is ReplyJobReason.SUPERSEDED
    assert client.calls == 0


async def test_the_burst_age_ignores_messages_written_before_the_resume(
    rig: AutopilotRig,
) -> None:
    contact = await rig.contact()
    before_resume = await rig.say(contact, ago=MAX_WAIT * 4)
    await rig.contacts.set_status(contact.id, ContactStatus.PAUSED)
    await rig.contacts.set_status(
        contact.id, ContactStatus.ACTIVE, reply_after=before_resume.created_at
    )
    trigger = await rig.say(contact, ago=MAX_WAIT - GAP_SECONDS)
    await rig.say(contact, ago=1)
    client = SequenceLLMClient(llm_result())

    result = await rig.runner(client).run(contact.id, trigger.id)

    assert result.reason is ReplyJobReason.SUPERSEDED
    assert client.calls == 0


async def test_an_adopting_job_cancels_the_scheduled_row_of_an_older_trigger(
    rig: AutopilotRig,
) -> None:
    contact = await rig.contact()
    first, middle, newest = await _burst(rig, contact, first_ago=MAX_WAIT * 2)
    older = await rig.outbox.enqueue(
        contact.id,
        "invented older row",
        rig.clock() + timedelta(hours=1),
        OutboxSource.AUTOPILOT,
        middle.id,
    )

    result = await rig.runner(SequenceLLMClient(llm_result())).run(contact.id, first.id)

    row_id = _queued_for(result)
    stored_older = await rig.outbox.get(older.id)
    assert stored_older is not None
    assert (stored_older.state, stored_older.cancel_reason) == (
        OutboxState.CANCELLED,
        CANCEL_REASON_SUPERSEDED,
    )
    adopted = await rig.outbox.get(row_id)
    assert adopted is not None
    assert (adopted.trigger_message_id, adopted.state) == (newest.id, OutboxState.SCHEDULED)


async def test_two_jobs_adopting_the_same_newest_message_write_one_row(rig: AutopilotRig) -> None:
    contact = await rig.contact()
    first, second, newest = await _burst(rig, contact, first_ago=MAX_WAIT * 2)
    client = _BarrierLLMClient(TWO_JOBS)
    runner = rig.runner(client)

    results = await asyncio.gather(
        runner.run(contact.id, first.id), runner.run(contact.id, second.id)
    )

    rows = await _rows(rig, contact)
    assert [row.trigger_message_id for row in rows] == [newest.id]
    assert client.calls == TWO_JOBS
    assert [_queued_for(result) for result in results] == [rows[0].id, rows[0].id]
    assert {result.detail for result in results} == {ADOPTED_NEWEST_DETAIL}


async def test_the_jobs_of_a_whole_burst_past_the_maximum_wait_write_one_row(
    rig: AutopilotRig,
) -> None:
    contact = await rig.contact()
    burst = await _burst(rig, contact, first_ago=MAX_WAIT * 2)
    client = SequenceLLMClient(llm_result())
    runner = rig.runner(client)

    results = [await runner.run(contact.id, message.id) for message in burst]

    [row] = await _rows(rig, contact)
    assert row.trigger_message_id == burst[-1].id
    assert [(result.reason, result.detail) for result in results] == [
        (ReplyJobReason.QUEUED, ADOPTED_NEWEST_DETAIL),
        (ReplyJobReason.ROW_EXISTS, ADOPTED_NEWEST_DETAIL),
        (ReplyJobReason.ROW_EXISTS, None),
    ]
    assert client.calls == 1


async def test_the_finished_line_carries_the_adopted_detail_and_no_text(
    rig: AutopilotRig, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=JOB_LOGGER)
    contact = await rig.contact()
    first, _, _ = await _burst(rig, contact, first_ago=MAX_WAIT * 2)

    await rig.runner(SequenceLLMClient(llm_result())).run(contact.id, first.id)

    [record] = [
        record
        for record in caplog.records
        if record.name == JOB_LOGGER and record.getMessage() == "reply job finished"
    ]
    assert record.__dict__["detail"] == ADOPTED_NEWEST_DETAIL
    assert record.__dict__["trigger_message_id"] == first.id
    assert "invented line" not in json_logs(caplog)
