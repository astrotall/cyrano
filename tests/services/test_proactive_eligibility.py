from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from cyrano.domain.enums import (
    BlockKind,
    ContactStatus,
    MessageDirection,
    ProactiveReason,
    WrapUpPhase,
)
from cyrano.domain.proactive import DialogueTail, ProactiveSnapshot
from cyrano.domain.schedule import UnavailableSoon
from cyrano.services.proactive_eligibility import (
    decide_proactive,
    silence_started_at,
    switch_reason,
)
from tests.services.proactive_data import (
    HOUR,
    INCOMING_ID,
    NOW,
    OUTGOING_ID,
    Rules,
    autopilot_state,
    contact,
    eligible_snapshot,
    message,
    sender_state,
)

MINUTE = timedelta(minutes=1)
UNAVAILABLE_CASES = [
    UnavailableSoon(
        kind=BlockKind.NIGHT,
        starts_at=NOW + 50 * MINUTE,
        minutes_left=50,
        phase=WrapUpPhase.SOFT,
    ),
    UnavailableSoon(
        kind=BlockKind.BUSY,
        starts_at=NOW + 5 * MINUTE,
        minutes_left=5,
        phase=WrapUpPhase.HARD,
        label="invented-class-zq",
    ),
]
BASE = eligible_snapshot()
LAST = message(OUTGOING_ID, MessageDirection.OUTGOING, NOW - 7 * HOUR)
INCOMING = message(INCOMING_ID, MessageDirection.INCOMING, NOW - 8 * HOUR)
SINGLE_FAILURES: list[tuple[ProactiveSnapshot, Rules, ProactiveReason]] = [
    (BASE, Rules(proactive_enabled=False), ProactiveReason.PROACTIVE_DISABLED),
    (BASE, Rules(autopilot_enabled=False), ProactiveReason.AUTOPILOT_DISABLED),
    (
        replace(BASE, autopilot=autopilot_state(stopped=True)),
        Rules(),
        ProactiveReason.AUTOPILOT_STOPPED,
    ),
    (
        replace(BASE, autopilot=autopilot_state(paused_until=NOW + HOUR)),
        Rules(),
        ProactiveReason.AUTOPILOT_PAUSED,
    ),
    (replace(BASE, sender=sender_state(stopped=True)), Rules(), ProactiveReason.SENDER_STOPPED),
    (
        replace(BASE, sender=sender_state(paused_until=NOW + HOUR)),
        Rules(),
        ProactiveReason.SENDER_PAUSED,
    ),
    *[
        (replace(BASE, unavailable=soon), Rules(), ProactiveReason.UNAVAILABLE_SOON)
        for soon in UNAVAILABLE_CASES
    ],
    (replace(BASE, contact=None), Rules(), ProactiveReason.CONTACT_MISSING),
    *[
        (replace(BASE, contact=contact(status)), Rules(), ProactiveReason.CONTACT_NOT_ACTIVE)
        for status in ContactStatus
        if status is not ContactStatus.ACTIVE
    ],
    (
        replace(BASE, contact=contact(not_before=NOW + timedelta(seconds=1))),
        Rules(),
        ProactiveReason.POSTPONED,
    ),
    (
        replace(BASE, tail=DialogueTail(last=LAST, last_incoming=None)),
        Rules(),
        ProactiveReason.NO_INCOMING,
    ),
    (
        replace(BASE, tail=DialogueTail(last=None, last_incoming=None)),
        Rules(),
        ProactiveReason.NO_INCOMING,
    ),
    (
        replace(
            BASE,
            tail=DialogueTail(
                last=message(OUTGOING_ID, MessageDirection.INCOMING, LAST.created_at),
                last_incoming=INCOMING,
            ),
        ),
        Rules(),
        ProactiveReason.LAST_MESSAGE_INCOMING,
    ),
    (
        eligible_snapshot(silence=6 * HOUR - timedelta(seconds=1)),
        Rules(),
        ProactiveReason.SILENCE_TOO_SHORT,
    ),
    (
        replace(BASE, contact=contact(reply_after=NOW - 5 * HOUR)),
        Rules(),
        ProactiveReason.SILENCE_TOO_SHORT,
    ),
    (replace(BASE, silence_rows=2), Rules(), ProactiveReason.SILENCE_LIMIT_REACHED),
    (
        replace(BASE, newest_row_due_at=NOW - 24 * HOUR + timedelta(seconds=1)),
        Rules(),
        ProactiveReason.MIN_INTERVAL,
    ),
    (
        replace(BASE, newest_row_due_at=NOW + HOUR),
        Rules(),
        ProactiveReason.MIN_INTERVAL,
    ),
    (replace(BASE, daily_rows=20), Rules(), ProactiveReason.DAILY_LIMIT),
]


