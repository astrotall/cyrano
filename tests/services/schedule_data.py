from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from cyrano.domain.schedule import ScheduleProfile

MOSCOW = "Europe/Moscow"
BERLIN = "Europe/Berlin"
MONDAY = date(2026, 11, 2)
TUESDAY = date(2026, 11, 3)
WEDNESDAY = date(2026, 11, 4)
THURSDAY = date(2026, 11, 5)
FRIDAY = date(2026, 11, 6)
SATURDAY = date(2026, 11, 7)
SUNDAY = date(2026, 11, 8)
NEXT_MONDAY = date(2026, 11, 9)
ALL_WEEK = [MONDAY, TUESDAY, WEDNESDAY, THURSDAY, FRIDAY, SATURDAY, SUNDAY]
MON_WED_FRI = ["mon", "wed", "fri"]
WORKING_DAYS = ["mon", "tue", "wed", "thu", "fri"]
FIRST_BUSY_FROM = "10:40"
FIRST_BUSY_TO = "12:10"
SECOND_BUSY_FROM = "12:45"
SECOND_BUSY_TO = "14:45"


def profile_with(**fields: object) -> ScheduleProfile:
    return ScheduleProfile.model_validate({"version": 1, "timezone": MOSCOW, **fields})


def two_busy_windows() -> ScheduleProfile:
    return profile_with(
        busy=[
            {"days": MON_WED_FRI, "from": FIRST_BUSY_FROM, "to": FIRST_BUSY_TO},
            {"days": WORKING_DAYS, "from": SECOND_BUSY_FROM, "to": SECOND_BUSY_TO},
        ]
    )


def at_local(
    day: date, hour: int, minute: int = 0, second: int = 0, zone: str = MOSCOW
) -> datetime:
    local = datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=ZoneInfo(zone))
    return local.astimezone(UTC)


def in_local(moment: datetime, zone: str = MOSCOW) -> datetime:
    return moment.astimezone(ZoneInfo(zone))
