from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from cyrano.domain.enums import (
    AutopilotPauseReason,
    AutopilotStopReason,
    ContactStatus,
    MessageDirection,
    OutboxSource,
    OutboxState,
    PauseReason,
    PlanReason,
    ProactiveJobOutcome,
    ProactiveJobReason,
    ProactiveReason,
    ReplyJobOutcome,
    ReplyJobReason,
)
from cyrano.domain.errors import (
    LLMAuthError,
    LLMInvalidResponseError,
    LLMRateLimitError,
    LLMRequestError,
    LLMUnavailableError,
    RetryJob,
)
from cyrano.domain.llm import LLMMessage, LLMResult
from cyrano.domain.models import Contact
from cyrano.domain.proactive import FAILURE_POSTPONE_SECONDS, MODEL_SKIP_POSTPONE_SECONDS
from cyrano.domain.schedule import ScheduleProfile
from cyrano.prompts.texts import SKIP_SIGNAL
from cyrano.services.humanization import ScheduleService, blocked, in_proactive_window
from tests.privacy import assert_absent
from tests.services.autopilot_rig import (
    CONTACT_NAME_ZQ,
    CONTACT_USERNAME_ZQ,
    INCOMING_TEXT_ZQ,
    INVALID_REPLY,
    NEEDS_OWNER_REPLY,
    PERSONA_TEXT_ZQ,
    AutopilotRig,
    SequenceLLMClient,
    json_logs,
    llm_result,
)
from tests.services.outbox_fakes import ALWAYS_FREE_PROFILE, InMemoryScheduleProfiles
from tests.services.proactive_rig import FOLLOW_UP_TEXT_ZQ, OUR_TEXT_ZQ, ProactiveRig

HOUR = timedelta(hours=1)
DELETE_PERSONA = "DELETE FROM persona"
TWO_PARTS = f"{FOLLOW_UP_TEXT_ZQ}\nand the invented second line"


def _schedule(profile: ScheduleProfile) -> ScheduleService:
    return ScheduleService(InMemoryScheduleProfiles(profile), "UTC")


async def _not_before(prig: ProactiveRig, contact: Contact) -> datetime | None:
    return (await prig.rig.reload(contact)).proactive_not_before


