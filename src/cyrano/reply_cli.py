from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass

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
from cyrano.domain.reply import BuiltPrompt
from cyrano.services.reply_generator import ReplyGenerator
from cyrano.services.reply_split import split_reply

REPLY_PREVIEW_COMMAND = "reply-preview"
EXIT_OK = 0
EXIT_ERROR = 1
PERSONAL_DATA_WARNING = "warning: the prompt below contains personal data; do not share it"
FAILURE_LINE = "reply-preview failed: {error}"
FAILURE_HINT_LINE = "reply-preview failed: {error} ({hint})"
PROMPT_MESSAGE_LABEL = "[{role}]"
NEEDS_OWNER_LINE = "needs owner (model signal)"
PART_LINE = "[{number}/{total}] {text}"
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


def reply_preview_request(args: argparse.Namespace) -> ReplyPreviewRequest | None:
    if args.command != REPLY_PREVIEW_COMMAND:
        return None
    return ReplyPreviewRequest(
        contact_id=args.contact_id, mode=args.mode, show_prompt=args.show_prompt
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


def _reply_lines(text: str, *, needs_owner: bool) -> list[str]:
    if needs_owner:
        return [NEEDS_OWNER_LINE]
    parts = split_reply(text)
    if len(parts) <= 1:
        return [text]
    return [
        PART_LINE.format(number=number, total=len(parts), text=part)
        for number, part in enumerate(parts, start=1)
    ]


class ReplyPreviewCommand:
    def __init__(self, generator: ReplyGenerator, model: str) -> None:
        self._generator = generator
        self._model = model

    async def run(self, request: ReplyPreviewRequest) -> int:
        try:
            prompt = await self._generator.prepare(request.contact_id, request.mode)
            if request.show_prompt:
                _print_prompt(prompt)
            reply = await self._generator.complete(prompt)
        except DomainError as exc:
            print(_failure_line(exc), file=sys.stderr)
            return EXIT_ERROR
        for line in _reply_lines(reply.text, needs_owner=reply.needs_owner):
            print(line)
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
