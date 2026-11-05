from __future__ import annotations

import random
from datetime import UTC, date, datetime, time, timedelta

import pytest

from cyrano.domain.enums import BlockKind, PlanReason
from cyrano.domain.schedule import ScheduleProfile
from cyrano.services.humanization import (
    MAX_POSTPONE_ROUNDS,
    blocked,
    is_ongoing,
    night_window,
    plan_send_time,
    typing_seconds,
)
from tests.services.schedule_data import (
    ALL_WEEK,
    BERLIN,
    FRIDAY,
    MONDAY,
    MOSCOW,
    NEXT_MONDAY,
    SATURDAY,
    SUNDAY,
    THURSDAY,
    TUESDAY,
    WEDNESDAY,
    at_local,
    in_local,
    profile_with,
    two_busy_windows,
)

DEFAULTS = profile_with()
SEED = 41
SEVERAL_DATES = [date(2026, 11, 2) + timedelta(days=offset) for offset in range(120)]


def _local_time(moment: datetime, zone: str = MOSCOW) -> time:
    return in_local(moment, zone).time()


def _minute_of_day(moment: datetime) -> int:
    local = _local_time(moment)
    return local.hour * 60 + local.minute


def test_the_boundaries_of_a_date_are_the_same_on_every_call() -> None:
    for day in SEVERAL_DATES:
        assert night_window(day, DEFAULTS) == night_window(day, DEFAULTS)


def test_the_boundaries_do_not_depend_on_the_global_random_state() -> None:
    random.seed(1)
    first = night_window(SATURDAY, DEFAULTS)
    random.seed(2)
    second = night_window(SATURDAY, DEFAULTS)

    assert first == second


def test_the_boundaries_stay_inside_their_ranges_and_vary_between_dates() -> None:
    starts = set()
    weekend_ends = set()
    for day in SEVERAL_DATES:
        start, end = night_window(day, DEFAULTS)
        starts.add(_local_time(start))
        assert time(1, 0) <= _local_time(start) <= time(2, 0)
        assert in_local(start).date() == day
        assert in_local(end).date() == day
        if day.weekday() >= 5:
            weekend_ends.add(_local_time(end))
            assert time(11, 0) <= _local_time(end) <= time(12, 0)
        else:
            assert _local_time(end) == time(9, 0)

    assert len(starts) > 5
    assert len(weekend_ends) > 5


def test_the_start_and_the_end_come_from_different_draws() -> None:
    profile = profile_with(
        night={
            "start": ["00:00", "05:00"],
            "end_weekday": ["05:01", "12:00"],
            "end_weekend": ["05:01", "12:00"],
        }
    )
    windows = [night_window(day, profile) for day in SEVERAL_DATES]
    offsets = {_minute_of_day(end) - _minute_of_day(start) for start, end in windows}

    assert len(offsets) > 1


@pytest.mark.parametrize("day", [MONDAY, TUESDAY, WEDNESDAY, THURSDAY, FRIDAY])
def test_a_weekday_morning_ends_at_nine(day: date) -> None:
    assert _local_time(night_window(day, DEFAULTS)[1]) == time(9, 0)
    assert blocked(at_local(day, 8, 59, 59), DEFAULTS) is not None
    assert blocked(at_local(day, 9, 0), DEFAULTS) is None


@pytest.mark.parametrize("day", [SATURDAY, SUNDAY])
def test_a_weekend_morning_ends_between_eleven_and_twelve(day: date) -> None:
    end = night_window(day, DEFAULTS)[1]

    assert time(11, 0) <= _local_time(end) <= time(12, 0)
    assert blocked(at_local(day, 10, 59), DEFAULTS) is not None
    assert blocked(at_local(day, 12, 0), DEFAULTS) is None


def test_the_window_is_half_open() -> None:
    start, end = night_window(TUESDAY, DEFAULTS)

    block = blocked(start, DEFAULTS)
    assert block is not None
    assert block.end == end
    assert block.kind is BlockKind.NIGHT
    assert blocked(start - timedelta(microseconds=1), DEFAULTS) is None
    assert blocked(end - timedelta(microseconds=1), DEFAULTS) is not None
    assert blocked(end, DEFAULTS) is None


