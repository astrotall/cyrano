from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from cyrano.domain.enums import ReplyPurpose
from cyrano.domain.errors import (
    ContactNotFound,
    DefaultModeUnavailable,
    DomainError,
    LLMError,
    ModeNameNotFound,
    NoMessagesToReplyTo,
    PersonaMissing,
    PromptTooLarge,
    ReplyInvalid,
)
from cyrano.domain.reply import BuiltPrompt, MessageRef, ReplyPart
from cyrano.domain.schedule import UnavailableSoon
from cyrano.services.gender import GenderResolver
from cyrano.services.humanization import ScheduleService
from cyrano.services.prompt_builder import wrapup_reason_text
from cyrano.services.reply_generator import ReplyGenerator
from cyrano.services.reply_quotes import quote_label, split_reply_with_targets
from cyrano.services.repositories import ContactRepository

REPLY_PREVIEW_COMMAND = "reply-preview"
EXIT_OK = 0
EXIT_ERROR = 1
PERSONAL_DATA_WARNING = "warning: the prompt below contains personal data; do not share it"
FAILURE_LINE = "reply-preview failed: {error}"
FAILURE_HINT_LINE = "reply-preview failed: {error} ({hint})"
PROMPT_MESSAGE_LABEL = "[{role}]"
NEEDS_OWNER_LINE = "needs owner (model signal)"
SKIP_LINE = "skip (model signal)"
WRAPUP_PREVIEW_LINE = "wrap-up: {phase}, {minutes} min, {reason}"
AT_FORMAT = "%Y-%m-%d %H:%M"
BAD_AT_LINE = 'reply-preview failed: bad --at, use "YYYY-MM-DD HH:MM"'
PART_LINE = "[{number}/{total}] {text}"
QUOTED_PART_LINE = "[{number}/{total} {quote}] {text}"
GENDER_LINE = "gender: {gender} ({source})"
STATS_LINE = (
    "model={model} input_tokens={input_tokens} output_tokens={output_tokens} "
    "examples={examples_kept}/{examples_offered} messages={messages_kept}/{messages_offered} "
    "prompt_characters={prompt_characters}"
)
FAILURE_HINTS: dict[type[DomainError], str] = {
    ContactNotFound: "no contact with this id",
    PersonaMissing: "import a persona first",
    NoMessagesToReplyTo: "the contact has no incoming message",
    ModeNameNotFound: "no mode with this name",
    DefaultModeUnavailable: "the default mode is missing or disabled",
    PromptTooLarge: "raise REPLY_PROMPT_BUDGET_CHARS or shorten the persona",
    ReplyInvalid: "the model reply was refused",
    LLMError: "the language model call failed",
}


@dataclass(frozen=True)
class ReplyPreviewRequest:
    contact_id: int
    mode: str | None = None
    show_prompt: bool = False
    proactive: bool = False
    at: str | None = None


