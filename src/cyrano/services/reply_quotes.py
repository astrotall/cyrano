from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta

from cyrano.domain.enums import MessageDirection
from cyrano.domain.models import Message
from cyrano.domain.reply import MAX_REPLY_PARTS, MessageRef, ReplyPart
from cyrano.prompts.texts import QUOTE_MARKER
from cyrano.services.reply_split import split_reply

MARKER_VARIATION_SELECTOR = "\N{VARIATION SELECTOR-16}"
MARKER_MAX_DIGITS = 3
MARKER_ARROW = f"{re.escape(QUOTE_MARKER)}{MARKER_VARIATION_SELECTOR}?"
LEADING_MARKER = re.compile(rf"^\s*{MARKER_ARROW}\s*(\d{{1,{MARKER_MAX_DIGITS}}})(?!\d)[:.\-]?\s*")
STRAY_MARKER = re.compile(rf"\s*{MARKER_ARROW}\s*\d+[:.\-]?\s*")
MARKER_SEPARATOR = " "
LINE_BREAK = "\n"


def quote_label(number: int) -> str:
    return f"{QUOTE_MARKER}{number}"


def strip_stray_markers(text: str) -> str:
    while STRAY_MARKER.search(text) is not None:
        text = STRAY_MARKER.sub(MARKER_SEPARATOR, text)
    return text.strip()


def _parse_line(line: str) -> tuple[str, int | None]:
    match = LEADING_MARKER.match(line)
    if match is None:
        return strip_stray_markers(line), None
    return strip_stray_markers(line[match.end() :]), int(match.group(1))


def _valid_target(number: int | None, refs: Mapping[int, MessageRef]) -> MessageRef | None:
    if number is None:
        return None
    ref = refs.get(number)
    if ref is None or ref.direction is not MessageDirection.INCOMING:
        return None
    return ref


def split_reply_with_targets(text: str, refs: Mapping[int, MessageRef]) -> list[ReplyPart]:
    lines: list[str] = []
    target: tuple[int, int, MessageRef] | None = None
    for raw in text.splitlines():
        line, number = _parse_line(raw)
        if not line:
            continue
        ref = _valid_target(number, refs) if target is None else None
        if ref is not None and number is not None:
            target = (min(len(lines), MAX_REPLY_PARTS - 1), number, ref)
        lines.append(line)
    parts: list[ReplyPart] = []
    for index, part in enumerate(split_reply(LINE_BREAK.join(lines))):
        cleaned = strip_stray_markers(part)
        if not cleaned:
            continue
        if target is not None and index == target[0]:
            parts.append(
                ReplyPart(
                    text=cleaned,
                    quote_number=target[1],
                    reply_to_tg_message_id=target[2].tg_message_id,
                )
            )
        else:
            parts.append(ReplyPart(text=cleaned))
    return parts


def with_late_target(
    parts: Sequence[ReplyPart],
    trigger: Message | None,
    contact_id: int,
    first_send_at: datetime,
    late_seconds: int,
) -> list[ReplyPart]:
    kept = list(parts)
    if late_seconds <= 0 or not kept:
        return kept
    if any(part.reply_to_tg_message_id is not None for part in kept):
        return kept
    if trigger is None or trigger.contact_id != contact_id:
        return kept
    if trigger.direction is not MessageDirection.INCOMING:
        return kept
    if first_send_at - trigger.created_at < timedelta(seconds=late_seconds):
        return kept
    first = ReplyPart(text=kept[0].text, reply_to_tg_message_id=trigger.tg_message_id)
    return [first, *kept[1:]]
