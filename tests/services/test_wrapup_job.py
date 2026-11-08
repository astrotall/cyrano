from __future__ import annotations

import logging
import random
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest

from cyrano.domain.enums import MessageDirection, PlanReason, ReplyJobOutcome, ReplyJobReason
from cyrano.domain.errors import RetryJob, StoredScheduleInvalid
from cyrano.domain.llm import LLMMessage, LLMResult
from cyrano.domain.models import Contact, Message
from cyrano.domain.schedule import ScheduleProfile
from cyrano.services.autopilot import AutopilotSweeper
from cyrano.services.humanization import HumanizationPlanner, ScheduleService
from tests.privacy import assert_absent
from tests.services.autopilot_fakes import RecordingQueue
from tests.services.autopilot_rig import (
    INCOMING_TEXT_ZQ,
    AutopilotRig,
    SequenceLLMClient,
    json_logs,
    llm_result,
)

HARD_TIME = datetime(2026, 11, 4, 23, 45, tzinfo=UTC)
SOFT_TIME = datetime(2026, 11, 4, 23, 30, tzinfo=UTC)
NIGHT_START = datetime(2026, 11, 5, 0, 0, tzinfo=UTC)
GOODNIGHT_ZQ = "goodnight-marker-zq спокойной ночи"
WRAPUP_LINE_START = "The owner becomes unavailable in about"
DEBOUNCE = 12


