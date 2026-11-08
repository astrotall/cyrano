from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from typing import NoReturn

from cyrano.domain.enums import SummaryOutcome
from cyrano.domain.errors import ContactNotFound, DomainError
from cyrano.domain.summary import StoredSummary, SummaryUpdateResult, read_stored_summary
from cyrano.services.repositories import ContactRepository, SummaryRepository
from cyrano.services.summary_updater import SummaryUpdater

SUMMARY_COMMAND = "summary"
UPDATE_ACTION = "update"
SHOW_ACTION = "show"
CLEAR_ACTION = "clear"
ARGUMENTS_DEST = "summary_arguments"
EXIT_OK = 0
EXIT_ERROR = 1
USAGE = (
    "usage: python -m cyrano summary update CONTACT_ID | show CONTACT_ID | clear CONTACT_ID --yes"
)
SUCCESSFUL_OUTCOMES = frozenset({SummaryOutcome.UPDATED, SummaryOutcome.NOTHING_TO_DO})
UPDATE_LINE = (
    "summary update: outcome={outcome} reason={reason} messages={messages} "
    "messages_in_prompt={kept} remaining={remaining} removed={removed}"
)
UPDATE_ERROR_SUFFIX = " error={error}"
NO_VALUE = "-"
CONTACT_MISSING_LINE = "summary {action} failed: no contact with this id"
FAILURE_LINE = "summary {action} failed: {error}"
PERSONAL_DATA_WARNING = "warning: the summary below contains personal data; do not share it"
NO_SUMMARY_LINE = "no summary"
SHOW_HEADER_LINE = "updated_at: {updated_at}"
SHOW_CURSOR_LINE = "covers messages up to: {message_id}"
SHOW_TEXT_LINE = "{field}: {value}"
SHOW_LIST_HEADER = "{field}:"
SHOW_LIST_ITEM = "  - {item}"
CLEAR_NEEDS_YES_LINE = "add --yes to clear the summary of this contact; nothing was changed"
CLEARED_LINE = "summary cleared"
NOTHING_TO_CLEAR_LINE = "nothing to clear"
SHOWN_TEXT_FIELDS = ("name", "age", "gender", "city", "occupation", "mood_and_tone")
SHOWN_LIST_FIELDS = (
    "interests",
    "agreements_and_plans",
    "open_questions",
    "told_about_owner",
    "notes",
)


class SummaryUsageError(Exception):
    pass


class _StrictParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise SummaryUsageError(USAGE)


def _build_parser() -> argparse.ArgumentParser:
    parser = _StrictParser(prog=f"python -m cyrano {SUMMARY_COMMAND}")
    actions = parser.add_subparsers(dest="action", required=True)
    for action in (UPDATE_ACTION, SHOW_ACTION):
        actions.add_parser(action).add_argument("contact_id", type=int)
    clear = actions.add_parser(CLEAR_ACTION)
    clear.add_argument("contact_id", type=int)
    clear.add_argument("--yes", action="store_true")
    return parser


@dataclass(frozen=True)
class SummaryRequest:
    action: str
    contact_id: int
    yes: bool = False


def add_summary_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = commands.add_parser(
        SUMMARY_COMMAND, help="update, show or clear the summary of one contact", add_help=False
    )
    parser.add_argument(ARGUMENTS_DEST, nargs=argparse.REMAINDER)


def summary_request(args: argparse.Namespace) -> SummaryRequest | None:
    if args.command != SUMMARY_COMMAND:
        return None
    parsed = _build_parser().parse_args(getattr(args, ARGUMENTS_DEST))
    return SummaryRequest(
        action=parsed.action, contact_id=parsed.contact_id, yes=getattr(parsed, "yes", False)
    )


def _update_line(result: SummaryUpdateResult) -> str:
    line = UPDATE_LINE.format(
        outcome=result.outcome.value,
        reason=result.reason.value,
        messages=result.messages,
        kept=result.messages_in_prompt,
        remaining=NO_VALUE if result.remaining is None else result.remaining,
        removed=result.removed,
    )
    return line if result.error is None else line + UPDATE_ERROR_SUFFIX.format(error=result.error)


def _summary_lines(stored: StoredSummary) -> list[str]:
    lines = [
        SHOW_HEADER_LINE.format(updated_at=stored.updated_at.isoformat()),
        SHOW_CURSOR_LINE.format(message_id=stored.cursor.message_id),
    ]
    for field in SHOWN_TEXT_FIELDS:
        value = getattr(stored.data, field)
        if value is not None:
            lines.append(SHOW_TEXT_LINE.format(field=field, value=value))
    for field in SHOWN_LIST_FIELDS:
        items: list[str] = getattr(stored.data, field)
        if items:
            lines.append(SHOW_LIST_HEADER.format(field=field))
            lines.extend(SHOW_LIST_ITEM.format(item=item) for item in items)
    return lines


class SummaryCommands:
    def __init__(
        self, updater: SummaryUpdater, contacts: ContactRepository, summaries: SummaryRepository
    ) -> None:
        self._updater = updater
        self._contacts = contacts
        self._summaries = summaries

    async def run(self, request: SummaryRequest) -> int:
        try:
            if request.action == UPDATE_ACTION:
                return await self._update(request.contact_id)
            if request.action == SHOW_ACTION:
                return await self._show(request.contact_id)
            return await self._clear(request)
        except ContactNotFound:
            print(CONTACT_MISSING_LINE.format(action=request.action), file=sys.stderr)
            return EXIT_ERROR
        except DomainError as exc:
            print(
                FAILURE_LINE.format(action=request.action, error=type(exc).__name__),
                file=sys.stderr,
            )
            return EXIT_ERROR

    async def _update(self, contact_id: int) -> int:
        result = await self._updater.update(contact_id)
        if result.outcome is SummaryOutcome.CONTACT_MISSING:
            raise ContactNotFound(contact_id)
        if result.outcome in SUCCESSFUL_OUTCOMES:
            print(_update_line(result))
            return EXIT_OK
        print(_update_line(result), file=sys.stderr)
        return EXIT_ERROR

    async def _show(self, contact_id: int) -> int:
        contact = await self._contacts.get_by_id(contact_id)
        if contact is None:
            raise ContactNotFound(contact_id)
        stored = read_stored_summary(contact.summary)
        if stored is None:
            print(NO_SUMMARY_LINE)
            return EXIT_OK
        print(PERSONAL_DATA_WARNING, file=sys.stderr)
        for line in _summary_lines(stored):
            print(line)
        return EXIT_OK

    async def _clear(self, request: SummaryRequest) -> int:
        if not request.yes:
            print(CLEAR_NEEDS_YES_LINE, file=sys.stderr)
            return EXIT_ERROR
        cleared = await self._summaries.clear_summary(request.contact_id)
        print(CLEARED_LINE if cleared else NOTHING_TO_CLEAR_LINE)
        return EXIT_OK
