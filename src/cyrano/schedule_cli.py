from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, time
from pathlib import Path
from typing import NoReturn

from cyrano.domain.errors import DomainError
from cyrano.domain.schedule import TIME_FORMAT, ScheduleProfile
from cyrano.services.humanization import HumanizationPlanner, ScheduleService, blocked
from cyrano.services.repositories import ScheduleProfileRepository
from cyrano.services.schedule_files import ScheduleImporter

SCHEDULE_COMMAND = "schedule"
IMPORT_ACTION = "import"
SHOW_ACTION = "show"
CHECK_ACTION = "check"
PLAN_ACTION = "plan"
ARGUMENTS_DEST = "schedule_arguments"
EXIT_OK = 0
EXIT_ERROR = 1
USAGE = (
    'usage: python -m cyrano schedule import FILE | show | check [--at "YYYY-MM-DD HH:MM"]'
    " | plan CONTACT_ID"
)
AT_FORMAT = "%Y-%m-%d %H:%M"
BAD_AT = 'bad --at: use "YYYY-MM-DD HH:MM"'
PROBLEM_SEPARATOR = "; "
IMPORTED_LINE = "schedule profile imported: {busy} busy window(s)"
DEFAULT_IN_USE_LINE = "the default profile is in use"
STORED_LINE = "the stored profile is in use"
FREE_LINE = "free"
BLOCKED_LINE = "blocked until {until} ({timezone}), kind={kind}"
PLANNED_LINE = "planned for {send_at} ({timezone}), reason={reason}, in {seconds} s"
SHOW_LINES = (
    "timezone: {timezone}",
    "night start: {start}",
    "night end, Monday to Friday: {end_weekday}",
    "night end, Saturday and Sunday: {end_weekend}",
    "busy windows: {busy}",
    "reaction, ongoing: {ongoing} s; new: {new} s; ongoing window: {window} min",
    "read pause: {read_pause} s",
    "typing: {chars} characters per second, {typing} s",
    "postponed spread: {spread} min",
    "split pause: {split_pause} s",
)
RANGE = "{}-{}"
BUSY_WINDOW = "{days} {start}-{end}"
DAY_SEPARATOR = ","
WINDOW_SEPARATOR = "; "
NO_BUSY_WINDOWS = "none"


class ScheduleUsageError(Exception):
    pass


class _StrictParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise ScheduleUsageError(USAGE)


def _build_parser() -> argparse.ArgumentParser:
    parser = _StrictParser(prog=f"python -m cyrano {SCHEDULE_COMMAND}")
    actions = parser.add_subparsers(dest="action", required=True)
    importing = actions.add_parser(IMPORT_ACTION)
    importing.add_argument("file", type=Path)
    actions.add_parser(SHOW_ACTION)
    check = actions.add_parser(CHECK_ACTION)
    check.add_argument("--at", default=None)
    plan = actions.add_parser(PLAN_ACTION)
    plan.add_argument("contact_id", type=int)
    return parser


@dataclass(frozen=True)
class ScheduleRequest:
    action: str
    path: Path | None = None
    at: str | None = None
    contact_id: int = 0


def add_schedule_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = commands.add_parser(
        SCHEDULE_COMMAND, help="the owner's commands for the schedule profile", add_help=False
    )
    parser.add_argument(ARGUMENTS_DEST, nargs=argparse.REMAINDER)


def schedule_request(args: argparse.Namespace) -> ScheduleRequest | None:
    if args.command != SCHEDULE_COMMAND:
        return None
    parsed = _build_parser().parse_args(getattr(args, ARGUMENTS_DEST))
    return ScheduleRequest(
        action=parsed.action,
        path=getattr(parsed, "file", None),
        at=getattr(parsed, "at", None),
        contact_id=getattr(parsed, "contact_id", 0),
    )


