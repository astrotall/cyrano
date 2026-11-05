"""The send time planner: pure rules over a schedule profile, and the two services that read it."""

from __future__ import annotations

import random
from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta

from cyrano.domain.enums import BlockKind, MessageDirection, PlanReason
from cyrano.domain.errors import NoMessagesToReplyTo, PlanNotPossible
from cyrano.domain.models import Message
from cyrano.domain.schedule import (
    WEEKDAYS,
    Block,
    ScheduleProfile,
    SendPlan,
    default_schedule_profile,
)
from cyrano.services.repositories import MessageRepository, ScheduleProfileRepository

NIGHT_START_LABEL = "night-start"
NIGHT_END_LABEL = "night-end"
BOUNDARY_SEED = "{day}:{label}"
FIRST_WEEKEND_DAY = 5
MINUTES_PER_HOUR = 60
SECONDS_PER_MINUTE = 60
MAX_POSTPONE_ROUNDS = 8
PLANNER_HISTORY_MESSAGES = 20
POSTPONED_REASONS = {
    BlockKind.NIGHT: PlanReason.POSTPONED_NIGHT,
    BlockKind.BUSY: PlanReason.POSTPONED_BUSY,
}


def _minutes(moment: time) -> int:
    return moment.hour * MINUTES_PER_HOUR + moment.minute


def _boundary(day: date, label: str, low: time, high: time) -> time:
    picker = random.Random(BOUNDARY_SEED.format(day=day.isoformat(), label=label))
    hours, minutes = divmod(picker.randint(_minutes(low), _minutes(high)), MINUTES_PER_HOUR)
    return time(hours, minutes)


def _utc(day: date, moment: time, profile: ScheduleProfile) -> datetime:
    return datetime.combine(day, moment, tzinfo=profile.zone).astimezone(UTC)


def night_window(day: date, profile: ScheduleProfile) -> tuple[datetime, datetime]:
    night = profile.night
    end_range = night.end_weekend if day.weekday() >= FIRST_WEEKEND_DAY else night.end_weekday
    start = _boundary(day, NIGHT_START_LABEL, *night.start)
    end = _boundary(day, NIGHT_END_LABEL, *end_range)
    return _utc(day, start, profile), _utc(day, end, profile)


def blocked(at: datetime, profile: ScheduleProfile) -> Block | None:
    day = at.astimezone(profile.zone).date()
    blocks: list[Block] = []
    night_start, night_end = night_window(day, profile)
    if night_start <= at < night_end:
        blocks.append(Block(night_end, BlockKind.NIGHT))
    weekday = WEEKDAYS[day.weekday()]
    for window in profile.busy:
        if weekday not in window.days:
            continue
        window_end = _utc(day, window.end, profile)
        if _utc(day, window.start, profile) <= at < window_end:
            blocks.append(Block(window_end, BlockKind.BUSY))
    return max(blocks, key=lambda block: block.end, default=None)


def is_ongoing(
    burst_first_at: datetime, previous_activity_at: datetime | None, profile: ScheduleProfile
) -> bool:
    if previous_activity_at is None:
        return False
    window = timedelta(minutes=profile.reaction.ongoing_window_minutes)
    return burst_first_at - previous_activity_at < window


def postponed_after(end: datetime, profile: ScheduleProfile, rng: random.Random) -> datetime:
    return end + timedelta(minutes=rng.uniform(*profile.postponed_spread_minutes))


def plan_send_time(
    now: datetime,
    trigger_at: datetime,
    ongoing: bool,
    profile: ScheduleProfile,
    rng: random.Random,
) -> SendPlan:
    reaction = profile.reaction
    delay = rng.uniform(*(reaction.ongoing_seconds if ongoing else reaction.new_seconds))
    candidate = max(now, trigger_at + timedelta(seconds=delay))
    reason = PlanReason.ONGOING if ongoing else PlanReason.DELAYED
    for _ in range(MAX_POSTPONE_ROUNDS):
        block = blocked(candidate, profile)
        if block is None:
            return SendPlan(send_at=candidate, reason=reason)
        candidate = postponed_after(block.end, profile, rng)
        reason = POSTPONED_REASONS[block.kind]
    return SendPlan(send_at=candidate, reason=PlanReason.EXHAUSTED)


def typing_seconds(text: str, profile: ScheduleProfile) -> float:
    typing = profile.typing
    return min(max(len(text) / typing.chars_per_second, typing.min_seconds), typing.max_seconds)


class ScheduleService:
    def __init__(self, profiles: ScheduleProfileRepository, default_timezone: str) -> None:
        self._profiles = profiles
        self._default_timezone = default_timezone

    async def get_profile(self) -> ScheduleProfile:
        stored = await self._profiles.get()
        if stored is None:
            return default_schedule_profile(self._default_timezone)
        return stored.with_default_timezone(self._default_timezone)

    async def blocked_until(self, now: datetime) -> Block | None:
        return blocked(now, await self.get_profile())


class HumanizationPlanner:
    def __init__(
        self, messages: MessageRepository, schedule: ScheduleService, rng: random.Random
    ) -> None:
        self._messages = messages
        self._schedule = schedule
        self._rng = rng

    async def plan(self, contact_id: int, trigger_message_id: int, now: datetime) -> SendPlan:
        history = await self._messages.list_last(contact_id, PLANNER_HISTORY_MESSAGES)
        return await self._plan_from(history, contact_id, trigger_message_id, now)

    async def plan_latest(self, contact_id: int, now: datetime) -> SendPlan:
        history = await self._messages.list_last(contact_id, PLANNER_HISTORY_MESSAGES)
        for message in reversed(history):
            if message.direction is MessageDirection.INCOMING:
                return await self._plan_from(history, contact_id, message.id, now)
        raise NoMessagesToReplyTo(contact_id)

    async def _plan_from(
        self, history: Sequence[Message], contact_id: int, trigger_id: int, now: datetime
    ) -> SendPlan:
        trigger = _trigger_index(history, contact_id, trigger_id)
        first = _burst_start(history, trigger)
        previous_activity_at = history[first - 1].created_at if first > 0 else None
        profile = await self._schedule.get_profile()
        ongoing = is_ongoing(history[first].created_at, previous_activity_at, profile)
        return plan_send_time(now, history[trigger].created_at, ongoing, profile, self._rng)


def _trigger_index(history: Sequence[Message], contact_id: int, trigger_id: int) -> int:
    for index, message in enumerate(history):
        if message.id == trigger_id and message.direction is MessageDirection.INCOMING:
            return index
    raise PlanNotPossible(contact_id, trigger_id)


def _burst_start(history: Sequence[Message], trigger: int) -> int:
    first = trigger
    while first > 0 and history[first - 1].direction is MessageDirection.INCOMING:
        first -= 1
    return first
