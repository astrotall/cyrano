from __future__ import annotations

import random
from datetime import datetime, timedelta

from cyrano.domain.enums import PlanReason, WrapUpPhase
from cyrano.domain.schedule import ScheduleProfile, UnavailableSoon
from cyrano.services.humanization import (
    WRAPUP_SEND_MARGIN_SECONDS,
    blocked,
    plan_send_time,
    plan_wrapup_time,
    unavailable_soon,
    wrapup_deadline,
)
from tests.services.schedule_data import TUESDAY, at_local, profile_with

SEED = 9
TYPING = 10.0
SHORT = profile_with(night={"start": ["01:00", "01:00"]})
SLOW = profile_with(
    night={"start": ["01:00", "01:00"]},
    reaction={"ongoing_seconds": [100, 100], "new_seconds": [200, 200]},
)


def _soon(now: datetime, profile: ScheduleProfile) -> UnavailableSoon:
    soon = unavailable_soon(now, profile)
    assert soon is not None and soon.phase is WrapUpPhase.HARD
    return soon


def test_the_margin_is_thirty_seconds() -> None:
    assert WRAPUP_SEND_MARGIN_SECONDS == 30


def test_the_hard_phase_draws_the_short_delay_whatever_the_conversation_state() -> None:
    now = at_local(TUESDAY, 0, 45)

    plan = plan_wrapup_time(now, now, False, _soon(now, SHORT), TYPING, SHORT, random.Random(SEED))

    expected = plan_send_time(now, now, True, SHORT, random.Random(SEED))
    assert plan == expected
    assert plan.reason is PlanReason.ONGOING
    assert now + timedelta(seconds=1) <= plan.send_at <= now + timedelta(seconds=4)


def test_a_send_time_after_the_deadline_is_clamped_to_it() -> None:
    now = at_local(TUESDAY, 0, 58, 30)
    soon = _soon(now, SLOW)

    plan = plan_wrapup_time(now, now, True, soon, TYPING, SLOW, random.Random(SEED))

    assert plan.reason is PlanReason.WRAPUP
    assert plan.send_at == soon.starts_at - timedelta(seconds=TYPING + 30)
    assert plan.send_at == wrapup_deadline(soon, TYPING)
    assert blocked(plan.send_at, SLOW) is None


def test_a_send_time_exactly_at_the_deadline_is_kept() -> None:
    now = at_local(TUESDAY, 0, 57) - timedelta(seconds=TYPING + 30 + 100) + timedelta(minutes=3)
    soon = _soon(now, SLOW)
    assert wrapup_deadline(soon, TYPING) == now + timedelta(seconds=100)

    plan = plan_wrapup_time(now, now, True, soon, TYPING, SLOW, random.Random(SEED))

    assert plan.send_at == now + timedelta(seconds=100)
    assert plan.reason is PlanReason.ONGOING


def test_a_deadline_exactly_now_falls_back_to_the_normal_postponement() -> None:
    soon = _soon(at_local(TUESDAY, 0, 59), SHORT)
    now = wrapup_deadline(soon, TYPING)

    plan = plan_wrapup_time(now, now, False, soon, TYPING, SHORT, random.Random(SEED))

    assert plan.reason is PlanReason.POSTPONED_NIGHT
    assert plan.send_at >= at_local(TUESDAY, 9, 0)


def test_an_infeasible_deadline_postpones_like_any_reply_in_the_window() -> None:
    now = at_local(TUESDAY, 0, 59, 50)
    soon = _soon(now, SHORT)

    plan = plan_wrapup_time(now, now, True, soon, TYPING, SHORT, random.Random(SEED))
    expected = plan_send_time(now, now, True, SHORT, random.Random(SEED), not_before=soon.starts_at)

    assert plan == expected
    assert plan.reason is PlanReason.POSTPONED_NIGHT
    assert blocked(plan.send_at, SHORT) is None
    assert plan.send_at > soon.starts_at


def test_without_not_before_the_planner_is_unchanged() -> None:
    now = at_local(TUESDAY, 15, 0)

    assert plan_send_time(now, now, False, SHORT, random.Random(SEED)) == plan_send_time(
        now, now, False, SHORT, random.Random(SEED), not_before=None
    )