def test_friday_night_belongs_to_the_saturday_window() -> None:
    saturday_start, saturday_end = night_window(SATURDAY, DEFAULTS)

    assert blocked(at_local(FRIDAY, 23, 30), DEFAULTS) is None
    block = blocked(saturday_start + timedelta(minutes=1), DEFAULTS)
    assert block is not None
    assert block.end == saturday_end
    assert time(11, 0) <= _local_time(block.end) <= time(12, 0)


def test_sunday_night_belongs_to_the_monday_window() -> None:
    monday_start, monday_end = night_window(NEXT_MONDAY, DEFAULTS)

    assert blocked(at_local(SUNDAY, 23, 30), DEFAULTS) is None
    block = blocked(monday_start + timedelta(minutes=1), DEFAULTS)
    assert block is not None
    assert block.end == monday_end
    assert _local_time(block.end) == time(9, 0)


def test_half_past_midnight_is_not_blocked_and_half_past_one_may_be() -> None:
    results = {blocked(at_local(day, 1, 30), DEFAULTS) is None for day in SEVERAL_DATES}

    for day in SEVERAL_DATES:
        assert blocked(at_local(day, 0, 30), DEFAULTS) is None
    assert results == {True, False}


def test_two_is_always_at_or_after_the_start() -> None:
    for day in SEVERAL_DATES:
        assert blocked(at_local(day, 2, 0), DEFAULTS) is not None


def test_a_zone_with_no_dst_keeps_the_same_utc_offset_all_year() -> None:
    winter_end = night_window(date(2026, 1, 12), DEFAULTS)[1]
    summer_end = night_window(date(2026, 7, 13), DEFAULTS)[1]

    assert (winter_end.hour, winter_end.minute) == (6, 0)
    assert (summer_end.hour, summer_end.minute) == (6, 0)
    assert winter_end.tzinfo is UTC


def test_a_dst_zone_keeps_the_local_time_across_the_change() -> None:
    profile = profile_with(timezone=BERLIN)

    assert night_window(date(2026, 3, 27), profile)[1] == datetime(2026, 3, 27, 8, 0, tzinfo=UTC)
    assert night_window(date(2026, 3, 30), profile)[1] == datetime(2026, 3, 30, 7, 0, tzinfo=UTC)
    sunday_start, sunday_end = night_window(date(2026, 3, 29), profile)
    assert (
        datetime(2026, 3, 29, 0, 0, tzinfo=UTC)
        <= sunday_start
        <= datetime(2026, 3, 29, 1, 0, tzinfo=UTC)
    )
    assert (
        datetime(2026, 3, 29, 9, 0, tzinfo=UTC)
        <= sunday_end
        <= datetime(2026, 3, 29, 10, 0, tzinfo=UTC)
    )


def test_a_moment_is_judged_by_its_local_date_in_a_dst_zone() -> None:
    profile = profile_with(timezone=BERLIN)
    start, end = night_window(date(2026, 10, 25), profile)

    assert start < datetime(2026, 10, 25, 0, 0, tzinfo=UTC)
    block = blocked(start, profile)
    assert block is not None
    assert block.end == end


def test_blocked_refuses_a_naive_time_instead_of_guessing() -> None:
    with pytest.raises(TypeError):
        blocked(datetime(2026, 11, 3, 4, 0), DEFAULTS)


@pytest.mark.parametrize("day", [MONDAY, WEDNESDAY, FRIDAY])
def test_monday_wednesday_and_friday_have_both_windows(day: date) -> None:
    profile = two_busy_windows()

    first = blocked(at_local(day, 11, 0), profile)
    second = blocked(at_local(day, 13, 0), profile)
    assert first is not None
    assert (first.end, first.kind) == (at_local(day, 12, 10), BlockKind.BUSY)
    assert second is not None
    assert (second.end, second.kind) == (at_local(day, 14, 45), BlockKind.BUSY)


