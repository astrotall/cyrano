"""Turning a Telegram Desktop chat export into a draft of the style examples file."""

from __future__ import annotations

import json
import logging
import math
import os
import secrets
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from cyrano.domain.chat_export import (
    MAX_GAP_HOURS,
    MAX_WINDOW,
    MIN_WINDOW,
    OUTPUT_DIRECTORY,
    PART_SUFFIX,
    PRIVATE_FILE_MODE,
    ConvertOptions,
    ConvertReport,
    Dialogue,
    ExportMessage,
    RedactionKind,
)
from cyrano.domain.errors import ChatExportError
from cyrano.domain.style import (
    MAX_DIALOGUES_PER_FILE,
    MAX_TAGS_PER_DIALOGUE,
    TAG_RULE,
    StyleExampleCreate,
    Tags,
    Turn,
)
from cyrano.services.chat_dialogues import (
    build_turns,
    dialogue_candidates,
    drop_duplicates,
    sample_evenly,
    split_conversations,
    truncate_turns,
)
from cyrano.services.chat_export_reader import (
    ExportData,
    load_export_file,
    message_items,
    read_messages,
)
from cyrano.services.chat_redaction import Redactor
from cyrano.services.style_files import FILE_FORMAT_VERSION, parse_examples, read_import_file

logger = logging.getLogger(__name__)

OWNER_NOT_FOUND = "--me matches none of the {count} sender(s) found in the export"
OWNER_ID_NOT_FOUND = "--me-id matches none of the {count} sender id(s) found in the export"
OWNER_OPTION_COUNT = "give exactly one of --me and --me-id"
NO_DIALOGUES = "no dialogue could be built from this export; nothing was written"
ME_EMPTY = "--me and --me-id must not be empty"
MAX_OUT_OF_RANGE = f"--max must be from 1 to {MAX_DIALOGUES_PER_FILE}"
WINDOW_OUT_OF_RANGE = f"--window must be from {MIN_WINDOW} to {MAX_WINDOW}"
GAP_OUT_OF_RANGE = f"--gap-hours must be greater than 0 and at most {MAX_GAP_HOURS:g}"
REDACT_WITHOUT_LETTERS = "--redact needs a word with letters"
TAGS_INVALID = f"--tags is invalid: each tag {TAG_RULE}, at most {MAX_TAGS_PER_DIALOGUE} tags"
OUTPUT_OUTSIDE = (
    f"--out must be inside the {OUTPUT_DIRECTORY}/ folder of the current directory; "
    "use --allow-any-path to write elsewhere"
)
OUTPUT_EXISTS = "output file already exists: {path}; use --force to replace it"
OUTPUT_IS_DIRECTORY = "output path is a directory: {path}"
OUTPUT_FAILED = "cannot write the draft: {error}"
TAGS_ADAPTER: TypeAdapter[list[str]] = TypeAdapter(Tags)


@dataclass(frozen=True, slots=True)
class Draft:
    dialogues: tuple[Dialogue, ...]
    report: ConvertReport


def normalize_tags(raw_tags: Sequence[str]) -> tuple[str, ...]:
    try:
        return tuple(TAGS_ADAPTER.validate_python(list(raw_tags)))
    except ValidationError:
        raise ChatExportError(TAGS_INVALID) from None


def validate_options(options: ConvertOptions) -> None:
    given = [value for value in (options.me, options.me_id) if value is not None]
    if len(given) != 1:
        raise ChatExportError(OWNER_OPTION_COUNT)
    if not given[0].strip():
        raise ChatExportError(ME_EMPTY)
    if not 1 <= options.max_dialogues <= MAX_DIALOGUES_PER_FILE:
        raise ChatExportError(MAX_OUT_OF_RANGE)
    if not MIN_WINDOW <= options.window <= MAX_WINDOW:
        raise ChatExportError(WINDOW_OUT_OF_RANGE)
    if not (math.isfinite(options.gap_hours) and 0 < options.gap_hours <= MAX_GAP_HOURS):
        raise ChatExportError(GAP_OUT_OF_RANGE)
    if any(not any(character.isalpha() for character in word) for word in options.redact_words):
        raise ChatExportError(REDACT_WITHOUT_LETTERS)


def owner_matcher(options: ConvertOptions) -> Callable[[ExportMessage], bool]:
    if options.me_id is not None:
        owner_id = options.me_id
        return lambda message: message.sender_id == owner_id
    owner_name = options.me
    return lambda message: message.sender == owner_name


