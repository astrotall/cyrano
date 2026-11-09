from __future__ import annotations

import logging
from datetime import timedelta

import pytest

from cyrano.domain.enums import (
    BlockKind,
    MessageDirection,
    ReplyJobOutcome,
    ReplyJobReason,
    WrapUpPhase,
)
from cyrano.domain.models import Contact, Message, MessageCreate
from cyrano.domain.outbox import ScheduledMessage
from cyrano.domain.schedule import UnavailableSoon
from tests.privacy import assert_absent
from tests.services.autopilot_rig import (
    AutopilotRig,
    SequenceLLMClient,
    json_logs,
    llm_result,
)
from tests.services.quote_fakes import LATE, FixedPlanner

JOB_LOGGER = "cyrano.services.reply_job"
EARLIER_TEXT_ZQ = "earlier-quote-marker-zq do you like invented tea"
LATER_TEXT_ZQ = "later-quote-marker-zq and what about the invented weekend"
FIRST_LINE_ZQ = "first-part-quote-zq yes, green tea"
SECOND_LINE_ZQ = "second-part-quote-zq weekend is free"
LONG_EARLIER_TG_MESSAGE_ID = 731000000001
LONG_LATER_TG_MESSAGE_ID = 731000000002


async def _rows(rig: AutopilotRig, contact: Contact) -> list[ScheduledMessage]:
    rows = await rig.outbox.list_rows(None, 50)
    return sorted((row for row in rows if row.contact_id == contact.id), key=lambda row: row.id)


async def _two_waiting(rig: AutopilotRig, *, ago: float = 40) -> tuple[Contact, Message, Message]:
    contact = await rig.contact()
    await rig.say(contact, ago=ago + 600, direction=MessageDirection.OUTGOING, text="hi there")
    earlier = await rig.say(contact, ago=ago + 20, text=EARLIER_TEXT_ZQ)
    later = await rig.say(contact, ago=ago, text=LATER_TEXT_ZQ)
    return contact, earlier, later


async def _say_with_id(
    rig: AutopilotRig, contact: Contact, text: str, tg_message_id: int, ago: float
) -> Message:
    return await rig.messages.append(
        MessageCreate(
            contact_id=contact.id,
            direction=MessageDirection.INCOMING,
            text=text,
            tg_message_id=tg_message_id,
            created_at=rig.clock() - timedelta(seconds=ago),
        )
    )


async def test_a_model_marker_puts_the_target_on_its_part(rig: AutopilotRig) -> None:
    contact, earlier, later = await _two_waiting(rig)
    reply = f"↩2 {FIRST_LINE_ZQ}\n{SECOND_LINE_ZQ}"

    result = await rig.runner(SequenceLLMClient(llm_result(reply)), quote_enabled=True).run(
        contact.id, later.id
    )

    first, second = await _rows(rig, contact)
    assert (result.outcome, result.parts, result.quoted) == (ReplyJobOutcome.QUEUED, 2, True)
    assert [first.text, second.text] == [FIRST_LINE_ZQ, SECOND_LINE_ZQ]
    assert first.reply_to_tg_message_id == earlier.tg_message_id
    assert second.reply_to_tg_message_id is None


async def test_a_marker_on_the_second_line_quotes_only_the_second_part(rig: AutopilotRig) -> None:
    contact, earlier, later = await _two_waiting(rig)
    reply = f"{FIRST_LINE_ZQ}\n↩2: {SECOND_LINE_ZQ}"

    await rig.runner(SequenceLLMClient(llm_result(reply)), quote_enabled=True).run(
        contact.id, later.id
    )

    first, second = await _rows(rig, contact)
    assert (first.reply_to_tg_message_id, second.reply_to_tg_message_id) == (
        None,
        earlier.tg_message_id,
    )
    assert second.text == SECOND_LINE_ZQ


async def test_a_marker_for_our_own_message_gives_no_target_and_no_marker(
    rig: AutopilotRig,
) -> None:
    contact, _, later = await _two_waiting(rig)

    result = await rig.runner(
        SequenceLLMClient(llm_result(f"↩1 {FIRST_LINE_ZQ}")), quote_enabled=True
    ).run(contact.id, later.id)

    [row] = await _rows(rig, contact)
    assert (row.text, row.reply_to_tg_message_id, result.quoted) == (FIRST_LINE_ZQ, None, False)