class _Watching(SequenceLLMClient):
    def __init__(self, *steps: LLMResult | Exception) -> None:
        super().__init__(*steps)
        self.seen: list[str] = []

    async def complete(
        self,
        messages: Sequence[LLMMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResult:
        self.seen.extend(message.content for message in messages)
        return await super().complete(messages, temperature=temperature, max_tokens=max_tokens)


async def test_a_normal_run_writes_one_proactive_chain_inside_the_window(
    prig: ProactiveRig,
) -> None:
    contact, last = await prig.silent_contact()
    client = SequenceLLMClient(llm_result(FOLLOW_UP_TEXT_ZQ))

    result = await prig.runner(client).run(contact.id, last.id)

    [row] = await prig.rig.outbox.list_rows(None, 10)
    now = prig.rig.clock()
    assert result.outcome is ProactiveJobOutcome.QUEUED
    assert result.reason is ProactiveJobReason.QUEUED
    assert result.detail == PlanReason.DELAYED.value
    assert (result.row_id, result.parts) == (row.id, 1)
    assert row.source is OutboxSource.PROACTIVE
    assert row.trigger_message_id == last.id
    assert row.text == FOLLOW_UP_TEXT_ZQ
    assert row.state is OutboxState.SCHEDULED
    assert now < row.due_at == row.send_at <= now + timedelta(seconds=120)
    assert in_proactive_window(row.due_at, ALWAYS_FREE_PROFILE)
    assert await _not_before(prig, contact) == now + 24 * HOUR
    assert client.calls == 1


async def test_a_proactive_row_never_carries_a_reply_target(prig: ProactiveRig) -> None:
    contact, last = await prig.silent_contact()
    client = _Watching(llm_result(f"↩1 {FOLLOW_UP_TEXT_ZQ}"))

    await prig.runner(client, quote_enabled=True).run(contact.id, last.id)

    rows = await prig.rig.outbox.list_rows(None, 10)
    assert [row.reply_to_tg_message_id for row in rows] == [None]
    system, user = client.seen
    assert "#1 " not in user
    assert "The messages of the dialogue are numbered." not in system


async def test_the_model_receives_the_proactive_prompt(prig: ProactiveRig) -> None:
    contact, last = await prig.silent_contact()
    client = _Watching(llm_result(FOLLOW_UP_TEXT_ZQ))

    await prig.runner(client).run(contact.id, last.id)

    system, user = client.seen
    assert "The contact has not written for " in system
    assert " since the last message of the dialogue." in system
    assert f"reply with exactly {SKIP_SIGNAL}" in user.splitlines()[-1]


async def test_a_multi_line_follow_up_becomes_one_chain(prig: ProactiveRig) -> None:
    contact, last = await prig.silent_contact()

    result = await prig.runner(SequenceLLMClient(llm_result(TWO_PARTS))).run(contact.id, last.id)

    first, second = sorted(await prig.rig.outbox.list_rows(None, 10), key=lambda row: row.id)
    assert result.parts == 2
    assert first.trigger_message_id == last.id
    assert second.trigger_message_id is None
    assert second.follows_id == first.id
    assert {first.source, second.source} == {OutboxSource.PROACTIVE}


async def test_a_duplicate_run_writes_nothing_and_asks_the_model_once(prig: ProactiveRig) -> None:
    contact, last = await prig.silent_contact()
    client = SequenceLLMClient(llm_result(FOLLOW_UP_TEXT_ZQ))
    runner = prig.runner(client)

    await runner.run(contact.id, last.id)
    again = await runner.run(contact.id, last.id)

    assert again.outcome is ProactiveJobOutcome.SKIPPED
    assert again.reason is ProactiveJobReason.POSTPONED
    assert len(await prig.rig.outbox.list_rows(None, 10)) == 1
    assert client.calls == 1


async def test_a_row_for_the_same_last_message_is_a_skip_before_the_model(
    rig: AutopilotRig,
) -> None:
    prig = ProactiveRig(rig, replace(ProactiveRig(rig).rules, proactive_min_interval_hours=1))
    contact, last = await prig.silent_contact()
    client = SequenceLLMClient(llm_result(FOLLOW_UP_TEXT_ZQ))
    await prig.runner(client).run(contact.id, last.id)
    prig.later(3)

    again = await prig.runner(client).run(contact.id, last.id)

    assert again.reason is ProactiveJobReason.ROW_EXISTS
    assert len(await prig.rig.outbox.list_rows(None, 10)) == 1
    assert client.calls == 1


async def test_a_job_for_an_older_last_message_skips_as_superseded(prig: ProactiveRig) -> None:
    contact, older = await prig.silent_contact(8)
    await prig.rig.say(contact, ago=7 * 3600, text=OUR_TEXT_ZQ, direction=MessageDirection.OUTGOING)
    client = SequenceLLMClient(llm_result(FOLLOW_UP_TEXT_ZQ))

    result = await prig.runner(client).run(contact.id, older.id)

    assert result.reason is ProactiveJobReason.SUPERSEDED
    assert await prig.rig.outbox.list_rows(None, 10) == []
    assert client.calls == 0


async def test_a_new_incoming_message_makes_the_job_skip(prig: ProactiveRig) -> None:
    contact, last = await prig.silent_contact()
    await prig.rig.say(contact, ago=60)
    client = SequenceLLMClient(llm_result(FOLLOW_UP_TEXT_ZQ))

    result = await prig.runner(client).run(contact.id, last.id)

    assert result.reason is ProactiveJobReason.LAST_MESSAGE_INCOMING
    assert client.calls == 0


@pytest.mark.parametrize("raw", [SKIP_SIGNAL, "skip.", "`SKIP`", f"{FOLLOW_UP_TEXT_ZQ} SKIP"])
async def test_a_model_skip_postpones_the_next_check_by_twelve_hours_and_writes_no_row(
    prig: ProactiveRig, raw: str
) -> None:
    contact, last = await prig.silent_contact()

    result = await prig.runner(SequenceLLMClient(llm_result(raw))).run(contact.id, last.id)

    assert result.outcome is ProactiveJobOutcome.SKIPPED
    assert result.reason is ProactiveJobReason.MODEL_SKIP
    assert await prig.rig.outbox.list_rows(None, 10) == []
    assert await _not_before(prig, contact) == prig.rig.clock() + timedelta(
        seconds=MODEL_SKIP_POSTPONE_SECONDS
    )
    assert MODEL_SKIP_POSTPONE_SECONDS == 12 * 3600


async def test_a_model_skip_keeps_the_contact_out_until_the_postponement_ends(
    prig: ProactiveRig,
) -> None:
    prig.rig.clock.now = datetime(2026, 11, 4, 3, 0, tzinfo=UTC)
    contact, last = await prig.silent_contact()
    client = SequenceLLMClient(llm_result(SKIP_SIGNAL), llm_result(FOLLOW_UP_TEXT_ZQ))
    await prig.runner(client).run(contact.id, last.id)

    prig.later(11.9)
    blocked_run = await prig.runner(client).run(contact.id, last.id)
    prig.later(0.1)
    allowed_run = await prig.runner(client).run(contact.id, last.id)

    assert blocked_run.reason is ProactiveJobReason.POSTPONED
    assert allowed_run.outcome is ProactiveJobOutcome.QUEUED


async def test_needs_owner_hands_the_contact_over_and_writes_no_row(prig: ProactiveRig) -> None:
    contact, last = await prig.silent_contact()

    result = await prig.runner(SequenceLLMClient(llm_result(NEEDS_OWNER_REPLY))).run(
        contact.id, last.id
    )

    stored = await prig.rig.reload(contact)
    assert result.outcome is ProactiveJobOutcome.HANDED_OVER
    assert result.reason is ProactiveJobReason.NEEDS_OWNER
    assert stored.status is ContactStatus.PAUSED
    assert stored.pause_reason is PauseReason.STOP_TRIGGER
    assert len(prig.rig.hand_over_notifier.notices) == 1
    assert prig.rig.hand_over_notifier.notices[0].classes == ()
    assert await prig.rig.outbox.list_rows(None, 10) == []


async def test_a_truncated_reply_is_generated_again_with_twice_the_tokens(
    prig: ProactiveRig,
) -> None:
    contact, last = await prig.silent_contact()
    client = SequenceLLMClient(
        llm_result(FOLLOW_UP_TEXT_ZQ, truncated=True), llm_result(FOLLOW_UP_TEXT_ZQ)
    )

    result = await prig.runner(client).run(contact.id, last.id)

    assert result.outcome is ProactiveJobOutcome.QUEUED
    assert client.max_tokens == [None, 800]


@pytest.mark.parametrize(
    ("steps", "reason"),
    [
        (
            (llm_result(FOLLOW_UP_TEXT_ZQ, truncated=True),),
            ProactiveJobReason.TRUNCATED,
        ),
        ((llm_result(INVALID_REPLY),), ProactiveJobReason.REPLY_INVALID),
    ],
)
async def test_a_second_bad_reply_hands_over_like_the_reply_job(
    prig: ProactiveRig, steps: tuple[LLMResult, ...], reason: ProactiveJobReason
) -> None:
    contact, last = await prig.silent_contact()
    client = SequenceLLMClient(*steps)

    result = await prig.runner(client).run(contact.id, last.id)

    assert result.outcome is ProactiveJobOutcome.HANDED_OVER
    assert result.reason is reason
    assert client.calls == 2
    assert (await prig.rig.reload(contact)).status is ContactStatus.PAUSED
    assert await prig.rig.outbox.list_rows(None, 10) == []


async def test_a_prompt_too_large_hands_over_without_a_model_call(prig: ProactiveRig) -> None:
    contact, last = await prig.silent_contact()
    client = SequenceLLMClient(llm_result(FOLLOW_UP_TEXT_ZQ))

    result = await prig.runner(client, budget=10).run(contact.id, last.id)

    assert result.outcome is ProactiveJobOutcome.HANDED_OVER
    assert result.reason is ProactiveJobReason.PROMPT_TOO_LARGE
    assert client.calls == 0


async def test_a_missing_persona_stops_the_autopilot_and_postpones_the_contact(
    prig: ProactiveRig,
) -> None:
    contact, last = await prig.silent_contact()
    async with prig.rig.session_factory.begin() as session:
        await session.execute(text(DELETE_PERSONA))

    result = await prig.runner(SequenceLLMClient(llm_result(FOLLOW_UP_TEXT_ZQ))).run(
        contact.id, last.id
    )

    state = await prig.rig.state.get()
    assert result.outcome is ProactiveJobOutcome.STOPPED
    assert result.reason is ProactiveJobReason.PERSONA_MISSING
    assert state.stopped is True
    assert state.stop_reason is AutopilotStopReason.PERSONA_MISSING
    assert len(prig.rig.notifier.texts) == 1
    assert await _not_before(prig, contact) == prig.rig.clock() + timedelta(
        seconds=FAILURE_POSTPONE_SECONDS
    )


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (LLMAuthError("auth", status_code=401), AutopilotStopReason.PROVIDER_AUTH),
        (LLMRequestError("balance", status_code=402), AutopilotStopReason.PROVIDER_REQUEST),
    ],
)
async def test_a_provider_stop_follows_the_autopilot_policy_and_postpones_one_hour(
    prig: ProactiveRig, error: Exception, reason: AutopilotStopReason
) -> None:
    contact, last = await prig.silent_contact()

    result = await prig.runner(SequenceLLMClient(error)).run(contact.id, last.id)

    assert result.outcome is ProactiveJobOutcome.STOPPED
    assert result.reason.value == reason.value
    assert result.detail == type(error).__name__
    assert (await prig.rig.state.get()).stop_reason is reason
    assert await _not_before(prig, contact) == prig.rig.clock() + HOUR


