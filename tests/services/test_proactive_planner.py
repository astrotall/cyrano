from __future__ import annotations

import random
from datetime import date, datetime, timedelta

import pytest

from cyrano.domain.enums import PlanReason
from cyrano.domain.schedule import ScheduleProfile
from cyrano.services.humanization import (
    blocked,
    in_proactive_window,
    plan_proactive_time,
    proactive_window_start_after,
)
from tests.services.schedule_data import (
    ALL_WEEK,
    FRIDAY,
    MONDAY,
    SATURDAY,
    SUNDAY,
    TUESDAY,
    WEDNESDAY,
    at_local,
    in_local,
    profile_with,
)

SEED = 31
CLASS_DAYS = ["tue"]
PROFILE = profile_with(busy=[{"days": CLASS_DAYS, "from": "12:00", "to": "14:00"}])


class _FixedUniform(random.Random):
    def __init__(self, value: float) -> None:
        super().__init__()
        self._value = value

    def uniform(self, a: float, b: float) -> float:
        return self._value


def _assert_sendable(planned: datetime, profile: ScheduleProfile) -> None:
    assert in_proactive_window(planned, profile)
    assert blocked(planned, profile) is None


def test_inside_the_window_the_message_waits_the_new_reaction() -> None:
    now = at_local(WEDNESDAY, 15, 0)
    expected = random.Random(SEED).uniform(20, 120)

    plan = plan_proactive_time(now, PROFILE, random.Random(SEED))

    assert plan.reason is PlanReason.DELAYED
    assert plan.send_at == now + timedelta(seconds=expected)


def test_before_the_window_the_message_waits_for_its_start_plus_the_spread() -> None:
    now = at_local(WEDNESDAY, 9, 30)
    spread = random.Random(SEED).uniform(1, 15)

    plan = plan_proactive_time(now, PROFILE, random.Random(SEED))

    assert plan.reason is PlanReason.POSTPONED_WINDOW
    assert plan.send_at == at_local(WEDNESDAY, 10, 0) + timedelta(minutes=spread)


def test_after_the_window_the_message_waits_for_the_next_day() -> None:
    now = at_local(WEDNESDAY, 22, 0)

    plan = plan_proactive_time(now, PROFILE, _FixedUniform(4))

    assert plan.reason is PlanReason.POSTPONED_WINDOW
    assert plan.send_at == at_local(WEDNESDAY + timedelta(days=1), 10, 4)


def test_a_delay_that_crosses_the_window_end_moves_to_the_next_day() -> None:
    now = at_local(WEDNESDAY, 21, 59, 30)

    plan = plan_proactive_time(now, PROFILE, _FixedUniform(60))

    assert plan.reason is PlanReason.POSTPONED_WINDOW
    assert in_local(plan.send_at).date() == WEDNESDAY + timedelta(days=1)
    _assert_sendable(plan.send_at, PROFILE)


def test_the_window_start_is_inside_and_its_end_is_outside() -> None:
    assert in_proactive_window(at_local(WEDNESDAY, 10, 0), PROFILE)
    assert in_proactive_window(at_local(WEDNESDAY, 21, 59, 59), PROFILE)
    assert not in_proactive_window(at_local(WEDNESDAY, 22, 0), PROFILE)
    assert not in_proactive_window(at_local(WEDNESDAY, 9, 59, 59), PROFILE)


def test_the_next_window_start_is_today_before_it_and_tomorrow_after_it() -> None:
    assert proactive_window_start_after(at_local(WEDNESDAY, 7, 0), PROFILE) == at_local(
        WEDNESDAY, 10, 0
    )
    assert proactive_window_start_after(at_local(WEDNESDAY, 22, 0), PROFILE) == at_local(
        WEDNESDAY + timedelta(days=1), 10, 0
    )


def test_inside_a_class_window_the_message_waits_for_its_end() -> None:
    now = at_local(TUESDAY, 12, 30)

    plan = plan_proactive_time(now, PROFILE, _FixedUniform(5))

    assert plan.reason is PlanReason.POSTPONED_BUSY
    assert plan.send_at == at_local(TUESDAY, 14, 5)


def test_a_delay_that_lands_in_a_class_window_waits_for_its_end() -> None:
    now = at_local(TUESDAY, 11, 59)

    plan = plan_proactive_time(now, PROFILE, _FixedUniform(90))

    assert plan.reason is PlanReason.POSTPONED_BUSY
    _assert_sendable(plan.send_at, PROFILE)


def test_a_weekday_night_ends_before_the_window_so_the_window_decides() -> None:
    now = at_local(MONDAY, 3, 0)

    plan = plan_proactive_time(now, PROFILE, _FixedUniform(2))

    assert plan.reason is PlanReason.POSTPONED_WINDOW
    assert plan.send_at == at_local(MONDAY, 10, 2)


@pytest.mark.parametrize("day", [SATURDAY, SUNDAY])
def test_a_weekend_night_ends_after_the_window_start_so_the_night_decides(day: date) -> None:
    now = at_local(day, 3, 0)

    plan = plan_proactive_time(now, PROFILE, _FixedUniform(2))

    assert plan.reason is PlanReason.POSTPONED_NIGHT
    assert in_local(plan.send_at).hour >= 11
    _assert_sendable(plan.send_at, PROFILE)


def test_friday_late_evening_waits_for_saturday_after_the_weekend_night() -> None:
    now = at_local(FRIDAY, 23, 30)

    plan = plan_proactive_time(now, PROFILE, _FixedUniform(3))

    assert in_local(plan.send_at).date() == SATURDAY
    assert plan.reason is PlanReason.POSTPONED_NIGHT
    _assert_sendable(plan.send_at, PROFILE)


@pytest.mark.parametrize("day", ALL_WEEK)
@pytest.mark.parametrize("hour", range(24))
def test_every_planned_time_is_inside_the_window_and_outside_every_blocked_window(
    day: date, hour: int
) -> None:
    now = at_local(day, hour, 17)

    plan = plan_proactive_time(now, PROFILE, random.Random(SEED + hour))

    assert plan.reason is not PlanReason.EXHAUSTED
    assert plan.send_at >= now
    _assert_sendable(plan.send_at, PROFILE)


def test_the_same_seed_gives_the_same_plan() -> None:
    now = at_local(WEDNESDAY, 23, 0)

    first = plan_proactive_time(now, PROFILE, random.Random(SEED))
    second = plan_proactive_time(now, PROFILE, random.Random(SEED))

    assert first == second


def test_a_window_that_is_always_busy_ends_exhausted() -> None:
    always_busy = profile_with(
        busy=[
            {
                "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                "from": "09:00",
                "to": "23:00",
            }
        ]
    )

    plan = plan_proactive_time(at_local(WEDNESDAY, 15, 0), always_busy, _FixedUniform(1))

    assert plan.reason is PlanReason.EXHAUSTED