async def test_the_switch_off_gives_no_target_but_still_strips_the_marker(
    rig: AutopilotRig,
) -> None:
    contact, _, later = await _two_waiting(rig, ago=LATE * 2)

    await rig.runner(
        SequenceLLMClient(llm_result(f"↩2 {FIRST_LINE_ZQ}")),
        quote_enabled=False,
        planner=FixedPlanner(rig),
    ).run(contact.id, later.id)

    [row] = await _rows(rig, contact)
    assert (row.text, row.reply_to_tg_message_id) == (FIRST_LINE_ZQ, None)


async def test_the_job_switch_off_gives_no_target_even_from_a_numbered_prompt(
    rig: AutopilotRig,
) -> None:
    contact, _, later = await _two_waiting(rig)

    await rig.runner(
        SequenceLLMClient(llm_result(f"↩2 {FIRST_LINE_ZQ}")),
        quote_enabled=False,
        prompt_quotes=True,
    ).run(contact.id, later.id)

    [row] = await _rows(rig, contact)
    assert (row.text, row.reply_to_tg_message_id) == (FIRST_LINE_ZQ, None)


async def test_the_switch_off_never_reads_the_late_threshold(rig: AutopilotRig) -> None:
    contact, _, later = await _two_waiting(rig, ago=LATE * 2)
    planner = FixedPlanner(rig)

    await rig.runner(SequenceLLMClient(llm_result()), planner=planner).run(contact.id, later.id)

    [row] = await _rows(rig, contact)
    assert row.reply_to_tg_message_id is None
    assert planner.late_reads == 0


@pytest.mark.parametrize(("ago", "quoted"), [(LATE, True), (LATE - 1, False)])
async def test_the_late_rule_quotes_the_trigger_exactly_from_the_threshold(
    rig: AutopilotRig, ago: int, quoted: bool
) -> None:
    contact = await rig.contact()
    trigger = await rig.say(contact, ago=ago)

    result = await rig.runner(
        SequenceLLMClient(llm_result(f"{FIRST_LINE_ZQ}\n{SECOND_LINE_ZQ}")),
        quote_enabled=True,
        planner=FixedPlanner(rig),
    ).run(contact.id, trigger.id)

    first, second = await _rows(rig, contact)
    assert first.reply_to_tg_message_id == (trigger.tg_message_id if quoted else None)
    assert second.reply_to_tg_message_id is None
    assert result.quoted is quoted


async def test_the_late_rule_counts_the_planned_delay(rig: AutopilotRig) -> None:
    contact = await rig.contact()
    trigger = await rig.say(contact, ago=10)

    await rig.runner(
        SequenceLLMClient(llm_result()),
        quote_enabled=True,
        planner=FixedPlanner(rig, delay_seconds=LATE - 10),
    ).run(contact.id, trigger.id)

    [row] = await _rows(rig, contact)
    assert row.reply_to_tg_message_id == trigger.tg_message_id


async def test_a_zero_threshold_disables_the_late_rule(rig: AutopilotRig) -> None:
    contact = await rig.contact()
    trigger = await rig.say(contact, ago=3600)

    await rig.runner(
        SequenceLLMClient(llm_result()),
        quote_enabled=True,
        planner=FixedPlanner(rig, late_seconds=0),
    ).run(contact.id, trigger.id)

    [row] = await _rows(rig, contact)
    assert row.reply_to_tg_message_id is None


async def test_a_model_target_keeps_the_late_rule_away(rig: AutopilotRig) -> None:
    contact, earlier, later = await _two_waiting(rig, ago=LATE * 3)

    await rig.runner(
        SequenceLLMClient(llm_result(f"{FIRST_LINE_ZQ}\n↩2 {SECOND_LINE_ZQ}")),
        quote_enabled=True,
        planner=FixedPlanner(rig),
    ).run(contact.id, later.id)

    first, second = await _rows(rig, contact)
    assert (first.reply_to_tg_message_id, second.reply_to_tg_message_id) == (
        None,
        earlier.tg_message_id,
    )


@pytest.mark.parametrize("phase", list(WrapUpPhase))
async def test_the_wrapup_path_never_uses_the_late_rule(
    rig: AutopilotRig, phase: WrapUpPhase
) -> None:
    contact = await rig.contact()
    trigger = await rig.say(contact, ago=LATE * 2)
    soon = UnavailableSoon(
        kind=BlockKind.NIGHT,
        starts_at=rig.clock() + timedelta(minutes=10),
        minutes_left=10,
        phase=phase,
    )
    planner = FixedPlanner(rig, soon=soon)

    result = await rig.runner(
        SequenceLLMClient(llm_result()), quote_enabled=True, planner=planner, wrapup_enabled=True
    ).run(contact.id, trigger.id)

    [row] = await _rows(rig, contact)
    assert result.outcome is ReplyJobOutcome.QUEUED
    assert row.reply_to_tg_message_id is None
    assert planner.late_reads == 0


