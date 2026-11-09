from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import NoReturn

from cyrano.domain.enums import LimitScope, OutboxSource, OutboxState
from cyrano.domain.errors import DomainError, EnqueueRefused, OutboxStateConflict
from cyrano.domain.outbox import CANCEL_REASON_OWNER, SenderState
from cyrano.services.outbox_service import OutboxService
from cyrano.services.repositories import OutboxRepository, SenderStateRepository
from cyrano.services.send_limiter import LimitUsage, SendLimiter

OUTBOX_COMMAND = "outbox"
ENQUEUE_ACTION = "enqueue"
LIST_ACTION = "list"
CANCEL_ACTION = "cancel"
RETRY_ACTION = "retry"
STATUS_ACTION = "status"
RESUME_ACTION = "resume"
ARGUMENTS_DEST = "outbox_arguments"
EXIT_OK = 0
EXIT_ERROR = 1
DEFAULT_IN_SECONDS = 5
MIN_IN_SECONDS = 0
MAX_IN_SECONDS = 86400
DEFAULT_LIST_LIMIT = 50
MIN_LIST_LIMIT = 1
MAX_LIST_LIMIT = 1000
USAGE = (
    "usage: python -m cyrano outbox enqueue CONTACT_ID TEXT [--in-seconds N]"
    " | list [--state S] [--limit N] | cancel ID | retry ID [--in-seconds N] [--yes]"
    " | status | resume [--yes]"
)
IN_SECONDS_RANGE = f"--in-seconds must be from {MIN_IN_SECONDS} to {MAX_IN_SECONDS}"
LIMIT_RANGE = f"--limit must be from {MIN_LIST_LIMIT} to {MAX_LIST_LIMIT}"
ENQUEUED_LINE = "outbox row {row_id} planned for {send_at}"
REFUSED_LINE = "outbox enqueue refused: {reason}"
CANCELLED_LINE = "outbox row {row_id} cancelled"
CANCEL_REFUSED_LINE = "outbox row {row_id} is {state} and cannot be cancelled"
RETRY_REFUSED_LINE = "outbox row {row_id} is {state} and cannot be retried"
RETRY_NEEDS_YES_LINE = (
    "check the chat by hand first, the message may have arrived; "
    "to send outbox row {row_id} again, add --yes"
)
LIST_LINE = (
    "id={id} contact={contact_id} source={source} state={state} send_at={send_at} due_at={due_at} "
    "error={error} cancel_reason={cancel_reason} quoted={quoted}"
)
QUOTED_YES = "yes"
QUOTED_NO = "no"
NO_ERROR = "-"
NO_CANCEL_REASON = "-"
SENDER_RUNNING_LINE = "sender: running"
SENDER_PAUSED_LINE = "sender: paused until {until} reason={reason}"
SENDER_STOPPED_LINE = "sender: stopped reason={reason}"
USAGE_LINE = "{scope} {window} {used}/{maximum}"
USAGE_CONTACT_SUFFIX = " (contact {contact_id})"
ROWS_LINE = "rows: {counts}"
ROW_COUNT = "{state}={count}"
RESUME_NOTHING_LINE = "nothing to clear: the sender is running"
RESUME_PAUSE_LINE = "pause cleared, it was until {until} reason={reason}"
RESUME_STOP_LINE = "stop cleared, the reason was {reason}"
RESUME_NEEDS_YES_LINE = (
    "the sender is stopped, reason={reason}; nothing was cleared; "
    "check the account by hand first, then add --yes to clear the stop"
)


class OutboxUsageError(Exception):
    pass


class _StrictParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise OutboxUsageError(USAGE)


