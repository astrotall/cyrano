"""The schedule profile, its limits, the blocked moment and the send plan."""

from __future__ import annotations

import re
from datetime import datetime, time
from typing import Annotated, NamedTuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    StrictInt,
    field_validator,
    model_validator,
)

from cyrano.domain.enums import BlockKind, PlanReason, Weekday
from cyrano.domain.errors import ScheduleTimezoneUnresolved
from cyrano.domain.models import UtcDatetime

SCHEDULE_PROFILE_VERSION = 1
MAX_BUSY_WINDOWS = 20
LATEST_NIGHT_START = time(5, 0)
MAX_REACTION_SECONDS = 3600
MIN_ONGOING_WINDOW_MINUTES = 1
MAX_ONGOING_WINDOW_MINUTES = 120
MAX_READ_PAUSE_SECONDS = 10
MIN_CHARS_PER_SECOND = 1
MAX_CHARS_PER_SECOND = 50
MAX_TYPING_SECONDS = 60
MAX_SPREAD_MINUTES = 120
DEFAULT_NIGHT_START = (time(1, 0), time(2, 0))
DEFAULT_NIGHT_END_WEEKDAY = (time(9, 0), time(9, 0))
DEFAULT_NIGHT_END_WEEKEND = (time(11, 0), time(12, 0))
DEFAULT_ONGOING_SECONDS = (1, 4)
DEFAULT_NEW_SECONDS = (20, 120)
DEFAULT_ONGOING_WINDOW_MINUTES = 10
DEFAULT_READ_PAUSE_SECONDS = (1, 3)
DEFAULT_CHARS_PER_SECOND = 5
DEFAULT_TYPING_MIN_SECONDS = 2
DEFAULT_TYPING_MAX_SECONDS = 25
DEFAULT_SPREAD_MINUTES = (1, 15)
WEEKDAYS = tuple(Weekday)
TIME_PATTERN = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")
TIME_SEPARATOR = ":"
TIME_FORMAT = "%H:%M"
TIME_RULE = "must be a time as HH:MM"
PAIR_RULE = "must be [lo, hi] with lo <= hi"
TIMEZONE_RULE = "must be an IANA time zone name"
VERSION_RULE = f"must be {SCHEDULE_PROFILE_VERSION}"
NIGHT_START_RULE = f"the start must not be after {LATEST_NIGHT_START.strftime(TIME_FORMAT)}"
NIGHT_END_RULE = "every end must be after the latest start"
NO_DAYS_RULE = "must name at least one day"
BUSY_ORDER_RULE = "from must be before to"
TYPING_ORDER_RULE = "min_seconds must not be above max_seconds"


def _parse_time(value: object) -> time:
    if not isinstance(value, str) or TIME_PATTERN.fullmatch(value) is None:
        raise ValueError(TIME_RULE)
    hours, minutes = value.split(TIME_SEPARATOR)
    return time(int(hours), int(minutes))


def _format_time(value: time) -> str:
    return value.strftime(TIME_FORMAT)


def _ordered[T: (time, int)](pair: tuple[T, T]) -> tuple[T, T]:
    if pair[1] < pair[0]:
        raise ValueError(PAIR_RULE)
    return pair


def _known_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError, ValueError, OSError:
        raise ValueError(TIMEZONE_RULE) from None
    return value


def _unique_in_week_order(days: tuple[Weekday, ...]) -> tuple[Weekday, ...]:
    if not days:
        raise ValueError(NO_DAYS_RULE)
    return tuple(day for day in WEEKDAYS if day in days)


LocalTime = Annotated[
    time, BeforeValidator(_parse_time), PlainSerializer(_format_time, return_type=str)
]
TimeRange = Annotated[tuple[LocalTime, LocalTime], AfterValidator(_ordered)]
ReactionSeconds = Annotated[StrictInt, Field(ge=0, le=MAX_REACTION_SECONDS)]
ReactionRange = Annotated[tuple[ReactionSeconds, ReactionSeconds], AfterValidator(_ordered)]
ReadPauseSeconds = Annotated[StrictInt, Field(ge=0, le=MAX_READ_PAUSE_SECONDS)]
ReadPauseRange = Annotated[tuple[ReadPauseSeconds, ReadPauseSeconds], AfterValidator(_ordered)]
SpreadMinutes = Annotated[StrictInt, Field(ge=0, le=MAX_SPREAD_MINUTES)]
SpreadRange = Annotated[tuple[SpreadMinutes, SpreadMinutes], AfterValidator(_ordered)]
TypingSeconds = Annotated[StrictInt, Field(ge=0, le=MAX_TYPING_SECONDS)]
TimezoneName = Annotated[str, AfterValidator(_known_timezone)]
Days = Annotated[tuple[Weekday, ...], AfterValidator(_unique_in_week_order)]