@pytest.mark.parametrize(
    "error",
    [
        LLMUnavailableError("unavailable", status_code=503),
        LLMRateLimitError("rate", status_code=429),
        LLMInvalidResponseError("invalid"),
        RuntimeError("invented unexpected failure"),
    ],
)
async def test_a_transient_failure_is_retried_on_the_schedule_without_a_postponement(
    prig: ProactiveRig, error: Exception
) -> None:
    contact, last = await prig.silent_contact()
    runner = prig.runner(SequenceLLMClient(error))

    delays = []
    for attempt in (1, 2, 3):
        with pytest.raises(RetryJob) as raised:
            await runner.run(contact.id, last.id, attempt=attempt)
        delays.append(raised.value.defer_seconds)

    assert delays == [60, 300, 900]
    assert await _not_before(prig, contact) is None
    assert (await prig.rig.state.get()).is_running(prig.rig.clock())


async def test_the_last_try_pauses_the_autopilot_and_postpones_the_contact(
    prig: ProactiveRig,
) -> None:
    contact, last = await prig.silent_contact()

    result = await prig.runner(
        SequenceLLMClient(LLMUnavailableError("unavailable", status_code=503))
    ).run(contact.id, last.id, attempt=4)

    state = await prig.rig.state.get()
    assert result.outcome is ProactiveJobOutcome.PAUSED
    assert result.reason is ProactiveJobReason.PROVIDER_UNAVAILABLE
    assert state.pause_reason is AutopilotPauseReason.PROVIDER_UNAVAILABLE
    assert await _not_before(prig, contact) == prig.rig.clock() + HOUR
    assert await prig.rig.outbox.list_rows(None, 10) == []