def _build_parser() -> argparse.ArgumentParser:
    parser = _StrictParser(prog=f"python -m cyrano {OUTBOX_COMMAND}")
    actions = parser.add_subparsers(dest="action", required=True)
    enqueue = actions.add_parser(ENQUEUE_ACTION)
    enqueue.add_argument("contact_id", type=int)
    enqueue.add_argument("text")
    enqueue.add_argument("--in-seconds", type=int, default=DEFAULT_IN_SECONDS, dest="in_seconds")
    listing = actions.add_parser(LIST_ACTION)
    listing.add_argument("--state", type=OutboxState, default=None)
    listing.add_argument("--limit", type=int, default=DEFAULT_LIST_LIMIT)
    cancel = actions.add_parser(CANCEL_ACTION)
    cancel.add_argument("row_id", type=int)
    retry = actions.add_parser(RETRY_ACTION)
    retry.add_argument("row_id", type=int)
    retry.add_argument("--in-seconds", type=int, default=DEFAULT_IN_SECONDS, dest="in_seconds")
    retry.add_argument("--yes", action="store_true")
    actions.add_parser(STATUS_ACTION)
    resume = actions.add_parser(RESUME_ACTION)
    resume.add_argument("--yes", action="store_true")
    return parser


@dataclass(frozen=True)
class OutboxRequest:
    action: str
    contact_id: int = 0
    text: str = field(default="", repr=False)
    in_seconds: int = DEFAULT_IN_SECONDS
    state: OutboxState | None = None
    limit: int = DEFAULT_LIST_LIMIT
    row_id: int = 0
    yes: bool = False


def add_outbox_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = commands.add_parser(
        OUTBOX_COMMAND, help="the owner's test commands for the outbox", add_help=False
    )
    parser.add_argument(ARGUMENTS_DEST, nargs=argparse.REMAINDER)


def outbox_request(args: argparse.Namespace) -> OutboxRequest | None:
    if args.command != OUTBOX_COMMAND:
        return None
    parsed = _build_parser().parse_args(getattr(args, ARGUMENTS_DEST))
    return OutboxRequest(
        action=parsed.action,
        contact_id=getattr(parsed, "contact_id", 0),
        text=getattr(parsed, "text", ""),
        in_seconds=getattr(parsed, "in_seconds", DEFAULT_IN_SECONDS),
        state=getattr(parsed, "state", None),
        limit=getattr(parsed, "limit", DEFAULT_LIST_LIMIT),
        row_id=getattr(parsed, "row_id", 0),
        yes=getattr(parsed, "yes", False),
    )