class _ProfilePart(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)


class NightSettings(_ProfilePart):
    start: TimeRange = DEFAULT_NIGHT_START
    end_weekday: TimeRange = DEFAULT_NIGHT_END_WEEKDAY
    end_weekend: TimeRange = DEFAULT_NIGHT_END_WEEKEND

    @model_validator(mode="after")
    def _stays_inside_one_day(self) -> NightSettings:
        latest_start = self.start[1]
        if latest_start > LATEST_NIGHT_START:
            raise ValueError(NIGHT_START_RULE)
        if min(self.end_weekday[0], self.end_weekend[0]) <= latest_start:
            raise ValueError(NIGHT_END_RULE)
        return self


class BusyWindow(_ProfilePart):
    days: Days
    start: LocalTime = Field(alias="from")
    end: LocalTime = Field(alias="to")

    @model_validator(mode="after")
    def _starts_before_it_ends(self) -> BusyWindow:
        if self.start >= self.end:
            raise ValueError(BUSY_ORDER_RULE)
        return self


class ReactionSettings(_ProfilePart):
    ongoing_seconds: ReactionRange = DEFAULT_ONGOING_SECONDS
    new_seconds: ReactionRange = DEFAULT_NEW_SECONDS
    ongoing_window_minutes: Annotated[
        StrictInt, Field(ge=MIN_ONGOING_WINDOW_MINUTES, le=MAX_ONGOING_WINDOW_MINUTES)
    ] = DEFAULT_ONGOING_WINDOW_MINUTES


class TypingSettings(_ProfilePart):
    chars_per_second: Annotated[
        StrictInt, Field(ge=MIN_CHARS_PER_SECOND, le=MAX_CHARS_PER_SECOND)
    ] = DEFAULT_CHARS_PER_SECOND
    min_seconds: TypingSeconds = DEFAULT_TYPING_MIN_SECONDS
    max_seconds: TypingSeconds = DEFAULT_TYPING_MAX_SECONDS

    @model_validator(mode="after")
    def _min_not_above_max(self) -> TypingSettings:
        if self.min_seconds > self.max_seconds:
            raise ValueError(TYPING_ORDER_RULE)
        return self


class ScheduleProfile(_ProfilePart):
    version: StrictInt
    timezone: TimezoneName | None = None
    night: NightSettings = Field(default_factory=NightSettings)
    busy: tuple[BusyWindow, ...] = Field(default=(), max_length=MAX_BUSY_WINDOWS)
    reaction: ReactionSettings = Field(default_factory=ReactionSettings)
    read_pause_seconds: ReadPauseRange = DEFAULT_READ_PAUSE_SECONDS
    typing: TypingSettings = Field(default_factory=TypingSettings)
    postponed_spread_minutes: SpreadRange = DEFAULT_SPREAD_MINUTES

    @field_validator("version")
    @classmethod
    def _check_version(cls, value: int) -> int:
        if value != SCHEDULE_PROFILE_VERSION:
            raise ValueError(VERSION_RULE)
        return value

    @property
    def zone(self) -> ZoneInfo:
        if self.timezone is None:
            raise ScheduleTimezoneUnresolved
        return ZoneInfo(self.timezone)

    def with_default_timezone(self, timezone: str) -> ScheduleProfile:
        if self.timezone is not None:
            return self
        return self.model_copy(update={"timezone": timezone})

    def stored_data(self) -> dict[str, object]:
        return self.model_dump(mode="json", by_alias=True)


def default_schedule_profile(timezone: str) -> ScheduleProfile:
    return ScheduleProfile(version=SCHEDULE_PROFILE_VERSION, timezone=timezone)


class Block(NamedTuple):
    end: datetime
    kind: BlockKind


class SendPlan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    send_at: UtcDatetime
    reason: PlanReason
