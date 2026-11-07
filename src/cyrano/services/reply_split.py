from __future__ import annotations

from cyrano.domain.reply import MAX_REPLY_PARTS, REPLY_PART_JOINER


def split_reply(text: str) -> list[str]:
    parts = [stripped for line in text.splitlines() if (stripped := line.strip())]
    if len(parts) <= MAX_REPLY_PARTS:
        return parts
    kept = parts[: MAX_REPLY_PARTS - 1]
    return [*kept, REPLY_PART_JOINER.join(parts[MAX_REPLY_PARTS - 1 :])]
