from __future__ import annotations

import logging

import pytest

from cyrano.domain.enums import OutboxSource, OutboxState, ReplyJobOutcome, ReplyJobReason
from cyrano.domain.models import Contact
from cyrano.domain.outbox import CANCEL_REASON_SUPERSEDED, ScheduledMessage
from cyrano.domain.reply import MAX_REPLY_PARTS
from tests.services.autopilot_rig import AutopilotRig, SequenceLLMClient, llm_result

JOB_LOGGER = "cyrano.services.reply_job"
TWO_LINES = "invented first line\ninvented second line"
FIVE_LINES = "one\ntwo\nthree\nfour\nfive"
OLDER_AGO = 300
NEWER_AGO = 30
FIRST_TG_MESSAGE_ID = 5_501


async def _rows(rig: AutopilotRig, contact: Contact) -> list[ScheduledMessage]:
    rows = await rig.outbox.list_rows(None, 50)
    return sorted((row for row in rows if row.contact_id == contact.id), key=lambda row: row.id)


async def test_a_multi_line_reply_is_queued_as_a_chain_of_parts(rig: AutopilotRig) -> None:
    contact = await rig.contact()
    trigger = await rig.say(contact)

    result = await rig.runner(SequenceLLMClient(llm_result(TWO_LINES))).run(contact.id, trigger.id)

    first, second = await _rows(rig, contact)
    assert (result.outcome, result.reason) == (ReplyJobOutcome.QUEUED, ReplyJobReason.QUEUED)
    assert (result.row_id, result.parts) == (first.id, 2)
    assert [first.text, second.text] == TWO_LINES.splitlines()
    assert (first.trigger_message_id, first.follows_id) == (trigger.id, None)
    assert (second.trigger_message_id, second.follows_id) == (None, first.id)
    assert {row.source for row in (first, second)} == {OutboxSource.AUTOPILOT}
    assert second.send_at == first.send_at


async def test_a_single_line_reply_is_one_row_as_before(rig: AutopilotRig) -> None:
    contact = await rig.contact()
    trigger = await rig.say(contact)

    result = await rig.runner(SequenceLLMClient(llm_result())).run(contact.id, trigger.id)

    [row] = await _rows(rig, contact)
    assert (result.row_id, result.parts) == (row.id, 1)
    assert row.follows_id is None


async def test_more_lines_than_the_part_limit_give_three_parts(rig: AutopilotRig) -> None:
    contact = await rig.contact()
    trigger = await rig.say(contact)

    result = await rig.runner(SequenceLLMClient(llm_result(FIVE_LINES))).run(contact.id, trigger.id)

    rows = await _rows(rig, contact)
    assert result.parts == MAX_REPLY_PARTS
    assert [row.text for row in rows] == ["one", "two", "three four five"]


async def test_a_job_run_again_creates_no_extra_part(rig: AutopilotRig) -> None:
    contact = await rig.contact()
    trigger = await rig.say(contact)
    client = SequenceLLMClient(llm_result(TWO_LINES))

    first = await rig.runner(client).run(contact.id, trigger.id)
    again = await rig.runner(client).run(contact.id, trigger.id, attempt=2)

    assert first.parts == 2
    assert (again.outcome, again.reason) == (ReplyJobOutcome.SKIPPED, ReplyJobReason.ROW_EXISTS)
    assert len(await _rows(rig, contact)) == 2
    assert client.calls == 1


async def test_a_new_reply_leaves_every_part_of_an_older_reply_whose_first_part_was_sent(
    rig: AutopilotRig,
) -> None:
    contact = await rig.contact()
    older = await rig.say(contact, ago=OLDER_AGO)
    sent, waiting = await rig.outbox.enqueue_sequence(
        contact.id, TWO_LINES.splitlines(), rig.clock(), OutboxSource.AUTOPILOT, older.id
    )
    await rig.outbox.claim_due(rig.clock())
    await rig.outbox.complete_send(sent.id, contact.id, sent.text, FIRST_TG_MESSAGE_ID, rig.clock())
    newer = await rig.say(contact, ago=0)

    result = await rig.runner(SequenceLLMClient(llm_result(TWO_LINES))).run(contact.id, newer.id)

    assert result.outcome is ReplyJobOutcome.QUEUED
    stored_sent = await rig.outbox.get(sent.id)
    stored_waiting = await rig.outbox.get(waiting.id)
    assert stored_sent is not None
    assert stored_waiting is not None
    assert stored_sent.state is OutboxState.SENT
    assert (stored_waiting.state, stored_waiting.cancel_reason) == (OutboxState.SCHEDULED, None)


async def test_a_new_reply_cancels_every_part_of_an_older_reply_that_has_not_started(
    rig: AutopilotRig,
) -> None:
    contact = await rig.contact()
    older = await rig.say(contact, ago=OLDER_AGO)
    first, second = await rig.outbox.enqueue_sequence(
        contact.id, TWO_LINES.splitlines(), rig.clock(), OutboxSource.AUTOPILOT, older.id
    )
    newer = await rig.say(contact, ago=0)

    result = await rig.runner(SequenceLLMClient(llm_result(TWO_LINES))).run(contact.id, newer.id)

    assert result.outcome is ReplyJobOutcome.QUEUED
    for part in (first, second):
        stored = await rig.outbox.get(part.id)
        assert stored is not None
        assert (stored.state, stored.cancel_reason) == (
            OutboxState.CANCELLED,
            CANCEL_REASON_SUPERSEDED,
        )


async def test_the_finished_line_of_a_queued_job_carries_the_number_of_parts(
    rig: AutopilotRig, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=JOB_LOGGER)
    contact = await rig.contact()
    trigger = await rig.say(contact)

    await rig.runner(SequenceLLMClient(llm_result(TWO_LINES))).run(contact.id, trigger.id)

    [record] = [
        record
        for record in caplog.records
        if record.name == JOB_LOGGER and record.getMessage() == "reply job finished"
    ]
    assert record.__dict__["parts"] == 2
    for line in TWO_LINES.splitlines():
        assert line not in caplog.text
