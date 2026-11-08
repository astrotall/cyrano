from __future__ import annotations

from datetime import date, timedelta

import pytest

from cyrano.domain.enums import BlockKind, WrapUpPhase
from cyrano.domain.errors import StoredScheduleInvalid
from cyrano.domain.schedule import ScheduleProfile, UnavailableSoon
from cyrano.services.humanization import ScheduleService, night_window, unavailable_soon
from tests.services.schedule_data import (
    ALL_WEEK,
    MOSCOW,
    TUESDAY,
    WEDNESDAY,
    at_local,
    profile_with,
)

FIXED_NIGHT = profile_with(night={"start": ["01:00", "01:00"]})
DRAWN_NIGHT = profile_with(night={"start": ["00:30", "01:30"]})
DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
SECOND = timedelta(seconds=1)
MINUTE = timedelta(minutes=1)


def _soon(now_day: date, hour: int, minute: int, second: int = 0) -> UnavailableSoon | None:
    return unavailable_soon(at_local(now_day, hour, minute, second), FIXED_NIGHT)


def test_exactly_the_soft_limit_before_the_night_is_soft() -> None:
    soon = _soon(TUESDAY, 0, 0)

    assert soon is not None
    assert (soon.kind, soon.phase, soon.minutes_left) == (BlockKind.NIGHT, WrapUpPhase.SOFT, 60)
    assert soon.starts_at == at_local(TUESDAY, 1, 0)
    assert soon.label is None


def test_one_second_beyond_the_soft_limit_is_nothing() -> None:
    assert _soon(TUESDAY - timedelta(days=1), 23, 59, 59) is None


def test_exactly_the_hard_limit_is_hard_and_one_second_more_is_soft() -> None:
    hard = _soon(TUESDAY, 0, 40)
    soft = _soon(TUESDAY, 0, 39, 59)

    assert hard is not None and hard.phase is WrapUpPhase.HARD and hard.minutes_left == 20
    assert soft is not None and soft.phase is WrapUpPhase.SOFT and soft.minutes_left == 21


def test_the_minutes_are_rounded_up() -> None:
    soon = _soon(TUESDAY, 0, 59, 59)

    assert soon is not None and soon.minutes_left == 1 and soon.phase is WrapUpPhase.HARD


def test_a_window_that_starts_exactly_now_is_already_blocked() -> None:
    assert _soon(TUESDAY, 1, 0) is None


def test_inside_the_night_and_inside_a_busy_window_there_is_no_context() -> None:
    busy = profile_with(
        night={"start": ["01:00", "01:00"]},
        busy=[{"days": ["tue"], "from": "12:00", "to": "13:00"}],
    )

    assert unavailable_soon(at_local(TUESDAY, 3, 0), busy) is None
    assert unavailable_soon(at_local(TUESDAY, 12, 30), busy) is None


def test_inside_a_window_there_is_no_context_even_when_the_next_one_is_near() -> None:
    profile = profile_with(
        night={"start": ["01:00", "01:00"]},
        busy=[
            {"days": ["tue"], "from": "00:00", "to": "00:50", "label": "смена"},
            {"days": ["tue"], "from": "12:00", "to": "13:00", "label": "первая"},
            {"days": ["tue"], "from": "13:00", "to": "14:00", "label": "вторая"},
        ],
    )

    assert unavailable_soon(at_local(TUESDAY, 0, 30), profile) is None
    assert unavailable_soon(at_local(TUESDAY, 12, 30), profile) is None
    after_class = unavailable_soon(at_local(TUESDAY, 0, 50), profile)
    assert after_class is not None and after_class.kind is BlockKind.NIGHT


def test_far_from_every_window_there_is_no_context() -> None:
    assert _soon(TUESDAY, 15, 0) is None


@pytest.mark.parametrize("day", ALL_WEEK)
def test_the_drawn_night_start_of_every_date_is_found_at_its_boundaries(day: date) -> None:
    start, _ = night_window(day, DRAWN_NIGHT)

    soft = unavailable_soon(start - 60 * MINUTE, DRAWN_NIGHT)
    hard = unavailable_soon(start - 20 * MINUTE, DRAWN_NIGHT)
    blocked_now = unavailable_soon(start, DRAWN_NIGHT)
    too_early = unavailable_soon(start - 60 * MINUTE - SECOND, DRAWN_NIGHT)

    assert soft is not None and soft.phase is WrapUpPhase.SOFT and soft.starts_at == start
    assert hard is not None and hard.phase is WrapUpPhase.HARD and hard.starts_at == start
    assert blocked_now is None
    assert too_early is None


