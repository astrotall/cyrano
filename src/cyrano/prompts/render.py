"""Pure rendering of the system and user messages of a reply prompt."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from cyrano.domain.enums import MessageDirection, TurnRole
from cyrano.domain.models import Message
from cyrano.domain.reply import ContactFacts
from cyrano.domain.style import StyleExample, Turn
from cyrano.prompts.texts import (
    CONTACT_LABEL,
    CONTINUATION_INDENT,
    DATE_FORMAT,
    DIALOGUE_BEGIN,
    DIALOGUE_BOUNDARY,
    DIALOGUE_END,
    EXAMPLE_TITLE,
    EXAMPLES_HEADER,
    EXAMPLES_NOTE,
    FACT_ABOUT_LABEL,
    FACT_AGE_LABEL,
    FACT_BIO_LABEL,
    FACT_CITY_LABEL,
    FACT_NAME_LABEL,
    FACTS_HEADER,
    FACTS_NOTE,
    INSTRUCTIONS,
    INSTRUCTIONS_HEADER,
    LABEL_SEPARATOR,
    LAYER_HEADERS,
    LAYER_SEPARATOR,
    MODE_HEADER,
    NEXT_MESSAGE_REQUEST,
    NO_TEXT_PLACEHOLDER,
    OWNER_LABEL,
    PERSONA_HEADER,
    SUMMARY_HEADER,
    TIME_FORMAT,
    TIME_LINE,
    WEEKDAYS,
)

LINE_BREAK = "\n"


def remove_all(text: str, fragment: str) -> str:
    while fragment in text:
        text = text.replace(fragment, "")
    return text


def strip_boundary(text: str) -> str:
    return remove_all(text, DIALOGUE_BOUNDARY)


def strip_layer_headers(text: str) -> str:
    for header in LAYER_HEADERS:
        text = remove_all(text, header)
    return text


def one_line(text: str) -> str:
    return " ".join(strip_boundary(text).split())


def labelled(label: str, text: str) -> str:
    lines = text.strip().splitlines() or [NO_TEXT_PLACEHOLDER]
    head = f"{label}{LABEL_SEPARATOR}{lines[0]}"
    tail = [
        f"{CONTINUATION_INDENT}{line}".rstrip() if line else CONTINUATION_INDENT
        for line in lines[1:]
    ]
    return LINE_BREAK.join([head, *tail])


def _layer(header: str, *parts: str) -> str:
    return LINE_BREAK.join([header, *parts])


def _facts_lines(facts: ContactFacts) -> list[str]:
    pairs = (
        (FACT_NAME_LABEL, facts.name),
        (FACT_AGE_LABEL, None if facts.age is None else str(facts.age)),
        (FACT_CITY_LABEL, facts.city),
        (FACT_ABOUT_LABEL, facts.about),
        (FACT_BIO_LABEL, facts.bio),
    )
    return [
        f"- {label}{LABEL_SEPARATOR}{one_line(value)}"
        for label, value in pairs
        if value is not None and one_line(value)
    ]


def _turn_label(turn: Turn) -> str:
    return OWNER_LABEL if turn.role is TurnRole.ME else CONTACT_LABEL


def _example_block(number: int, example: StyleExample) -> str:
    lines = [labelled(_turn_label(turn), turn.text) for turn in example.turns]
    return LINE_BREAK.join([EXAMPLE_TITLE.format(number=number), *lines])


def render_system_prompt(
    *,
    persona: str,
    mode_fragment: str,
    facts: ContactFacts,
    summary: str | None,
    examples: Sequence[StyleExample],
) -> str:
    layers = [_layer(INSTRUCTIONS_HEADER, INSTRUCTIONS), _layer(PERSONA_HEADER, persona)]
    if mode_fragment.strip():
        layers.append(_layer(MODE_HEADER, mode_fragment.strip()))
    fact_lines = _facts_lines(facts)
    if fact_lines:
        layers.append(_layer(FACTS_HEADER, FACTS_NOTE, *fact_lines))
    cleaned_summary = (
        None if summary is None else strip_boundary(strip_layer_headers(summary)).strip()
    )
    if cleaned_summary:
        layers.append(_layer(SUMMARY_HEADER, cleaned_summary))
    if examples:
        blocks = [_example_block(number, example) for number, example in enumerate(examples, 1)]
        layers.append(_layer(EXAMPLES_HEADER, EXAMPLES_NOTE, LAYER_SEPARATOR.join(blocks)))
    return LAYER_SEPARATOR.join(layers)


def render_time_line(now: datetime, timezone_name: str) -> str:
    return TIME_LINE.format(
        weekday=WEEKDAYS[now.weekday()],
        date=now.strftime(DATE_FORMAT),
        time=now.strftime(TIME_FORMAT),
        timezone=timezone_name,
    )


def _dialogue_line(message: Message) -> str:
    label = CONTACT_LABEL if message.direction is MessageDirection.INCOMING else OWNER_LABEL
    return labelled(label, strip_boundary(message.text))


def render_user_prompt(*, now: datetime, timezone_name: str, messages: Sequence[Message]) -> str:
    dialogue = [_dialogue_line(message) for message in messages]
    block = LINE_BREAK.join([DIALOGUE_BEGIN, *dialogue, DIALOGUE_END])
    return LAYER_SEPARATOR.join([render_time_line(now, timezone_name), block, NEXT_MESSAGE_REQUEST])
