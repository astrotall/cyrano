from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

from cyrano.domain.chat_export import (
    DEFAULT_GAP_HOURS,
    DEFAULT_MAX_DIALOGUES,
    DEFAULT_WINDOW,
    TAG_SEPARATOR,
    ConvertOptions,
    ConvertReport,
    RedactionKind,
    SkipReason,
)
from cyrano.domain.errors import DomainError
from cyrano.services.chat_export_converter import convert_chat_export
from cyrano.style_cli import ACTION_DEST, EXAMPLES_COMMAND, EXIT_ERROR, EXIT_OK

CONVERT_ACTION = "convert"
DEFAULT_OUT = Path("data/style-examples.json")
REVIEW_REMINDER = (
    "review the draft before importing: it can still hold names, intimate details and other "
    "personal data, and the examples, the other person's lines included, are sent to the LLM "
    "provider inside prompts"
)


@dataclass(frozen=True)
class ConvertRequest:
    source: Path
    me: str | None = None
    me_id: str | None = None
    out: Path = DEFAULT_OUT
    tags: str = ""
    max_dialogues: int = DEFAULT_MAX_DIALOGUES
    gap_hours: float = DEFAULT_GAP_HOURS
    window: int = DEFAULT_WINDOW
    redact: list[str] = field(default_factory=list)
    force: bool = False
    allow_any_path: bool = False


def add_convert_parser(actions: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    convert = actions.add_parser(
        CONVERT_ACTION,
        help="convert a Telegram Desktop JSON chat export into a draft examples file",
    )
    convert.add_argument("input", type=Path, help="result.json of one chat")
    convert.add_argument("--me", help="your sender name in the export, exactly")
    convert.add_argument(
        "--me-id",
        dest="me_id",
        help="your from_id in the export, exactly",
    )
    convert.add_argument("--out", type=Path, default=DEFAULT_OUT)
    convert.add_argument("--tags", default="", help="comma-separated tags for every dialogue")
    convert.add_argument("--max", type=int, default=DEFAULT_MAX_DIALOGUES, dest="max_dialogues")
    convert.add_argument("--gap-hours", type=float, default=DEFAULT_GAP_HOURS)
    convert.add_argument("--window", type=int, default=DEFAULT_WINDOW)
    convert.add_argument("--redact", nargs="+", action="extend", default=[], metavar="WORD")
    convert.add_argument("--force", action="store_true")
    convert.add_argument("--allow-any-path", action="store_true")


def convert_request(args: argparse.Namespace) -> ConvertRequest | None:
    if args.command != EXAMPLES_COMMAND or getattr(args, ACTION_DEST, None) != CONVERT_ACTION:
        return None
    return ConvertRequest(
        source=args.input,
        me=args.me,
        me_id=args.me_id,
        out=args.out,
        tags=args.tags,
        max_dialogues=args.max_dialogues,
        gap_hours=args.gap_hours,
        window=args.window,
        redact=list(args.redact),
        force=args.force,
        allow_any_path=args.allow_any_path,
    )


def _tag_list(raw: str) -> tuple[str, ...]:
    return tuple(part for part in raw.split(TAG_SEPARATOR) if part.strip())


def print_report(report: ConvertReport, target: Path) -> None:
    print(f"messages read: {report.messages_read}")
    print(f"messages kept: {report.messages_kept}")
    for reason in SkipReason:
        print(f"skipped, {reason.value}: {report.skipped.get(reason, 0)}")
    print(f"dropped after redaction: {report.dropped_after_redaction}")
    print(f"conversations: {report.conversations}")
    print(f"turns built: {report.turns}")
    print(f"turns truncated: {report.truncations}")
    print(f"candidates: {report.candidates} (duplicates dropped: {report.duplicates})")
    print(f"dialogues written: {report.dialogues_written}")
    for kind in RedactionKind:
        print(f"redacted, {kind.value}: {report.redactions.get(kind, 0)}")
    print(f"output: {target}")
    print(REVIEW_REMINDER)


def run_convert(request: ConvertRequest, *, working_directory: Path | None = None) -> int:
    options = ConvertOptions(
        me=request.me,
        me_id=request.me_id,
        tags=_tag_list(request.tags),
        max_dialogues=request.max_dialogues,
        gap_hours=request.gap_hours,
        window=request.window,
        redact_words=tuple(request.redact),
    )
    try:
        report = convert_chat_export(
            request.source,
            request.out,
            options,
            force=request.force,
            allow_any_path=request.allow_any_path,
            working_directory=working_directory,
        )
    except DomainError as exc:
        print(exc, file=sys.stderr)
        return EXIT_ERROR
    print_report(report, request.out)
    return EXIT_OK
