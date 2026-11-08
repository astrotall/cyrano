from __future__ import annotations

import logging
import math
import random
from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta

from cyrano.domain.enums import BlockKind, MessageDirection, PlanReason, WrapUpPhase
from cyrano.domain.errors import NoMessagesToReplyTo, PlanNotPossible
from cyrano.domain.models import Message
from cyrano.domain.schedule import (
    WEEKDAYS,
    Block,
    ScheduleProfile,
    SendPlan,
    UnavailableSoon,
    default_schedule_profile,
)
from cyrano.services.repositories import MessageRepository, ScheduleProfileRepository

logger = logging.getLogger("cyrano.services.humanization")

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
WRAPUP_SEND_MARGIN_SECONDS = 30
WRAPUP_LOOKAHEAD_DAYS = (0, 1)
ONE_MINUTE = timedelta(minutes=1)


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


def _upcoming_starts(now: datetime, profile: ScheduleProfile) -> list[UnavailableSoon]:
    today = now.astimezone(profile.zone).date()
    found: list[tuple[datetime, int, BlockKind, str | None]] = []
    for offset in WRAPUP_LOOKAHEAD_DAYS:
        day = today + timedelta(days=offset)
        night_start, _ = night_window(day, profile)
        found.append((night_start, 0, BlockKind.NIGHT, None))
        weekday = WEEKDAYS[day.weekday()]
        for order, window in enumerate(profile.busy, start=1):
            if weekday in window.days:
                found.append(
                    (_utc(day, window.start, profile), order, BlockKind.BUSY, window.label)
                )
    upcoming = sorted((item for item in found if item[0] > now), key=lambda item: item[:2])
    return [
        UnavailableSoon(
            kind=kind,
            starts_at=starts_at,
            minutes_left=max(1, math.ceil((starts_at - now) / ONE_MINUTE)),
            phase=WrapUpPhase.HARD
            if starts_at - now <= timedelta(minutes=profile.wrapup_hard_minutes)
            else WrapUpPhase.SOFT,
            label=label,
        )
        for starts_at, _, kind, label in upcoming
    ]


def unavailable_soon(now: datetime, profile: ScheduleProfile) -> UnavailableSoon | None:
    if blocked(now, profile) is not None:
        return None
    upcoming = _upcoming_starts(now, profile)
    if not upcoming:
        return None
    earliest = upcoming[0]
    if earliest.starts_at - now > timedelta(minutes=profile.wrapup_soft_minutes):
        return None
    return earliest


def wrapup_deadline(soon: UnavailableSoon, typing: float) -> datetime:
    return soon.starts_at - timedelta(seconds=typing + WRAPUP_SEND_MARGIN_SECONDS)


def plan_wrapup_time(
    now: datetime,
    trigger_at: datetime,
    ongoing: bool,
    soon: UnavailableSoon,
    typing: float,
    profile: ScheduleProfile,
    rng: random.Random,
) -> SendPlan:
    deadline = wrapup_deadline(soon, typing)
    if deadline <= now:
        return plan_send_time(now, trigger_at, ongoing, profile, rng, not_before=soon.starts_at)
    plan = plan_send_time(now, trigger_at, True, profile, rng)
    if plan.send_at > deadline:
        return SendPlan(send_at=deadline, reason=PlanReason.WRAPUP)
    return plan


def plan_send_time(
    now: datetime,
    trigger_at: datetime,
    ongoing: bool,
    profile: ScheduleProfile,
    rng: random.Random,
    *,
    not_before: datetime | None = None,
) -> SendPlan:
    reaction = profile.reaction
    delay = rng.uniform(*(reaction.ongoing_seconds if ongoing else reaction.new_seconds))
    candidate = max(now, trigger_at + timedelta(seconds=delay))
    if not_before is not None:
        candidate = max(candidate, not_before)
    reason = PlanReason.ONGOING if ongoing else PlanReason.DELAYED
    for _ in range(MAX_POSTPONE_ROUNDS):
        block = blocked(candidate, profile)
        if block is None:
            return SendPlan(send_at=candidate, reason=reason)
        candidate = postponed_after(block.end, profile, rng)
        reason = POSTPONED_REASONS[block.kind]
    return SendPlan(send_at=candidate, reason=PlanReason.EXHAUSTED)