class OutboxCommands:
    def __init__(
        self,
        service: OutboxService,
        outbox: OutboxRepository,
        state: SenderStateRepository,
        limiter: SendLimiter,
        *,
        clock: Callable[[], datetime],
    ) -> None:
        self._service = service
        self._outbox = outbox
        self._state = state
        self._limiter = limiter
        self._clock = clock

    async def run(self, request: OutboxRequest) -> int:
        try:
            if request.action == ENQUEUE_ACTION:
                return await self._enqueue(request)
            if request.action == LIST_ACTION:
                return await self._list(request)
            if request.action == RETRY_ACTION:
                return await self._retry(request)
            if request.action == STATUS_ACTION:
                return await self._status()
            if request.action == RESUME_ACTION:
                return await self._resume(request)
            return await self._cancel(request)
        except DomainError as exc:
            print(exc, file=sys.stderr)
            return EXIT_ERROR

    async def _enqueue(self, request: OutboxRequest) -> int:
        if not MIN_IN_SECONDS <= request.in_seconds <= MAX_IN_SECONDS:
            print(IN_SECONDS_RANGE, file=sys.stderr)
            return EXIT_ERROR
        send_at = self._clock() + timedelta(seconds=request.in_seconds)
        try:
            row = await self._service.enqueue(
                request.contact_id, request.text, send_at, OutboxSource.MANUAL_TEST
            )
        except EnqueueRefused as exc:
            print(REFUSED_LINE.format(reason=exc.reason), file=sys.stderr)
            return EXIT_ERROR
        print(ENQUEUED_LINE.format(row_id=row.id, send_at=row.send_at.isoformat()))
        return EXIT_OK

    async def _list(self, request: OutboxRequest) -> int:
        if not MIN_LIST_LIMIT <= request.limit <= MAX_LIST_LIMIT:
            print(LIMIT_RANGE, file=sys.stderr)
            return EXIT_ERROR
        for row in await self._outbox.list_rows(request.state, request.limit):
            print(
                LIST_LINE.format(
                    id=row.id,
                    contact_id=row.contact_id,
                    source=row.source.value,
                    state=row.state.value,
                    send_at=row.send_at.isoformat(),
                    due_at=row.due_at.isoformat(),
                    error=row.error_class or NO_ERROR,
                    cancel_reason=row.cancel_reason or NO_CANCEL_REASON,
                    quoted=QUOTED_NO if row.reply_to_tg_message_id is None else QUOTED_YES,
                )
            )
        return EXIT_OK

    async def _cancel(self, request: OutboxRequest) -> int:
        try:
            await self._outbox.cancel_scheduled(request.row_id, CANCEL_REASON_OWNER)
        except OutboxStateConflict as exc:
            print(
                CANCEL_REFUSED_LINE.format(row_id=exc.row_id, state=exc.state.value),
                file=sys.stderr,
            )
            return EXIT_ERROR
        print(CANCELLED_LINE.format(row_id=request.row_id))
        return EXIT_OK

    async def _retry(self, request: OutboxRequest) -> int:
        if not MIN_IN_SECONDS <= request.in_seconds <= MAX_IN_SECONDS:
            print(IN_SECONDS_RANGE, file=sys.stderr)
            return EXIT_ERROR
        if not request.yes:
            print(RETRY_NEEDS_YES_LINE.format(row_id=request.row_id), file=sys.stderr)
            return EXIT_ERROR
        send_at = self._clock() + timedelta(seconds=request.in_seconds)
        try:
            row = await self._outbox.retry_failed(request.row_id, send_at)
        except OutboxStateConflict as exc:
            print(
                RETRY_REFUSED_LINE.format(row_id=exc.row_id, state=exc.state.value),
                file=sys.stderr,
            )
            return EXIT_ERROR
        print(ENQUEUED_LINE.format(row_id=row.id, send_at=row.send_at.isoformat()))
        return EXIT_OK

    async def _status(self) -> int:
        now = self._clock()
        for line in _sender_lines(await self._state.get(), now):
            print(line)
        for usage in await self._limiter.usage(now):
            print(_usage_line(usage))
        counts = await self._outbox.count_by_state()
        print(
            ROWS_LINE.format(
                counts=" ".join(
                    ROW_COUNT.format(state=state.value, count=count)
                    for state, count in counts.items()
                )
            )
        )
        return EXIT_OK

    async def _resume(self, request: OutboxRequest) -> int:
        now = self._clock()
        before = await self._state.resume(keep_stop=not request.yes)
        if before.stopped and not request.yes:
            print(RESUME_NEEDS_YES_LINE.format(reason=before.stop_reason), file=sys.stderr)
            return EXIT_ERROR
        lines = _cleared_lines(before, now)
        for line in lines or [RESUME_NOTHING_LINE]:
            print(line)
        return EXIT_OK


def _sender_lines(state: SenderState, now: datetime) -> list[str]:
    lines: list[str] = []
    if state.stopped:
        lines.append(SENDER_STOPPED_LINE.format(reason=state.stop_reason))
    if state.paused_until is not None and state.is_paused(now):
        lines.append(
            SENDER_PAUSED_LINE.format(
                until=state.paused_until.isoformat(), reason=state.pause_reason
            )
        )
    return lines or [SENDER_RUNNING_LINE]


def _usage_line(usage: LimitUsage) -> str:
    line = USAGE_LINE.format(
        scope=usage.limit.scope.value,
        window=usage.limit.window.value,
        used=usage.used,
        maximum=usage.limit.maximum,
    )
    if usage.limit.scope is LimitScope.CONTACT and usage.contact_id is not None:
        return line + USAGE_CONTACT_SUFFIX.format(contact_id=usage.contact_id)
    return line


def _cleared_lines(before: SenderState, now: datetime) -> list[str]:
    lines: list[str] = []
    if before.stopped:
        lines.append(RESUME_STOP_LINE.format(reason=before.stop_reason))
    if before.paused_until is not None and before.is_paused(now):
        lines.append(
            RESUME_PAUSE_LINE.format(
                until=before.paused_until.isoformat(), reason=before.pause_reason
            )
        )
    return lines