class ScheduleCommands:
    def __init__(
        self,
        importer: ScheduleImporter,
        service: ScheduleService,
        planner: HumanizationPlanner,
        profiles: ScheduleProfileRepository,
        *,
        clock: Callable[[], datetime],
    ) -> None:
        self._importer = importer
        self._service = service
        self._planner = planner
        self._profiles = profiles
        self._clock = clock

    async def run(self, request: ScheduleRequest) -> int:
        try:
            if request.action == IMPORT_ACTION and request.path is not None:
                return await self._import(request.path)
            if request.action == SHOW_ACTION:
                return await self._show()
            if request.action == CHECK_ACTION:
                return await self._check(request.at)
            return await self._plan(request.contact_id)
        except DomainError as exc:
            print(_one_line(exc), file=sys.stderr)
            return EXIT_ERROR

    async def _import(self, path: Path) -> int:
        result = await self._importer.import_file(path)
        print(IMPORTED_LINE.format(busy=result.busy_windows))
        return EXIT_OK

    async def _show(self) -> int:
        stored = await self._profiles.get()
        print(DEFAULT_IN_USE_LINE if stored is None else STORED_LINE)
        for line in _profile_lines(await self._service.get_profile()):
            print(line)
        return EXIT_OK

    async def _check(self, at: str | None) -> int:
        profile = await self._service.get_profile()
        try:
            moment = self._clock() if at is None else _parse_at(at, profile)
        except ValueError:
            print(BAD_AT, file=sys.stderr)
            return EXIT_ERROR
        block = blocked(moment, profile)
        if block is None:
            print(FREE_LINE)
            return EXIT_OK
        print(
            BLOCKED_LINE.format(
                until=_local(block.end, profile),
                timezone=profile.timezone,
                kind=block.kind.value,
            )
        )
        return EXIT_OK

    async def _plan(self, contact_id: int) -> int:
        now = self._clock()
        plan = await self._planner.plan_latest(contact_id, now)
        profile = await self._service.get_profile()
        print(
            PLANNED_LINE.format(
                send_at=_local(plan.send_at, profile),
                timezone=profile.timezone,
                reason=plan.reason.value,
                seconds=round((plan.send_at - now).total_seconds()),
            )
        )
        return EXIT_OK


def _one_line(error: DomainError) -> str:
    return PROBLEM_SEPARATOR.join(line.strip() for line in str(error).splitlines())


def _parse_at(value: str, profile: ScheduleProfile) -> datetime:
    local = datetime.strptime(value, AT_FORMAT).replace(tzinfo=profile.zone)
    return local.astimezone(UTC)


def _local(moment: datetime, profile: ScheduleProfile) -> str:
    return moment.astimezone(profile.zone).strftime(AT_FORMAT)


def _time_range(pair: tuple[time, time]) -> str:
    return RANGE.format(pair[0].strftime(TIME_FORMAT), pair[1].strftime(TIME_FORMAT))


def _int_range(pair: tuple[int, int]) -> str:
    return RANGE.format(*pair)


def _busy(profile: ScheduleProfile) -> str:
    windows = [
        BUSY_WINDOW.format(
            days=DAY_SEPARATOR.join(day.value for day in window.days),
            start=window.start.strftime(TIME_FORMAT),
            end=window.end.strftime(TIME_FORMAT),
        )
        for window in profile.busy
    ]
    return WINDOW_SEPARATOR.join(windows) or NO_BUSY_WINDOWS


def _profile_lines(profile: ScheduleProfile) -> list[str]:
    values = {
        "timezone": profile.timezone,
        "start": _time_range(profile.night.start),
        "end_weekday": _time_range(profile.night.end_weekday),
        "end_weekend": _time_range(profile.night.end_weekend),
        "busy": _busy(profile),
        "ongoing": _int_range(profile.reaction.ongoing_seconds),
        "new": _int_range(profile.reaction.new_seconds),
        "window": profile.reaction.ongoing_window_minutes,
        "read_pause": _int_range(profile.read_pause_seconds),
        "chars": profile.typing.chars_per_second,
        "typing": _int_range((profile.typing.min_seconds, profile.typing.max_seconds)),
        "spread": _int_range(profile.postponed_spread_minutes),
        "split_pause": _int_range(profile.split_pause_seconds),
    }
    return [line.format(**values) for line in SHOW_LINES]