def check_owner_present(data: ExportData, options: ConvertOptions) -> None:
    if options.me_id is not None:
        if options.me_id not in data.sender_ids:
            raise ChatExportError(OWNER_ID_NOT_FOUND.format(count=len(data.sender_ids)))
    elif options.me not in data.senders:
        raise ChatExportError(OWNER_NOT_FOUND.format(count=len(data.senders)))


def build_draft(data: ExportData, options: ConvertOptions) -> Draft:
    check_owner_present(data, options)
    is_owner = owner_matcher(options)
    redactor = Redactor(data.senders, options.redact_words)
    redactions: Counter[RedactionKind] = Counter()
    messages: list[ExportMessage] = []
    dropped = 0
    for message in data.messages:
        redacted = redactor.redact(message.text)
        redactions.update(redacted.counts)
        if redacted.only_tokens:
            dropped += 1
        else:
            messages.append(replace(message, text=redacted.text))

    conversations = split_conversations(messages, timedelta(hours=options.gap_hours))
    turn_count = 0
    truncations = 0
    candidates: list[Dialogue] = []
    for conversation in conversations:
        turns, truncated = truncate_turns(build_turns(conversation, is_owner))
        turn_count += len(turns)
        truncations += truncated
        candidates.extend(dialogue_candidates(turns, options.window))
    unique = drop_duplicates(candidates)
    chosen = sample_evenly(unique, options.max_dialogues)
    report = ConvertReport(
        messages_read=data.read,
        messages_kept=len(data.messages),
        skipped=dict(data.skipped),
        dropped_after_redaction=dropped,
        conversations=len(conversations),
        turns=turn_count,
        truncations=truncations,
        candidates=len(candidates),
        duplicates=len(candidates) - len(unique),
        dialogues_written=len(chosen),
        redactions=dict(redactions),
    )
    if not chosen:
        raise ChatExportError(NO_DIALOGUES)
    return Draft(dialogues=tuple(chosen), report=report)


def render_draft(dialogues: Sequence[Dialogue], tags: Sequence[str]) -> str:
    try:
        examples = [
            StyleExampleCreate(
                tags=list(tags), turns=[Turn(role=turn.role, text=turn.text) for turn in dialogue]
            )
            for dialogue in dialogues
        ]
    except ValidationError:
        raise ChatExportError(NO_DIALOGUES) from None
    document = {
        "version": FILE_FORMAT_VERSION,
        "dialogues": [example.model_dump(mode="json") for example in examples],
    }
    return json.dumps(document, ensure_ascii=False, indent=2) + "\n"


def resolve_output(out: Path, *, allow_any_path: bool, working_directory: Path) -> Path:
    target = (working_directory / out).resolve()
    allowed = (working_directory / OUTPUT_DIRECTORY).resolve()
    if not allow_any_path and not (target != allowed and target.is_relative_to(allowed)):
        raise ChatExportError(OUTPUT_OUTSIDE)
    return target


def check_target(target: Path, *, force: bool) -> None:
    if target.is_dir():
        raise ChatExportError(OUTPUT_IS_DIRECTORY.format(path=target))
    if target.exists() and not force:
        raise ChatExportError(OUTPUT_EXISTS.format(path=target))


def write_draft(target: Path, content: str, *, force: bool) -> None:
    check_target(target, force=force)
    temporary = target.with_name(f".{target.name}.{secrets.token_hex(4)}{PART_SUFFIX}")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, PRIVATE_FILE_MODE)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
        parse_examples(read_import_file(temporary))
        check_target(target, force=force)
        os.replace(temporary, target)
    except OSError as exc:
        raise ChatExportError(OUTPUT_FAILED.format(error=type(exc).__name__)) from None
    finally:
        temporary.unlink(missing_ok=True)


def convert_chat_export(
    source: Path,
    out: Path,
    options: ConvertOptions,
    *,
    force: bool = False,
    allow_any_path: bool = False,
    working_directory: Path | None = None,
) -> ConvertReport:
    validate_options(options)
    tags = normalize_tags(options.tags)
    target = resolve_output(
        out, allow_any_path=allow_any_path, working_directory=working_directory or Path.cwd()
    )
    check_target(target, force=force)
    data = read_messages(message_items(load_export_file(source)), by_id=options.me_id is not None)
    draft = build_draft(data, replace(options, tags=tags))
    write_draft(target, render_draft(draft.dialogues, tags), force=force)
    report = draft.report
    logger.info(
        "chat export converted",
        extra={
            "messages_read": report.messages_read,
            "messages_kept": report.messages_kept,
            "turns": report.turns,
            "candidates": report.candidates,
            "dialogues_written": report.dialogues_written,
            "truncations": report.truncations,
            "redactions": sum(report.redactions.values()),
        },
    )
    return report