@pytest.mark.parametrize("day", [TUESDAY, THURSDAY])
def test_tuesday_and_thursday_have_only_the_second_window(day: date) -> None:
    profile = two_busy_windows()

    assert blocked(at_local(day, 11, 0), profile) is None
    second = blocked(at_local(day, 13, 0), profile)
    assert second is not None
    assert second.end == at_local(day, 14, 45)


@pytest.mark.parametrize("day", [SATURDAY, SUNDAY])
def test_saturday_and_sunday_have_no_busy_window(day: date) -> None:
    profile = two_busy_windows()

    assert blocked(at_local(day, 13, 0), profile) is None
    assert blocked(at_local(day, 12, 30), profile) is None


def test_the_busy_edges_are_half_open_and_the_break_is_free() -> None:
    profile = two_busy_windows()

    assert blocked(at_local(MONDAY, 10, 39, 59), profile) is None
    assert blocked(at_local(MONDAY, 10, 40), profile) is not None
    assert blocked(at_local(MONDAY, 12, 9, 59), profile) is not None
    assert blocked(at_local(MONDAY, 12, 10), profile) is None
    assert blocked(at_local(MONDAY, 12, 30), profile) is None
    assert blocked(at_local(MONDAY, 12, 44, 59), profile) is None
    assert blocked(at_local(MONDAY, 12, 45), profile) is not None
    assert blocked(at_local(MONDAY, 14, 44, 59), profile) is not None
    assert blocked(at_local(MONDAY, 14, 45), profile) is None


def test_busy_days_follow_the_local_weekday_not_the_utc_one() -> None:
    zone = "Pacific/Auckland"
    profile = profile_with(timezone=zone, busy=[{"days": ["mon"], "from": "10:00", "to": "11:00"}])
    local_monday = at_local(MONDAY, 10, 30, zone=zone)
    local_tuesday = at_local(TUESDAY, 10, 30, zone=zone)

    assert (local_monday.weekday(), local_tuesday.weekday()) == (6, 0)
    assert blocked(local_monday, profile) is not None
    assert blocked(local_tuesday, profile) is None


def test_an_overlap_with_the_night_returns_the_later_end_and_its_kind() -> None:
    later_busy = profile_with(busy=[{"days": ["tue"], "from": "08:00", "to": "10:00"}])
    earlier_busy = profile_with(busy=[{"days": ["tue"], "from": "03:00", "to": "04:00"}])

    block = blocked(at_local(TUESDAY, 8, 30), later_busy)
    assert block is not None
    assert (block.end, block.kind) == (at_local(TUESDAY, 10, 0), BlockKind.BUSY)
    block = blocked(at_local(TUESDAY, 3, 30), earlier_busy)
    assert block is not None
    assert (block.end, block.kind) == (at_local(TUESDAY, 9, 0), BlockKind.NIGHT)


def test_overlapping_busy_windows_return_the_latest_end() -> None:
    profile = profile_with(
        busy=[
            {"days": ["tue"], "from": "10:00", "to": "11:00"},
            {"days": ["tue"], "from": "10:30", "to": "13:00"},
            {"days": ["tue"], "from": "10:15", "to": "12:00"},
        ]
    )

    block = blocked(at_local(TUESDAY, 10, 45), profile)
    assert block is not None
    assert block.end == at_local(TUESDAY, 13, 0)


def test_is_ongoing_is_false_without_previous_activity() -> None:
    assert is_ongoing(at_local(TUESDAY, 12), None, DEFAULTS) is False


def test_is_ongoing_at_its_boundary() -> None:
    first = at_local(TUESDAY, 12)

    assert is_ongoing(first, first - timedelta(minutes=10), DEFAULTS) is False
    assert is_ongoing(first, first - timedelta(minutes=10, seconds=1), DEFAULTS) is False
    assert is_ongoing(first, first - timedelta(minutes=9, seconds=59), DEFAULTS) is True
    assert is_ongoing(first, first, DEFAULTS) is True


