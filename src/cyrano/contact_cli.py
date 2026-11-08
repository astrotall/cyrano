from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from typing import NoReturn

from cyrano.domain.enums import ContactGender
from cyrano.domain.errors import ContactNotFound, DomainError
from cyrano.domain.gender import UNKNOWN_GENDER_LABEL
from cyrano.services.gender import GenderResolver
from cyrano.services.repositories import ContactRepository

CONTACT_COMMAND = "contact"
GENDER_ACTION = "gender"
ARGUMENTS_DEST = "contact_arguments"
GENDER_CHOICES = (*(member.value for member in ContactGender), UNKNOWN_GENDER_LABEL)
EXIT_OK = 0
EXIT_ERROR = 1
USAGE = "usage: python -m cyrano contact gender CONTACT_ID female|male|unknown"
GENDER_LINE = "gender: {gender} ({source})"
CONTACT_MISSING_LINE = "contact gender failed: no contact with this id"
FAILURE_LINE = "contact gender failed: {error}"


class ContactUsageError(Exception):
    pass


class _StrictParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise ContactUsageError(USAGE)


def _build_parser() -> argparse.ArgumentParser:
    parser = _StrictParser(prog=f"python -m cyrano {CONTACT_COMMAND}")
    actions = parser.add_subparsers(dest="action", required=True)
    gender = actions.add_parser(GENDER_ACTION)
    gender.add_argument("contact_id", type=int)
    gender.add_argument("gender", choices=GENDER_CHOICES)
    return parser


@dataclass(frozen=True)
class ContactRequest:
    action: str
    contact_id: int
    gender: ContactGender | None


def add_contact_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = commands.add_parser(
        CONTACT_COMMAND, help="set the grammatical gender of one contact", add_help=False
    )
    parser.add_argument(ARGUMENTS_DEST, nargs=argparse.REMAINDER)


def contact_request(args: argparse.Namespace) -> ContactRequest | None:
    if args.command != CONTACT_COMMAND:
        return None
    parsed = _build_parser().parse_args(getattr(args, ARGUMENTS_DEST))
    chosen = None if parsed.gender == UNKNOWN_GENDER_LABEL else ContactGender(parsed.gender)
    return ContactRequest(action=parsed.action, contact_id=parsed.contact_id, gender=chosen)


class ContactCommands:
    def __init__(self, contacts: ContactRepository, gender: GenderResolver) -> None:
        self._contacts = contacts
        self._gender = gender

    async def run(self, request: ContactRequest) -> int:
        try:
            return await self._set_gender(request)
        except ContactNotFound:
            print(CONTACT_MISSING_LINE, file=sys.stderr)
            return EXIT_ERROR
        except DomainError as exc:
            print(FAILURE_LINE.format(error=type(exc).__name__), file=sys.stderr)
            return EXIT_ERROR

    async def _set_gender(self, request: ContactRequest) -> int:
        if await self._contacts.get_by_id(request.contact_id) is None:
            raise ContactNotFound(request.contact_id)
        contact = await self._contacts.set_gender(request.contact_id, request.gender)
        resolved = self._gender.resolve(contact)
        print(GENDER_LINE.format(gender=resolved.label, source=resolved.source.value))
        return EXIT_OK