class _Watching(SequenceLLMClient):
    def __init__(self, *steps: LLMResult | Exception) -> None:
        super().__init__(*steps)
        self.users: list[str] = []
        self.systems: list[str] = []

    async def complete(
        self,
        messages: Sequence[LLMMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResult:
        self.systems.append(messages[0].content)
        self.users.append(messages[1].content)
        return await super().complete(messages, temperature=temperature, max_tokens=max_tokens)


class _BrokenProfiles:
    async def get(self) -> ScheduleProfile | None:
        raise StoredScheduleInvalid

    async def replace(self, profile: ScheduleProfile) -> ScheduleProfile:
        return profile


async def _incoming(rig: AutopilotRig, at: datetime, ago: float = 0) -> tuple[Contact, Message]:
    rig.clock.now = at
    contact = await rig.contact()
    await rig.say(contact, ago=600, direction=MessageDirection.OUTGOING)
    trigger = await rig.say(contact, ago=ago)
    return contact, trigger


def _sweeper(rig: AutopilotRig, queue: RecordingQueue) -> AutopilotSweeper:
    return AutopilotSweeper(
        rig.backlog, rig.state, queue, rig.switch, debounce_seconds=DEBOUNCE, clock=rig.clock
    )


async def test_a_skip_near_the_night_writes_no_row_and_marks_the_message_handled(
    rig: AutopilotRig, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    contact, trigger = await _incoming(rig, HARD_TIME, ago=30)
    client = _Watching(llm_result("SKIP"))

    result = await rig.runner(client, wrapup_enabled=True).run(contact.id, trigger.id)

    assert result.outcome is ReplyJobOutcome.SKIPPED
    assert result.reason is ReplyJobReason.MODEL_SKIP
    assert await rig.outbox.list_rows(None, 10) == []
    assert (await rig.reload(contact)).reply_after == trigger.created_at
    assert client.calls == 1
    [line] = [r for r in caplog.records if r.getMessage() == "reply job finished"]
    assert (line.__dict__["outcome"], line.__dict__["reason"]) == ("skipped", "model_skip")
    assert_absent(INCOMING_TEXT_ZQ, json_logs(caplog))


async def test_the_sweeper_finds_an_unanswered_message_but_not_a_skipped_one(
    rig: AutopilotRig,
) -> None:
    contact, trigger = await _incoming(rig, HARD_TIME, ago=30)
    before = RecordingQueue()
    await _sweeper(rig, before).sweep()

    await rig.runner(SequenceLLMClient(llm_result("SKIP")), wrapup_enabled=True).run(
        contact.id, trigger.id
    )
    after = RecordingQueue()
    await _sweeper(rig, after).sweep()

    assert [job[1] for job in before.jobs] == [trigger.id]
    assert after.jobs == []


async def test_a_skip_never_moves_reply_after_backwards(rig: AutopilotRig) -> None:
    contact, trigger = await _incoming(rig, HARD_TIME, ago=30)
    later = trigger.created_at + timedelta(seconds=10)
    await rig.contacts.set_reply_after(contact.id, later)
    newer = await rig.say(contact, ago=5)

    result = await rig.runner(SequenceLLMClient(llm_result("SKIP")), wrapup_enabled=True).run(
        contact.id, newer.id
    )

    assert result.reason is ReplyJobReason.MODEL_SKIP
    assert (await rig.reload(contact)).reply_after == newer.created_at
    await rig.contacts.set_reply_after(contact.id, trigger.created_at)
    assert (await rig.reload(contact)).reply_after == newer.created_at


async def test_a_skip_far_from_any_window_is_an_invalid_reply(rig: AutopilotRig) -> None:
    contact, trigger = await _incoming(rig, datetime(2026, 11, 4, 15, 0, tzinfo=UTC), ago=30)
    client = SequenceLLMClient(llm_result("SKIP"))

    result = await rig.runner(client, wrapup_enabled=True).run(contact.id, trigger.id)

    assert result.outcome is ReplyJobOutcome.HANDED_OVER
    assert result.reason is ReplyJobReason.REPLY_INVALID
    assert client.calls == 2
    assert (await rig.reload(contact)).reply_after is None


async def test_with_the_switch_off_a_skip_near_the_night_is_an_invalid_reply(
    rig: AutopilotRig,
) -> None:
    contact, trigger = await _incoming(rig, HARD_TIME, ago=30)
    client = _Watching(llm_result("SKIP"))

    result = await rig.runner(client, wrapup_enabled=False).run(contact.id, trigger.id)

    assert result.reason is ReplyJobReason.REPLY_INVALID
    assert all(WRAPUP_LINE_START not in user for user in client.users)


async def test_the_hard_phase_answers_with_the_short_delay_before_the_night(
    rig: AutopilotRig,
) -> None:
    contact, trigger = await _incoming(rig, HARD_TIME)
    client = _Watching(llm_result(GOODNIGHT_ZQ))

    result = await rig.runner(client, wrapup_enabled=True).run(contact.id, trigger.id)

    [row] = await rig.outbox.list_rows(None, 10)
    assert result.detail == PlanReason.ONGOING.value
    assert HARD_TIME + timedelta(seconds=1) <= row.send_at <= HARD_TIME + timedelta(seconds=4)
    assert row.send_at < NIGHT_START - timedelta(seconds=30)
    assert "The owner becomes unavailable in about 15 minutes (sleep)." in client.users[0]
    assert "The owner becomes unavailable in a few minutes." in client.systems[0]


async def test_without_the_context_the_same_message_gets_the_long_delay(
    rig: AutopilotRig,
) -> None:
    contact, trigger = await _incoming(rig, HARD_TIME)

    result = await rig.runner(SequenceLLMClient(llm_result(GOODNIGHT_ZQ))).run(
        contact.id, trigger.id
    )

    assert result.detail == PlanReason.DELAYED.value


async def test_a_reply_that_cannot_fit_before_the_night_is_postponed_after_it(
    rig: AutopilotRig,
) -> None:
    contact, trigger = await _incoming(rig, NIGHT_START - timedelta(seconds=20))

    result = await rig.runner(SequenceLLMClient(llm_result(GOODNIGHT_ZQ)), wrapup_enabled=True).run(
        contact.id, trigger.id
    )

    [row] = await rig.outbox.list_rows(None, 10)
    assert result.detail == PlanReason.POSTPONED_NIGHT.value
    assert row.send_at > NIGHT_START + timedelta(minutes=1)


async def test_the_soft_phase_changes_the_prompt_but_not_the_timing(rig: AutopilotRig) -> None:
    contact, trigger = await _incoming(rig, SOFT_TIME)
    client = _Watching(llm_result(GOODNIGHT_ZQ))
    planner = HumanizationPlanner(rig.messages, rig.schedule, random.Random(5))

    with_context = await rig.runner(client, wrapup_enabled=True, planner=planner).run(
        contact.id, trigger.id
    )
    [row] = await rig.outbox.list_rows(None, 10)
    expected = await HumanizationPlanner(rig.messages, rig.schedule, random.Random(5)).plan(
        contact.id, trigger.id, SOFT_TIME
    )

    assert with_context.detail == PlanReason.DELAYED.value
    assert row.send_at == expected.send_at
    assert "The owner becomes unavailable in about 30 minutes (sleep)." in client.users[0]
    assert "The owner becomes unavailable soon" in client.systems[0]


async def test_an_unreadable_profile_gives_the_old_behaviour(rig: AutopilotRig) -> None:
    contact, trigger = await _incoming(rig, HARD_TIME)
    rig.schedule = ScheduleService(_BrokenProfiles(), "UTC")
    client = _Watching(llm_result(GOODNIGHT_ZQ))

    with pytest.raises(RetryJob):
        await rig.runner(client, wrapup_enabled=True).run(contact.id, trigger.id)

    assert all(WRAPUP_LINE_START not in user for user in client.users)


async def test_inside_the_night_there_is_no_context(rig: AutopilotRig) -> None:
    contact, trigger = await _incoming(rig, NIGHT_START + timedelta(seconds=30))
    client = _Watching(llm_result(GOODNIGHT_ZQ))

    await rig.runner(client, wrapup_enabled=True).run(contact.id, trigger.id)

    assert all(WRAPUP_LINE_START not in user for user in client.users)


async def test_the_autopilot_switch_off_changes_nothing(rig: AutopilotRig) -> None:
    contact, trigger = await _incoming(rig, HARD_TIME)
    rig.switch.autopilot_enabled = False
    client = SequenceLLMClient(llm_result("SKIP"))

    result = await rig.runner(client, wrapup_enabled=True).run(contact.id, trigger.id)

    assert result.reason is ReplyJobReason.SWITCH_OFF
    assert client.calls == 0
    assert (await rig.reload(contact)).reply_after is None
