"""The stop-check command: a dry run of the stop-trigger detector that never writes or echoes."""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from typing import NoReturn

from cyrano.domain.enums import MessageDirection
from cyrano.domain.stop_triggers import StopDecision
from cyrano.services.repositories import ContactRepository, MessageRepository
from cyrano.services.stop_triggers import detect

STOP_CHECK_COMMAND = "stop-check"
ARGUMENTS_DEST = "stop_check_arguments"
PASSTHROUGH_PREFIX_CHARS = "\x00"
EXIT_OK = 0
EXIT_ERROR = 1
STOP_CHECK_MESSAGES = 3
STOP_CHECK_HISTORY_WINDOW = 50
USAGE = "usage: python -m cyrano stop-check CONTACT_ID | --text TEXT"
CLASSES_LINE = "classes: {classes}"
MESSAGE_LINE = "message {number}: {classes}"
NO_INCOMING_LINE = "no incoming message"
NOT_FOUND_LINE = "stop-check failed: no contact with this id"
NO_CLASSES = "none"
CLASS_SEPARATOR = ", "


class StopCheckUsageError(Exception):
    pass


class _StrictParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise StopCheckUsageError(USAGE)


def _build_parser() -> argparse.ArgumentParser:
    parser = _StrictParser(prog=f"python -m cyrano {STOP_CHECK_COMMAND}", add_help=False)
    parser.add_argument("contact_id", type=int, nargs="?", default=None)
    parser.add_argument("--text", default=None)
    return parser


@dataclass(frozen=True)
class StopCheckRequest:
    contact_id: int | None = None
    text: str | None = field(default=None, repr=False)


def add_stop_check_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = commands.add_parser(
        STOP_CHECK_COMMAND,
        help="dry run of the stop-trigger detector; prints class names only",
        add_help=False,
        prefix_chars=PASSTHROUGH_PREFIX_CHARS,
    )
    parser.add_argument(ARGUMENTS_DEST, nargs=argparse.REMAINDER)


def stop_check_request(args: argparse.Namespace) -> StopCheckRequest | None:
    if args.command != STOP_CHECK_COMMAND:
        return None
    parsed = _build_parser().parse_args(getattr(args, ARGUMENTS_DEST))
    if (parsed.contact_id is None) == (parsed.text is None):
        raise StopCheckUsageError(USAGE)
    return StopCheckRequest(contact_id=parsed.contact_id, text=parsed.text)


def format_classes(decision: StopDecision) -> str:
    names = [trigger.value for trigger in decision.ordered()]
    return CLASS_SEPARATOR.join(names) or NO_CLASSES


def run_text_check(text: str) -> int:
    print(CLASSES_LINE.format(classes=format_classes(detect(text))))
    return EXIT_OK


class StopCheckCommands:
    def __init__(self, contacts: ContactRepository, messages: MessageRepository) -> None:
        self._contacts = contacts
        self._messages = messages

    async def run_contact(self, contact_id: int) -> int:
        if await self._contacts.get_by_id(contact_id) is None:
            print(NOT_FOUND_LINE, file=sys.stderr)
            return EXIT_ERROR
        history = await self._messages.list_last(contact_id, STOP_CHECK_HISTORY_WINDOW)
        incoming = [m for m in reversed(history) if m.direction is MessageDirection.INCOMING]
        newest = incoming[:STOP_CHECK_MESSAGES]
        if not newest:
            print(NO_INCOMING_LINE)
            return EXIT_OK
        for number, message in enumerate(newest, start=1):
            classes = format_classes(detect(message.text))
            print(MESSAGE_LINE.format(number=number, classes=classes))
        return EXIT_OK