@pytest.mark.parametrize(("index", "day"), list(enumerate(ALL_WEEK)))
def test_a_busy_window_of_each_weekday_is_found_only_on_its_day(index: int, day: date) -> None:
    profile = profile_with(
        night={"start": ["01:00", "01:00"]},
        busy=[{"days": [DAY_NAMES[index]], "from": "14:00", "to": "15:00", "label": "лекция"}],
    )

    soon = unavailable_soon(at_local(day, 13, 30), profile)
    other = unavailable_soon(at_local(day + timedelta(days=1), 13, 30), profile)

    assert soon is not None
    assert (soon.kind, soon.label, soon.minutes_left) == (BlockKind.BUSY, "лекция", 30)
    assert other is None


def test_a_window_of_tomorrow_is_found_and_the_earliest_start_wins() -> None:
    profile = profile_with(
        night={"start": ["01:00", "01:00"]},
        busy=[{"days": ["wed"], "from": "00:10", "to": "00:50", "label": "смена"}],
    )

    soon = unavailable_soon(at_local(TUESDAY, 23, 30), profile)

    assert soon is not None
    assert (soon.kind, soon.label, soon.minutes_left) == (BlockKind.BUSY, "смена", 40)
    assert soon.starts_at == at_local(WEDNESDAY, 0, 10)


def test_the_night_of_tomorrow_is_found_late_in_the_evening() -> None:
    soon = unavailable_soon(
        at_local(TUESDAY, 23, 50), profile_with(night={"start": ["00:30", "00:30"]})
    )

    assert soon is not None
    assert soon.kind is BlockKind.NIGHT
    assert soon.starts_at == at_local(WEDNESDAY, 0, 30)


def test_a_busy_window_that_starts_with_the_night_gives_the_night() -> None:
    profile = profile_with(
        night={"start": ["01:00", "01:00"]},
        busy=[{"days": ["tue"], "from": "01:00", "to": "02:00", "label": "смена"}],
    )

    soon = unavailable_soon(at_local(TUESDAY, 0, 30), profile)

    assert soon is not None and soon.kind is BlockKind.NIGHT and soon.label is None


def test_two_busy_windows_with_one_start_give_the_first_label() -> None:
    profile = profile_with(
        night={"start": ["01:00", "01:00"]},
        busy=[
            {"days": ["tue"], "from": "14:00", "to": "15:00", "label": "первая"},
            {"days": ["tue"], "from": "14:00", "to": "16:00", "label": "вторая"},
        ],
    )

    soon = unavailable_soon(at_local(TUESDAY, 13, 50), profile)

    assert soon is not None and soon.label == "первая"


def test_a_busy_window_without_a_label_gives_the_default_label() -> None:
    profile = profile_with(busy=[{"days": ["tue"], "from": "14:00", "to": "15:00"}])

    soon = unavailable_soon(at_local(TUESDAY, 13, 50), profile)

    assert soon is not None and soon.label == "занят"


def test_the_profile_limits_decide_the_phases() -> None:
    profile = profile_with(
        night={"start": ["01:00", "01:00"]}, wrapup_soft_minutes=5, wrapup_hard_minutes=1
    )

    assert unavailable_soon(at_local(TUESDAY, 0, 54, 59), profile) is None
    soft = unavailable_soon(at_local(TUESDAY, 0, 55), profile)
    hard = unavailable_soon(at_local(TUESDAY, 0, 59), profile)
    assert soft is not None and soft.phase is WrapUpPhase.SOFT
    assert hard is not None and hard.phase is WrapUpPhase.HARD


class _BrokenProfiles:
    async def get(self) -> ScheduleProfile | None:
        raise StoredScheduleInvalid

    async def replace(self, profile: ScheduleProfile) -> ScheduleProfile:
        return profile


async def test_an_unreadable_profile_gives_no_context() -> None:
    service = ScheduleService(_BrokenProfiles(), MOSCOW)

    assert await service.unavailable_soon(at_local(TUESDAY, 0, 50)) is None