async def test_the_wrapup_path_still_honours_a_model_marker(rig: AutopilotRig) -> None:
    contact, earlier, later = await _two_waiting(rig)
    soon = UnavailableSoon(
        kind=BlockKind.NIGHT,
        starts_at=rig.clock() + timedelta(minutes=30),
        minutes_left=30,
        phase=WrapUpPhase.SOFT,
    )

    await rig.runner(
        SequenceLLMClient(llm_result(f"↩2 {FIRST_LINE_ZQ}")),
        quote_enabled=True,
        planner=FixedPlanner(rig, soon=soon),
        wrapup_enabled=True,
    ).run(contact.id, later.id)

    [row] = await _rows(rig, contact)
    assert row.reply_to_tg_message_id == earlier.tg_message_id


async def test_an_adopted_reply_is_quoted_from_the_newest_message(rig: AutopilotRig) -> None:
    contact = await rig.contact()
    first = await rig.say(contact, ago=LATE * 4)
    newest = await rig.say(contact, ago=LATE * 2)

    result = await rig.runner(
        SequenceLLMClient(llm_result()),
        quote_enabled=True,
        planner=FixedPlanner(rig),
        max_burst_wait_seconds=60,
    ).run(contact.id, first.id)

    [row] = await _rows(rig, contact)
    assert result.outcome is ReplyJobOutcome.QUEUED
    assert row.trigger_message_id == newest.id
    assert row.reply_to_tg_message_id == newest.tg_message_id


async def test_a_retried_job_keeps_the_stored_chain_and_its_target(rig: AutopilotRig) -> None:
    contact, earlier, later = await _two_waiting(rig)
    client = SequenceLLMClient(llm_result(f"↩2 {FIRST_LINE_ZQ}\n{SECOND_LINE_ZQ}"))

    await rig.runner(client, quote_enabled=True).run(contact.id, later.id)
    again = await rig.runner(client, quote_enabled=True).run(contact.id, later.id, attempt=2)

    rows = await _rows(rig, contact)
    assert (again.outcome, again.reason) == (ReplyJobOutcome.SKIPPED, ReplyJobReason.ROW_EXISTS)
    assert [row.reply_to_tg_message_id for row in rows] == [earlier.tg_message_id, None]
    assert client.calls == 1


async def test_a_marker_only_reply_is_refused_at_the_enqueue(rig: AutopilotRig) -> None:
    contact, _, later = await _two_waiting(rig)

    result = await rig.runner(SequenceLLMClient(llm_result("↩2")), quote_enabled=True).run(
        contact.id, later.id
    )

    assert (result.outcome, result.reason) == (
        ReplyJobOutcome.SKIPPED,
        ReplyJobReason.ENQUEUE_REFUSED,
    )
    assert await _rows(rig, contact) == []


async def test_the_logs_carry_the_flag_and_never_a_text_an_id_or_a_marker(
    rig: AutopilotRig, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    contact = await rig.contact()
    earlier = await _say_with_id(rig, contact, EARLIER_TEXT_ZQ, LONG_EARLIER_TG_MESSAGE_ID, 60)
    later = await _say_with_id(rig, contact, LATER_TEXT_ZQ, LONG_LATER_TG_MESSAGE_ID, 40)

    await rig.runner(
        SequenceLLMClient(llm_result(f"↩2 {FIRST_LINE_ZQ}\n{SECOND_LINE_ZQ}")), quote_enabled=True
    ).run(contact.id, later.id)

    [record] = [
        record
        for record in caplog.records
        if record.name == JOB_LOGGER and record.getMessage() == "reply job finished"
    ]
    assert record.__dict__["quoted"] is True
    logs = json_logs(caplog)
    for marker in (EARLIER_TEXT_ZQ, LATER_TEXT_ZQ, FIRST_LINE_ZQ, SECOND_LINE_ZQ, "↩"):
        assert_absent(marker, logs)
    assert_absent(earlier.tg_message_id, logs)
    assert_absent(later.tg_message_id, logs)
    assert_absent(contact.tg_id, logs)