async def test_an_ineligible_contact_is_skipped_with_the_eligibility_reason(
    rig: AutopilotRig,
) -> None:
    prig = ProactiveRig(rig, replace(ProactiveRig(rig).rules, proactive_enabled=False))
    contact, last = await prig.silent_contact()
    client = SequenceLLMClient(llm_result(FOLLOW_UP_TEXT_ZQ))

    result = await prig.runner(client).run(contact.id, last.id)

    assert result.reason is ProactiveJobReason.PROACTIVE_DISABLED
    assert client.calls == 0


async def test_a_contact_that_became_too_silent_after_the_scan_is_refused_and_left_alone(
    prig: ProactiveRig,
) -> None:
    contact, last = await prig.silent_contact()
    assert (await prig.eligibility().check(contact.id)).eligible
    prig.later(70)
    client = SequenceLLMClient(llm_result(FOLLOW_UP_TEXT_ZQ))

    result = await prig.runner(client).run(contact.id, last.id)

    assert result.outcome is ProactiveJobOutcome.SKIPPED
    assert result.reason is ProactiveJobReason.SILENCE_TOO_LONG
    assert client.calls == 0
    assert await prig.rig.outbox.list_rows(None, 10) == []
    assert await _not_before(prig, contact) is None


def test_the_job_has_a_reason_for_every_reason_of_the_rule() -> None:
    assert {reason.value for reason in ProactiveReason} - {ProactiveReason.ELIGIBLE.value} <= {
        reason.value for reason in ProactiveJobReason
    }


