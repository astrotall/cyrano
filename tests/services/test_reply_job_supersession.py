from __future__ import annotations

from datetime import timedelta

from cyrano.domain.enums import OutboxSource, OutboxState, ReplyJobOutcome, ReplyJobReason
from cyrano.domain.models import Contact, Message
from cyrano.domain.outbox import CANCEL_REASON_SUPERSEDED, ScheduledMessage
from tests.services.autopilot_rig import AutopilotRig, SequenceLLMClient, llm_result

FAR_AHEAD = timedelta(hours=1)
SEVENTH_AGO = 420
SIXTH_AGO = 360
FIFTH_AGO = 300
FOURTH_AGO = 240
THIRD_AGO = 180
NEWEST_AGO = 30
WAIT_LONGER_THAN_THE_BURST = FIFTH_AGO + 1


async def _row(
    rig: AutopilotRig,
    contact: Contact,
    trigger: Message,
    source: OutboxSource = OutboxSource.AUTOPILOT,
    *,
    due: bool = False,
) -> ScheduledMessage:
    send_at = rig.clock() if due else rig.clock() + FAR_AHEAD
    return await rig.outbox.enqueue(contact.id, "invented row text", send_at, source, trigger.id)


async def _state(rig: AutopilotRig, row_id: int) -> tuple[OutboxState, str | None]:
    row = await rig.outbox.get(row_id)
    assert row is not None
    return row.state, row.cancel_reason


async def test_a_queued_reply_cancels_the_older_scheduled_autopilot_rows_of_the_contact(
    rig: AutopilotRig,
) -> None:
    contact = await rig.contact()
    other = await rig.contact()
    sending_trigger = await rig.say(contact, ago=SEVENTH_AGO)
    manual_trigger = await rig.say(contact, ago=SIXTH_AGO)
    older_trigger = await rig.say(contact, ago=FIFTH_AGO)
    foreign_trigger = await rig.say(other, ago=FOURTH_AGO)
    sending = await _row(rig, contact, sending_trigger, due=True)
    claimed = await rig.outbox.claim_due(rig.clock())
    manual = await _row(rig, contact, manual_trigger, OutboxSource.MANUAL_TEST)
    older = await _row(rig, contact, older_trigger)
    foreign = await _row(rig, other, foreign_trigger)
    newest = await rig.say(contact, ago=NEWEST_AGO)

    result = await rig.runner(SequenceLLMClient(llm_result())).run(contact.id, newest.id)

    assert claimed is not None
    assert claimed.id == sending.id
    assert result.outcome is ReplyJobOutcome.QUEUED
    assert result.row_id is not None
    assert await _state(rig, older.id) == (OutboxState.CANCELLED, CANCEL_REASON_SUPERSEDED)
    assert await _state(rig, manual.id) == (OutboxState.SCHEDULED, None)
    assert await _state(rig, sending.id) == (OutboxState.SENDING, None)
    assert await _state(rig, foreign.id) == (OutboxState.SCHEDULED, None)
    assert await _state(rig, result.row_id) == (OutboxState.SCHEDULED, None)


async def test_a_sent_row_of_the_contact_is_not_cancelled_by_a_new_reply(
    rig: AutopilotRig,
) -> None:
    contact = await rig.contact()
    trigger = await rig.say(contact, ago=FIFTH_AGO)
    row = await _row(rig, contact, trigger, due=True)
    await rig.outbox.claim_due(rig.clock())
    sent_at = rig.clock() - timedelta(seconds=THIRD_AGO)
    await rig.outbox.complete_send(row.id, contact.id, row.text, 5001, sent_at)
    newest = await rig.say(contact, ago=NEWEST_AGO)

    result = await rig.runner(SequenceLLMClient(llm_result())).run(contact.id, newest.id)

    assert result.outcome is ReplyJobOutcome.QUEUED
    assert await _state(rig, row.id) == (OutboxState.SENT, None)


async def test_a_skipped_job_cancels_nothing(rig: AutopilotRig) -> None:
    contact = await rig.contact()
    older = await rig.say(contact, ago=FIFTH_AGO)
    row = await _row(rig, contact, older)
    newest = await rig.say(contact, ago=NEWEST_AGO)
    other_trigger = await rig.say(contact, ago=THIRD_AGO)

    runner = rig.runner(
        SequenceLLMClient(llm_result()), max_burst_wait_seconds=WAIT_LONGER_THAN_THE_BURST
    )

    result = await runner.run(contact.id, other_trigger.id)

    assert newest.id < other_trigger.id
    assert result.reason is ReplyJobReason.SUPERSEDED
    assert await _state(rig, row.id) == (OutboxState.SCHEDULED, None)