def in_proactive_window(at: datetime, profile: ScheduleProfile) -> bool:
    day = at.astimezone(profile.zone).date()
    start, end = profile.proactive_window
    return _utc(day, start, profile) <= at < _utc(day, end, profile)


def proactive_window_start_after(at: datetime, profile: ScheduleProfile) -> datetime:
    day = at.astimezone(profile.zone).date()
    start = _utc(day, profile.proactive_window[0], profile)
    if at < start:
        return start
    return _utc(day + timedelta(days=1), profile.proactive_window[0], profile)


def plan_proactive_time(now: datetime, profile: ScheduleProfile, rng: random.Random) -> SendPlan:
    candidate = now
    reason = PlanReason.DELAYED
    if in_proactive_window(now, profile) and blocked(now, profile) is None:
        candidate = now + timedelta(seconds=rng.uniform(*profile.reaction.new_seconds))
    for _ in range(MAX_POSTPONE_ROUNDS):
        if not in_proactive_window(candidate, profile):
            candidate = postponed_after(
                proactive_window_start_after(candidate, profile), profile, rng
            )
            reason = PlanReason.POSTPONED_WINDOW
            continue
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

    async def unavailable_soon(self, now: datetime) -> UnavailableSoon | None:
        try:
            profile = await self.get_profile()
        except Exception as exc:
            logger.warning("the schedule profile was not read", extra={"error": type(exc).__name__})
            return None
        return unavailable_soon(now, profile)


class HumanizationPlanner:
    def __init__(
        self, messages: MessageRepository, schedule: ScheduleService, rng: random.Random
    ) -> None:
        self._messages = messages
        self._schedule = schedule
        self._rng = rng

    async def plan(
        self,
        contact_id: int,
        trigger_message_id: int,
        now: datetime,
        *,
        ongoing: bool | None = None,
        wrapup: UnavailableSoon | None = None,
        first_part: str = "",
    ) -> SendPlan:
        history = await self._messages.list_last(contact_id, PLANNER_HISTORY_MESSAGES)
        return await self._plan_from(
            history, contact_id, trigger_message_id, now, ongoing, wrapup, first_part
        )

    async def unavailable_soon(self, now: datetime) -> UnavailableSoon | None:
        return await self._schedule.unavailable_soon(now)

    async def conversation_ongoing(self, contact_id: int, message_id: int) -> bool:
        history = await self._messages.list_last(contact_id, PLANNER_HISTORY_MESSAGES)
        trigger = _trigger_index(history, contact_id, message_id)
        return burst_is_ongoing(history, trigger, await self._schedule.get_profile())

    async def plan_latest(self, contact_id: int, now: datetime) -> SendPlan:
        history = await self._messages.list_last(contact_id, PLANNER_HISTORY_MESSAGES)
        for message in reversed(history):
            if message.direction is MessageDirection.INCOMING:
                return await self._plan_from(history, contact_id, message.id, now)
        raise NoMessagesToReplyTo(contact_id)

    async def _plan_from(
        self,
        history: Sequence[Message],
        contact_id: int,
        trigger_id: int,
        now: datetime,
        ongoing: bool | None = None,
        wrapup: UnavailableSoon | None = None,
        first_part: str = "",
    ) -> SendPlan:
        trigger = _trigger_index(history, contact_id, trigger_id)
        profile = await self._schedule.get_profile()
        if ongoing is None:
            ongoing = burst_is_ongoing(history, trigger, profile)
        trigger_at = history[trigger].created_at
        if wrapup is not None and wrapup.phase is WrapUpPhase.HARD:
            typing = typing_seconds(first_part, profile)
            return plan_wrapup_time(now, trigger_at, ongoing, wrapup, typing, profile, self._rng)
        return plan_send_time(now, trigger_at, ongoing, profile, self._rng)


def burst_is_ongoing(history: Sequence[Message], trigger: int, profile: ScheduleProfile) -> bool:
    first = _burst_start(history, trigger)
    previous_activity_at = history[first - 1].created_at if first > 0 else None
    return is_ongoing(history[first].created_at, previous_activity_at, profile)


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
