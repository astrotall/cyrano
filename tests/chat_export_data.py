from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cyrano.domain.chat_export import ExportMessage

BASE_TIME = datetime(2024, 3, 1, 10, 0, tzinfo=UTC)
OWNER = "Zed Quark"
OTHER = "Olga Test"
CYRILLIC_OTHER = "Лиана Вербина"
OTHER_STEM = "Olga"


def at(minutes: float) -> datetime:
    return BASE_TIME + timedelta(minutes=minutes)


def message(
    minutes: float,
    sender: object,
    text: object,
    *,
    identifier: int = 1,
    **extra: object,
) -> dict[str, object]:
    moment = at(minutes)
    return {
        "id": identifier,
        "type": "message",
        "date": moment.replace(tzinfo=None).isoformat(),
        "date_unixtime": str(int(moment.timestamp())),
        "from": sender,
        "from_id": "user1",
        "text": text,
        "text_entities": [],
        **extra,
    }


def export_document(*messages: dict[str, object]) -> dict[str, object]:
    return {"name": "Invented chat", "type": "personal_chat", "id": 1, "messages": list(messages)}


def write_export(path: Path, *messages: dict[str, object]) -> Path:
    path.write_text(json.dumps(export_document(*messages), ensure_ascii=False), encoding="utf-8")
    return path


def export_message(minutes: float, sender: str, text: str) -> ExportMessage:
    return ExportMessage(time=at(minutes), sender=sender, text=text)


def chat(*lines: tuple[float, str, str]) -> list[dict[str, object]]:
    return [
        message(minutes, sender, text, identifier=index)
        for index, (minutes, sender, text) in enumerate(lines, start=1)
    ]


BACKTICK = "`"
OWNER_ID = "user1000000001"
OTHER_ID = "user1000000002"


def sample_shaped_message() -> dict[str, object]:
    return {
        "id": 1,
        "type": "message",
        "date": "2026-09-11T23:43:01",
        "date_unixtime": "1789159381",
        "from": BACKTICK,
        "from_id": OWNER_ID,
        "text": "hello",
        "text_entities": [{"type": "plain", "text": "hello"}],
    }