def test_is_ongoing_uses_the_profile_window() -> None:
    profile = profile_with(reaction={"ongoing_window_minutes": 1})
    first = at_local(TUESDAY, 12)

    assert is_ongoing(first, first - timedelta(seconds=59), profile) is True
    assert is_ongoing(first, first - timedelta(seconds=60), profile) is False


def _plan(
    now: datetime, trigger_at: datetime, ongoing: bool, profile: ScheduleProfile, seed: int = SEED
) -> tuple[datetime, PlanReason]:
    plan = plan_send_time(now, trigger_at, ongoing, profile, random.Random(seed))
    return plan.send_at, plan.reason


@pytest.mark.parametrize(
    ("ongoing", "low", "high", "reason"),
    [(True, 1, 4, PlanReason.ONGOING), (False, 20, 120, PlanReason.DELAYED)],
)
def test_the_delay_comes_from_the_matching_range(
    ongoing: bool, low: int, high: int, reason: PlanReason
) -> None:
    trigger = at_local(TUESDAY, 16)
    for seed in range(50):
        send_at, got = _plan(trigger, trigger, ongoing, DEFAULTS, seed)
        assert low <= (send_at - trigger).total_seconds() <= high
        assert got is reason


def test_a_seeded_generator_gives_the_exact_delay() -> None:
    trigger = at_local(TUESDAY, 16)
    expected = random.Random(SEED).uniform(20, 120)

    send_at, _ = _plan(trigger, trigger, False, DEFAULTS)

    assert send_at == trigger + timedelta(seconds=expected)


def test_the_delay_is_measured_from_the_trigger_not_from_now() -> None:
    trigger = at_local(TUESDAY, 16)
    now = trigger + timedelta(seconds=5)
    expected = random.Random(SEED).uniform(20, 120)

    send_at, _ = _plan(now, trigger, False, DEFAULTS)

    assert send_at == trigger + timedelta(seconds=expected)


def test_the_plan_is_never_earlier_than_now() -> None:
    trigger = at_local(TUESDAY, 16)
    now = trigger + timedelta(hours=1)

    send_at, reason = _plan(now, trigger, False, DEFAULTS)

    assert send_at == now
    assert reason is PlanReason.DELAYED


def test_a_plan_inside_the_night_goes_past_its_end_with_the_spread() -> None:
    trigger = at_local(TUESDAY, 4)
    rng = random.Random(SEED)
    rng.uniform(20, 120)
    spread = rng.uniform(1, 15)

    send_at, reason = _plan(trigger, trigger, False, DEFAULTS)

    assert send_at == at_local(TUESDAY, 9) + timedelta(minutes=spread)
    assert reason is PlanReason.POSTPONED_NIGHT


def test_a_plan_inside_a_busy_window_goes_past_its_end_and_says_so() -> None:
    trigger = at_local(MONDAY, 13)

    send_at, reason = _plan(trigger, trigger, True, two_busy_windows())

    assert at_local(MONDAY, 14, 46) <= send_at <= at_local(MONDAY, 15, 0)
    assert reason is PlanReason.POSTPONED_BUSY


def test_a_trigger_just_before_a_window_lands_inside_it_and_is_postponed() -> None:
    profile = profile_with(busy=[{"days": ["tue"], "from": "16:00", "to": "17:00"}])
    trigger = at_local(TUESDAY, 15, 59, 59)

    send_at, reason = _plan(trigger, trigger, False, profile)

    assert send_at > at_local(TUESDAY, 17, 0)
    assert reason is PlanReason.POSTPONED_BUSY


def test_a_spread_that_lands_in_the_next_window_is_pushed_again() -> None:
    profile = profile_with(
        busy=[{"days": ["tue"], "from": "09:05", "to": "10:00"}],
        postponed_spread_minutes=[10, 10],
    )
    trigger = at_local(TUESDAY, 4)

    send_at, reason = _plan(trigger, trigger, False, profile)

    assert send_at == at_local(TUESDAY, 10, 10)
    assert reason is PlanReason.POSTPONED_BUSY