def add_reply_preview_parser(
    commands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    parser = commands.add_parser(
        REPLY_PREVIEW_COMMAND,
        help="generate one reply for a stored conversation and print it; nothing is sent",
    )
    parser.add_argument("contact_id", type=int)
    parser.add_argument("--mode", default=None)
    parser.add_argument("--show-prompt", action="store_true", dest="show_prompt")
    parser.add_argument("--proactive", action="store_true")
    parser.add_argument("--at", default=None)


def reply_preview_request(args: argparse.Namespace) -> ReplyPreviewRequest | None:
    if args.command != REPLY_PREVIEW_COMMAND:
        return None
    return ReplyPreviewRequest(
        contact_id=args.contact_id,
        mode=args.mode,
        show_prompt=args.show_prompt,
        proactive=args.proactive,
        at=args.at,
    )


def _failure_line(error: DomainError) -> str:
    name = type(error).__name__
    for error_type, hint in FAILURE_HINTS.items():
        if isinstance(error, error_type):
            return FAILURE_HINT_LINE.format(error=name, hint=hint)
    return FAILURE_LINE.format(error=name)


def _print_prompt(prompt: BuiltPrompt) -> None:
    print(PERSONAL_DATA_WARNING, file=sys.stderr)
    for message in prompt.messages:
        print(PROMPT_MESSAGE_LABEL.format(role=message.role.value))
        print(message.content)


def _part_line(number: int, total: int, part: ReplyPart) -> str:
    if part.quote_number is None:
        return PART_LINE.format(number=number, total=total, text=part.text)
    return QUOTED_PART_LINE.format(
        number=number, total=total, quote=quote_label(part.quote_number), text=part.text
    )


def _reply_lines(
    text: str,
    refs: dict[int, MessageRef],
    *,
    needs_owner: bool,
    skipped: bool = False,
) -> list[str]:
    if needs_owner:
        return [NEEDS_OWNER_LINE]
    if skipped:
        return [SKIP_LINE]
    parts = split_reply_with_targets(text, refs)
    quoted = any(part.quote_number is not None for part in parts)
    if not parts:
        return [text]
    if len(parts) == 1 and not quoted:
        return [parts[0].text]
    return [_part_line(number, len(parts), part) for number, part in enumerate(parts, start=1)]


class PreviewClock:
    def __init__(self, now: Callable[[], datetime]) -> None:
        self._now = now
        self._fixed: datetime | None = None

    def fix(self, at: datetime) -> None:
        self._fixed = at

    def __call__(self) -> datetime:
        return self._now() if self._fixed is None else self._fixed


def _wrapup_line(soon: UnavailableSoon) -> str:
    return WRAPUP_PREVIEW_LINE.format(
        phase=soon.phase.value, minutes=soon.minutes_left, reason=wrapup_reason_text(soon)
    )


class ReplyPreviewCommand:
    def __init__(
        self,
        generator: ReplyGenerator,
        model: str,
        contacts: ContactRepository,
        gender: GenderResolver,
        *,
        schedule: ScheduleService | None = None,
        clock: PreviewClock | None = None,
        wrapup_enabled: bool = True,
    ) -> None:
        self._generator = generator
        self._model = model
        self._contacts = contacts
        self._gender = gender
        self._schedule = schedule
        self._clock = clock if clock is not None else PreviewClock(lambda: datetime.now(UTC))
        self._wrapup_enabled = wrapup_enabled

    async def _fix_clock(self, at: str | None) -> None:
        if at is None:
            return
        parsed = datetime.strptime(at, AT_FORMAT)
        zone = UTC if self._schedule is None else (await self._schedule.get_profile()).zone
        self._clock.fix(parsed.replace(tzinfo=zone).astimezone(UTC))

    async def _wrapup(self, purpose: ReplyPurpose) -> UnavailableSoon | None:
        if self._schedule is None or not self._wrapup_enabled or purpose is ReplyPurpose.PROACTIVE:
            return None
        return await self._schedule.unavailable_soon(self._clock())

    async def run(self, request: ReplyPreviewRequest) -> int:
        try:
            await self._fix_clock(request.at)
        except ValueError:
            print(BAD_AT_LINE, file=sys.stderr)
            return EXIT_ERROR
        try:
            purpose = ReplyPurpose.PROACTIVE if request.proactive else ReplyPurpose.REPLY
            wrapup = await self._wrapup(purpose)
            prompt = await self._generator.prepare(
                request.contact_id, request.mode, purpose=purpose, wrapup=wrapup
            )
            if request.show_prompt:
                _print_prompt(prompt)
            reply = await self._generator.complete(prompt)
            gender_line = await self._gender_line(request.contact_id)
        except DomainError as exc:
            print(_failure_line(exc), file=sys.stderr)
            return EXIT_ERROR
        for line in _reply_lines(
            reply.text, reply.message_refs, needs_owner=reply.needs_owner, skipped=reply.skipped
        ):
            print(line)
        print(gender_line)
        if wrapup is not None:
            print(_wrapup_line(wrapup))
        print(
            STATS_LINE.format(
                model=self._model,
                input_tokens=reply.usage.input_tokens,
                output_tokens=reply.usage.output_tokens,
                examples_kept=reply.examples_kept,
                examples_offered=reply.examples_offered,
                messages_kept=reply.messages_kept,
                messages_offered=reply.messages_offered,
                prompt_characters=reply.prompt_characters,
            )
        )
        return EXIT_OK

    async def _gender_line(self, contact_id: int) -> str:
        contact = await self._contacts.get_by_id(contact_id)
        if contact is None:
            raise ContactNotFound(contact_id)
        resolved = self._gender.resolve(contact)
        return GENDER_LINE.format(gender=resolved.label, source=resolved.source.value)
