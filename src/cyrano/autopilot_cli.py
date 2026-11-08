from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import NoReturn

from cyrano.domain.autopilot import AutopilotState
from cyrano.domain.enums import OutboxSource
from cyrano.domain.errors import DomainError
from cyrano.services.repositories import (
    AutopilotStateRepository,
    OutboxRepository,
    ReplyBacklogRepository,
)

AUTOPILOT_COMMAND = "autopilot"
STATUS_ACTION = "status"
RESUME_ACTION = "resume"
ARGUMENTS_DEST = "autopilot_arguments"
EXIT_OK = 0
EXIT_ERROR = 1
USAGE = "usage: python -m cyrano autopilot status | resume [--yes]"
SWITCH_LINE = "switch: AUTOPILOT_ENABLED={value}"
SWITCH_ON = "true"
SWITCH_OFF = "false"
NO_REASON = "-"
RUNNING_LINE = "autopilot: running"
STOPPED_LINE = "autopilot: stopped reason={reason}"
PAUSED_LINE = "autopilot: paused until {until} reason={reason}"
ROWS_LINE = "autopilot rows: {counts}"
ROW_COUNT = "{state}={count}"
UNANSWERED_LINE = "active contacts with an unanswered incoming message: {count}"
PROACTIVE_SWITCH_LINE = "switch: PROACTIVE_ENABLED={value}"
PROACTIVE_ROWS_LINE = "proactive rows: {counts}"
RESUME_NOTHING_LINE = "nothing to clear: the autopilot is running"
RESUME_PAUSE_LINE = "pause cleared, it was until {until} reason={reason}"
RESUME_STOP_LINE = "stop cleared, the reason was {reason}"
RESUME_NEEDS_YES_LINE = (
    "the autopilot is stopped, reason={reason}; nothing was cleared; "
    "fix the cause first, then add --yes to clear the stop"
)


class AutopilotUsageError(Exception):
    pass


class _StrictParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise AutopilotUsageError(USAGE)


def _build_parser() -> argparse.ArgumentParser:
    parser = _StrictParser(prog=f"python -m cyrano {AUTOPILOT_COMMAND}")
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser(STATUS_ACTION)
    resume = actions.add_parser(RESUME_ACTION)
    resume.add_argument("--yes", action="store_true")
    return parser


@dataclass(frozen=True)
class AutopilotRequest:
    action: str
    yes: bool = False


def add_autopilot_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = commands.add_parser(
        AUTOPILOT_COMMAND, help="the state of the autopilot and its resume", add_help=False
    )
    parser.add_argument(ARGUMENTS_DEST, nargs=argparse.REMAINDER)


def autopilot_request(args: argparse.Namespace) -> AutopilotRequest | None:
    if args.command != AUTOPILOT_COMMAND:
        return None
    parsed = _build_parser().parse_args(getattr(args, ARGUMENTS_DEST))
    return AutopilotRequest(action=parsed.action, yes=getattr(parsed, "yes", False))


class AutopilotCommands:
    def __init__(
        self,
        state: AutopilotStateRepository,
        outbox: OutboxRepository,
        backlog: ReplyBacklogRepository,
        *,
        enabled: bool,
        clock: Callable[[], datetime],
        proactive_enabled: bool = False,
    ) -> None:
        self._state = state
        self._outbox = outbox
        self._backlog = backlog
        self._enabled = enabled
        self._proactive_enabled = proactive_enabled
        self._clock = clock

    async def run(self, request: AutopilotRequest) -> int:
        try:
            if request.action == STATUS_ACTION:
                return await self._status()
            return await self._resume(request)
        except DomainError as exc:
            print(exc, file=sys.stderr)
            return EXIT_ERROR

    async def _status(self) -> int:
        now = self._clock()
        print(SWITCH_LINE.format(value=SWITCH_ON if self._enabled else SWITCH_OFF))
        for line in _state_lines(await self._state.get(), now):
            print(line)
        print(ROWS_LINE.format(counts=await self._counts(OutboxSource.AUTOPILOT)))
        print(UNANSWERED_LINE.format(count=await self._backlog.count_unanswered_active()))
        print(
            PROACTIVE_SWITCH_LINE.format(value=SWITCH_ON if self._proactive_enabled else SWITCH_OFF)
        )
        print(PROACTIVE_ROWS_LINE.format(counts=await self._counts(OutboxSource.PROACTIVE)))
        return EXIT_OK

    async def _counts(self, source: OutboxSource) -> str:
        counts = await self._outbox.count_by_state(source)
        return " ".join(
            ROW_COUNT.format(state=state.value, count=count) for state, count in counts.items()
        )

    async def _resume(self, request: AutopilotRequest) -> int:
        now = self._clock()
        current = await self._state.get()
        if current.stopped and not request.yes:
            print(RESUME_NEEDS_YES_LINE.format(reason=_value(current.stop_reason)), file=sys.stderr)
            return EXIT_ERROR
        if current.is_running(now):
            print(RESUME_NOTHING_LINE)
            return EXIT_OK
        before = await self._state.resume()
        lines = _cleared_lines(before, now)
        for line in lines or [RESUME_NOTHING_LINE]:
            print(line)
        return EXIT_OK


def _value(reason: StrEnum | None) -> str:
    return NO_REASON if reason is None else reason.value


def _state_lines(state: AutopilotState, now: datetime) -> list[str]:
    lines: list[str] = []
    if state.stopped:
        lines.append(STOPPED_LINE.format(reason=_value(state.stop_reason)))
    if state.paused_until is not None and state.is_paused(now):
        lines.append(
            PAUSED_LINE.format(
                until=state.paused_until.isoformat(), reason=_value(state.pause_reason)
            )
        )
    return lines or [RUNNING_LINE]


def _cleared_lines(before: AutopilotState, now: datetime) -> list[str]:
    lines: list[str] = []
    if before.stopped:
        lines.append(RESUME_STOP_LINE.format(reason=_value(before.stop_reason)))
    if before.paused_until is not None and before.is_paused(now):
        lines.append(
            RESUME_PAUSE_LINE.format(
                until=before.paused_until.isoformat(), reason=_value(before.pause_reason)
            )
        )
    return lines
