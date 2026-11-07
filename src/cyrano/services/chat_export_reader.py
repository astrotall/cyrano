from __future__ import annotations

import json
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from cyrano.domain.chat_export import HTML_SUFFIXES, MAX_EXPORT_BYTES, ExportMessage, SkipReason
from cyrano.domain.errors import ChatExportError
from cyrano.services.sanitize import strip_nul

MESSAGES_KEY = "messages"
CHATS_KEY = "chats"
MESSAGE_TYPE = "message"
FORWARDED_KEY = "forwarded_from"
TEXT_PART_KEY = "text"
HTML_START = "<"
INPUT_MISSING = "input file not found: {path}"
INPUT_IS_DIRECTORY = "input is a directory, not a file: {path}"
INPUT_UNREADABLE = "cannot read the input file: {error}"
INPUT_IS_HTML = (
    "HTML exports are not supported; export the chat again as machine-readable JSON "
    "(Telegram Desktop: export chat history, format: Machine-readable JSON)"
)
INPUT_TOO_LARGE = "input file is larger than {limit} bytes"
INPUT_NOT_UTF8 = "input file is not valid UTF-8"
INPUT_NOT_JSON = "input file is not valid JSON: {reason} at line {line} column {column}"
INPUT_TOO_DEEP = "input file is nested too deeply"
INPUT_ACCOUNT_WIDE = (
    "this is an account-wide export; export one chat (open the chat, then export chat history)"
)
INPUT_NO_MESSAGES = "the JSON has no messages; export one chat as machine-readable JSON"


class RawMessage(BaseModel):
    model_config = ConfigDict(
        frozen=True, extra="ignore", populate_by_name=True, hide_input_in_errors=True
    )

    type: JsonValue = None
    date: JsonValue = None
    date_unixtime: JsonValue = None
    sender: JsonValue = Field(default=None, alias="from")
    from_id: JsonValue = None
    text: JsonValue = None


@dataclass(frozen=True, slots=True)
class ExportData:
    messages: tuple[ExportMessage, ...]
    senders: frozenset[str]
    sender_ids: frozenset[str]
    read: int
    skipped: dict[SkipReason, int]


def load_export_file(path: Path) -> object:
    try:
        exists = path.exists()
        is_directory = path.is_dir()
    except OSError as exc:
        raise ChatExportError(INPUT_UNREADABLE.format(error=type(exc).__name__)) from None
    if not exists:
        raise ChatExportError(INPUT_MISSING.format(path=path))
    if is_directory:
        raise ChatExportError(INPUT_IS_DIRECTORY.format(path=path))
    if path.suffix.lower() in HTML_SUFFIXES:
        raise ChatExportError(INPUT_IS_HTML)
    try:
        if path.stat().st_size > MAX_EXPORT_BYTES:
            raise ChatExportError(INPUT_TOO_LARGE.format(limit=MAX_EXPORT_BYTES))
        with path.open("rb") as handle:
            data = handle.read(MAX_EXPORT_BYTES + 1)
    except OSError as exc:
        raise ChatExportError(INPUT_UNREADABLE.format(error=type(exc).__name__)) from None
    if len(data) > MAX_EXPORT_BYTES:
        raise ChatExportError(INPUT_TOO_LARGE.format(limit=MAX_EXPORT_BYTES))
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ChatExportError(INPUT_NOT_UTF8) from None
    if text.lstrip().startswith(HTML_START):
        raise ChatExportError(INPUT_IS_HTML)
    return parse_json(text)


def parse_json(text: str) -> object:
    try:
        parsed: object = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ChatExportError(
            INPUT_NOT_JSON.format(reason=exc.msg, line=exc.lineno, column=exc.colno)
        ) from None
    except RecursionError:
        raise ChatExportError(INPUT_TOO_DEEP) from None
    return parsed


def message_items(document: object) -> list[object]:
    if not isinstance(document, dict):
        raise ChatExportError(INPUT_NO_MESSAGES)
    messages = document.get(MESSAGES_KEY)
    if isinstance(messages, list):
        return messages
    if CHATS_KEY in document:
        raise ChatExportError(INPUT_ACCOUNT_WIDE)
    raise ChatExportError(INPUT_NO_MESSAGES)


def flatten_text(value: JsonValue) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_part_text(part) for part in value)
    return ""


def _part_text(part: JsonValue) -> str:
    if isinstance(part, str):
        return part
    if isinstance(part, dict):
        inner = part.get(TEXT_PART_KEY)
        return inner if isinstance(inner, str) else ""
    return ""


def sender_name(value: JsonValue) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None


def sender_identifier(value: JsonValue) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None


def message_time(unixtime: JsonValue, date: JsonValue) -> datetime | None:
    moment = _from_unixtime(unixtime)
    return moment if moment is not None else _from_date(date)


def _from_unixtime(value: JsonValue) -> datetime | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        seconds = value
    elif isinstance(value, str) and value.isascii() and value.isdigit():
        seconds = int(value)
    else:
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=UTC)
    except OverflowError, OSError, ValueError:
        return None


def _from_date(value: JsonValue) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _convert(raw: RawMessage, forwarded: bool, by_id: bool) -> ExportMessage | SkipReason:
    if raw.type != MESSAGE_TYPE:
        return SkipReason.NOT_A_MESSAGE
    if forwarded:
        return SkipReason.FORWARDED
    text = strip_nul(flatten_text(raw.text)).strip()
    if not text:
        return SkipReason.EMPTY_TEXT
    sender = sender_name(raw.sender)
    sender_id = sender_identifier(raw.from_id)
    if (sender_id if by_id else sender) is None:
        return SkipReason.NO_SENDER
    time = message_time(raw.date_unixtime, raw.date)
    if time is None:
        return SkipReason.NO_TIME
    return ExportMessage(time=time, sender=sender, text=text, sender_id=sender_id)


def read_messages(items: Sequence[object], *, by_id: bool = False) -> ExportData:
    skipped: Counter[SkipReason] = Counter()
    senders: set[str] = set()
    sender_ids: set[str] = set()
    kept: list[ExportMessage] = []
    for item in items:
        try:
            raw = RawMessage.model_validate(item)
        except ValidationError:
            skipped[SkipReason.UNREADABLE] += 1
            continue
        name = sender_name(raw.sender)
        if name is not None:
            senders.add(name)
        identifier = sender_identifier(raw.from_id)
        if identifier is not None:
            sender_ids.add(identifier)
        forwarded = FORWARDED_KEY in item if isinstance(item, dict) else False
        converted = _convert(raw, forwarded, by_id)
        if isinstance(converted, SkipReason):
            skipped[converted] += 1
        else:
            kept.append(converted)
    kept.sort(key=lambda message: message.time)
    return ExportData(
        messages=tuple(kept),
        senders=frozenset(senders),
        sender_ids=frozenset(sender_ids),
        read=len(items),
        skipped=dict(skipped),
    )