def test_the_reason_names_the_kind_of_the_last_push() -> None:
    profile = profile_with(
        busy=[{"days": ["tue"], "from": "00:00", "to": "00:55"}],
        night={
            "start": ["01:00", "01:00"],
            "end_weekday": ["09:00", "09:00"],
            "end_weekend": ["11:00", "12:00"],
        },
        postponed_spread_minutes=[10, 10],
    )
    trigger = at_local(TUESDAY, 0, 30)

    send_at, reason = _plan(trigger, trigger, False, profile)

    assert send_at == at_local(TUESDAY, 9, 10)
    assert reason is PlanReason.POSTPONED_NIGHT


def _chain_of_windows(count: int) -> ScheduleProfile:
    return profile_with(
        busy=[
            {
                "days": ["tue"],
                "from": f"{16 + index // 4:02d}:{(index % 4) * 15:02d}",
                "to": f"{16 + index // 4:02d}:{(index % 4) * 15 + 5:02d}",
            }
            for index in range(count)
        ],
        postponed_spread_minutes=[10, 10],
    )


def test_a_chain_longer_than_the_rounds_is_exhausted_and_uses_the_last_candidate() -> None:
    profile = _chain_of_windows(MAX_POSTPONE_ROUNDS + 2)
    trigger = at_local(TUESDAY, 16, 0)

    send_at, reason = _plan(trigger, trigger, True, profile)

    assert reason is PlanReason.EXHAUSTED
    assert send_at == at_local(TUESDAY, 16, 0) + timedelta(
        minutes=15 * (MAX_POSTPONE_ROUNDS - 1) + 5 + 10
    )
    assert blocked(send_at, profile) is not None


def test_a_chain_shorter_than_the_rounds_ends_free() -> None:
    profile = _chain_of_windows(MAX_POSTPONE_ROUNDS - 1)
    trigger = at_local(TUESDAY, 16, 0)

    send_at, reason = _plan(trigger, trigger, True, profile)

    assert reason is PlanReason.POSTPONED_BUSY
    assert blocked(send_at, profile) is None


def test_eight_pushes_are_reported_as_exhausted_even_if_the_last_candidate_is_free() -> None:
    profile = _chain_of_windows(MAX_POSTPONE_ROUNDS)
    trigger = at_local(TUESDAY, 16, 0)

    send_at, reason = _plan(trigger, trigger, True, profile)

    assert reason is PlanReason.EXHAUSTED
    assert blocked(send_at, profile) is None


def test_the_plan_is_in_utc_and_in_the_profile_zone_when_read_locally() -> None:
    profile = profile_with(timezone=BERLIN)
    trigger = datetime(2026, 11, 3, 3, 0, tzinfo=UTC)

    send_at, _ = _plan(trigger, trigger, False, profile)

    assert send_at.tzinfo is UTC
    assert in_local(send_at, BERLIN).hour == 9


def test_the_same_seed_gives_the_same_plan() -> None:
    trigger = at_local(TUESDAY, 4)

    assert _plan(trigger, trigger, False, DEFAULTS) == _plan(trigger, trigger, False, DEFAULTS)


@pytest.mark.parametrize(
    ("length", "expected"),
    [(0, 2.0), (1, 2.0), (10, 2.0), (11, 2.2), (60, 12.0), (125, 25.0), (126, 25.0), (5000, 25.0)],
)
def test_typing_seconds_clamps_at_min_and_max_and_scales_in_between(
    length: int, expected: float
) -> None:
    assert typing_seconds("x" * length, DEFAULTS) == pytest.approx(expected)


def test_typing_seconds_uses_the_profile_speed() -> None:
    profile = profile_with(typing={"chars_per_second": 10, "min_seconds": 0, "max_seconds": 60})

    assert typing_seconds("", profile) == 0
    assert typing_seconds("x" * 25, profile) == pytest.approx(2.5)


def test_every_day_of_the_week_gets_one_night_window() -> None:
    for day in ALL_WEEK:
        start, end = night_window(day, DEFAULTS)
        assert start < end