async def test_a_guard_refusal_at_enqueue_is_a_skip_with_its_reason(prig: ProactiveRig) -> None:
    contact, last = await prig.silent_contact()
    prig.rig.guard_switch.proactive_enabled = False

    result = await prig.runner(SequenceLLMClient(llm_result(FOLLOW_UP_TEXT_ZQ))).run(
        contact.id, last.id
    )

    assert result.reason is ProactiveJobReason.ENQUEUE_REFUSED
    assert result.detail == "proactive_disabled"
    assert await prig.rig.outbox.list_rows(None, 10) == []
    assert await _not_before(prig, contact) is None


NIGHT_AND_CLASS_PROFILE = ScheduleProfile.model_validate(
    {
        "version": 1,
        "timezone": "UTC",
        "busy": [{"days": ["wed"], "from": "10:00", "to": "13:00"}],
    }
)


@pytest.mark.parametrize(
    ("moment", "reason"),
    [
        (datetime(2026, 11, 4, 15, 0, tzinfo=UTC), PlanReason.DELAYED),
        (datetime(2026, 11, 4, 11, 0, tzinfo=UTC), PlanReason.POSTPONED_BUSY),
        (datetime(2026, 11, 4, 22, 30, tzinfo=UTC), PlanReason.POSTPONED_WINDOW),
        (datetime(2026, 11, 7, 3, 0, tzinfo=UTC), PlanReason.POSTPONED_NIGHT),
    ],
)
async def test_the_job_plans_inside_the_window_and_outside_the_blocked_windows(
    prig: ProactiveRig, moment: datetime, reason: PlanReason
) -> None:
    prig.rig.clock.now = moment
    contact, last = await prig.silent_contact()

    result = await prig.runner(
        SequenceLLMClient(llm_result(FOLLOW_UP_TEXT_ZQ)),
        schedule=_schedule(NIGHT_AND_CLASS_PROFILE),
    ).run(contact.id, last.id)

    [row] = await prig.rig.outbox.list_rows(None, 10)
    assert result.detail == reason.value
    assert in_proactive_window(row.due_at, NIGHT_AND_CLASS_PROFILE)
    assert blocked(row.due_at, NIGHT_AND_CLASS_PROFILE) is None


async def test_a_skip_in_a_normal_reply_is_treated_as_an_invalid_reply(rig: AutopilotRig) -> None:
    contact = await rig.contact()
    trigger = await rig.say(contact, ago=30)
    client = SequenceLLMClient(llm_result(SKIP_SIGNAL))

    result = await rig.runner(client).run(contact.id, trigger.id)

    assert result.outcome is ReplyJobOutcome.HANDED_OVER
    assert result.reason is ReplyJobReason.REPLY_INVALID
    assert client.calls == 2
    assert await rig.outbox.list_rows(None, 10) == []


async def test_one_info_line_per_job_with_ids_and_no_text(
    prig: ProactiveRig, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    contact, last = await prig.silent_contact()

    await prig.runner(SequenceLLMClient(llm_result(FOLLOW_UP_TEXT_ZQ))).run(contact.id, last.id)
    await prig.runner(SequenceLLMClient(llm_result(SKIP_SIGNAL))).run(contact.id, last.id)

    lines = [r for r in caplog.records if r.getMessage() == "proactive job finished"]
    assert [r.levelno for r in lines] == [logging.INFO, logging.INFO]
    assert lines[0].__dict__["contact_id"] == contact.id
    assert lines[0].__dict__["last_message_id"] == last.id
    assert lines[0].__dict__["outcome"] == "queued"
    assert lines[0].__dict__["parts"] == 1
    assert lines[1].__dict__["reason"] == "postponed"
    output = json_logs(caplog) + "\n".join(prig.rig.notifier.texts)
    for marker in (
        FOLLOW_UP_TEXT_ZQ,
        OUR_TEXT_ZQ,
        INCOMING_TEXT_ZQ,
        PERSONA_TEXT_ZQ,
        CONTACT_NAME_ZQ,
        CONTACT_USERNAME_ZQ,
        contact.tg_id,
    ):
        assert_absent(marker, output)
