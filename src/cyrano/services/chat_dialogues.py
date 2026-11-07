from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import timedelta

from cyrano.domain.chat_export import TURN_SEPARATOR, ChatTurn, Dialogue, ExportMessage
from cyrano.domain.enums import TurnRole
from cyrano.domain.style import MAX_TURN_LENGTH, MAX_TURNS_PER_DIALOGUE


def split_conversations(
    messages: Sequence[ExportMessage], gap: timedelta
) -> list[list[ExportMessage]]:
    conversations: list[list[ExportMessage]] = []
    for message in messages:
        if conversations and message.time - conversations[-1][-1].time <= gap:
            conversations[-1].append(message)
        else:
            conversations.append([message])
    return conversations


def build_turns(
    conversation: Sequence[ExportMessage], is_owner: Callable[[ExportMessage], bool]
) -> list[ChatTurn]:
    turns: list[ChatTurn] = []
    for message in conversation:
        role = TurnRole.ME if is_owner(message) else TurnRole.THEM
        if turns and turns[-1].role is role:
            turns[-1] = ChatTurn(role, f"{turns[-1].text}{TURN_SEPARATOR}{message.text}")
        else:
            turns.append(ChatTurn(role, message.text))
    return turns


def truncate_at_word(text: str, limit: int = MAX_TURN_LENGTH) -> str:
    if len(text) <= limit:
        return text
    head = text[:limit]
    if not text[limit].isspace() and not head[-1].isspace():
        parts = head.rsplit(maxsplit=1)
        if len(parts) == 2:
            head = parts[0]
    return head.rstrip()


def truncate_turns(turns: Sequence[ChatTurn]) -> tuple[list[ChatTurn], int]:
    result: list[ChatTurn] = []
    truncated = 0
    for turn in turns:
        text = truncate_at_word(turn.text)
        if text != turn.text:
            truncated += 1
        result.append(ChatTurn(turn.role, text))
    return result, truncated


def dialogue_candidates(turns: Sequence[ChatTurn], window: int) -> list[Dialogue]:
    candidates: list[Dialogue] = []
    for index, turn in enumerate(turns):
        if turn.role is not TurnRole.ME:
            continue
        selected = turns[max(0, index - window + 1) : index + 1][-MAX_TURNS_PER_DIALOGUE:]
        first_them = next(
            (position for position, item in enumerate(selected) if item.role is TurnRole.THEM),
            None,
        )
        if first_them is not None:
            candidates.append(tuple(selected[first_them:]))
    return candidates


def drop_duplicates(dialogues: Sequence[Dialogue]) -> list[Dialogue]:
    return list(dict.fromkeys(dialogues))


def sample_evenly(dialogues: Sequence[Dialogue], limit: int) -> list[Dialogue]:
    total = len(dialogues)
    if total <= limit:
        return list(dialogues)
    return [dialogues[(2 * position + 1) * total // (2 * limit)] for position in range(limit)]