def test_all_conditions_true_is_eligible() -> None:
    assert decide_proactive(BASE, Rules()) is ProactiveReason.ELIGIBLE


@pytest.mark.parametrize(
    ("snapshot", "rules", "reason"),
    SINGLE_FAILURES,
    ids=[f"{case[2].value}-{index}" for index, case in enumerate(SINGLE_FAILURES)],
)
def test_each_condition_alone_failing_gives_exactly_its_reason(
    snapshot: ProactiveSnapshot, rules: Rules, reason: ProactiveReason
) -> None:
    assert decide_proactive(snapshot, rules) is reason


def test_every_reason_but_eligible_is_reachable_alone() -> None:
    reached = {case[2] for case in SINGLE_FAILURES}

    assert reached == set(ProactiveReason) - {ProactiveReason.ELIGIBLE}


def test_a_silence_of_exactly_the_threshold_is_enough() -> None:
    assert (
        decide_proactive(eligible_snapshot(silence=6 * HOUR), Rules()) is ProactiveReason.ELIGIBLE
    )


@pytest.mark.parametrize("after_hours", [1, 6, 72])
def test_the_threshold_follows_the_setting(after_hours: int) -> None:
    rules = Rules(proactive_after_hours=after_hours)
    exactly = eligible_snapshot(silence=after_hours * HOUR)
    short = eligible_snapshot(silence=after_hours * HOUR - timedelta(seconds=1))

    assert decide_proactive(exactly, rules) is ProactiveReason.ELIGIBLE
    assert decide_proactive(short, rules) is ProactiveReason.SILENCE_TOO_SHORT


def test_the_silence_counts_from_the_later_of_the_last_message_and_reply_after() -> None:
    resumed = replace(BASE, contact=contact(reply_after=NOW - 6 * HOUR))
    earlier = replace(BASE, contact=contact(reply_after=NOW - 30 * HOUR))

    assert silence_started_at(resumed) == NOW - 6 * HOUR
    assert decide_proactive(resumed, Rules()) is ProactiveReason.ELIGIBLE
    assert silence_started_at(earlier) == LAST.created_at
    assert decide_proactive(earlier, Rules()) is ProactiveReason.ELIGIBLE


def test_a_postponement_that_has_passed_does_not_block() -> None:
    passed = replace(BASE, contact=contact(not_before=NOW))

    assert decide_proactive(passed, Rules()) is ProactiveReason.ELIGIBLE


@pytest.mark.parametrize(("rows", "maximum"), [(0, 1), (1, 2), (4, 5)])
def test_one_row_below_the_maximum_is_still_eligible(rows: int, maximum: int) -> None:
    rules = Rules(proactive_max_per_silence=maximum)

    assert decide_proactive(replace(BASE, silence_rows=rows), rules) is ProactiveReason.ELIGIBLE
    assert (
        decide_proactive(replace(BASE, silence_rows=rows + 1), rules)
        is ProactiveReason.SILENCE_LIMIT_REACHED
    )


def test_the_minimum_interval_counts_exactly_the_setting() -> None:
    exactly = replace(BASE, newest_row_due_at=NOW - 24 * HOUR)

    assert decide_proactive(exactly, Rules()) is ProactiveReason.ELIGIBLE
    assert (
        decide_proactive(
            replace(BASE, newest_row_due_at=NOW - 3 * HOUR), Rules(proactive_min_interval_hours=3)
        )
        is ProactiveReason.ELIGIBLE
    )


def test_the_daily_cap_counts_below_the_limit() -> None:
    assert decide_proactive(replace(BASE, daily_rows=19), Rules()) is ProactiveReason.ELIGIBLE
    assert decide_proactive(replace(BASE, daily_rows=1), Rules(proactive_daily_limit=1)) is (
        ProactiveReason.DAILY_LIMIT
    )


def test_the_switches_are_checked_before_anything_else() -> None:
    broken = replace(BASE, contact=None, daily_rows=999, sender=sender_state(stopped=True))

    assert (
        decide_proactive(broken, Rules(proactive_enabled=False))
        is ProactiveReason.PROACTIVE_DISABLED
    )
    assert (
        decide_proactive(broken, Rules(autopilot_enabled=False))
        is ProactiveReason.AUTOPILOT_DISABLED
    )
    assert switch_reason(Rules()) is None


def test_an_expired_autopilot_or_sender_pause_does_not_block() -> None:
    expired = replace(
        BASE, autopilot=autopilot_state(paused_until=NOW), sender=sender_state(paused_until=NOW)
    )

    assert decide_proactive(expired, Rules()) is ProactiveReason.ELIGIBLE


def test_the_base_snapshot_holds_the_same_messages_as_the_cases() -> None:
    assert BASE.tail == DialogueTail(last=LAST, last_incoming=INCOMING)
