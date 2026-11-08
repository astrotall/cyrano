from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime

from cyrano.domain.enums import MessageDirection, TurnRole
from cyrano.domain.gender import UNKNOWN_GENDER_LABEL
from cyrano.domain.models import Message
from cyrano.domain.reply import ContactFacts
from cyrano.domain.style import StyleExample, Turn
from cyrano.domain.summary import ContactSummary
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
    FACT_GENDER_LABEL,
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
    SUMMARY_AGE_LABEL,
    SUMMARY_AGREEMENTS_LABEL,
    SUMMARY_BOUNDARY,
    SUMMARY_CITY_LABEL,
    SUMMARY_HEADER,
    SUMMARY_INSTRUCTIONS,
    SUMMARY_INTERESTS_LABEL,
    SUMMARY_ITEM_SEPARATOR,
    SUMMARY_JSON_ITEM_SEPARATOR,
    SUMMARY_JSON_KEY_SEPARATOR,
    SUMMARY_LIST_MARK,
    SUMMARY_MEMORY_NOTE,
    SUMMARY_MESSAGES_BEGIN,
    SUMMARY_MESSAGES_END,
    SUMMARY_MOOD_LABEL,
    SUMMARY_NAME_LABEL,
    SUMMARY_NOTES_LABEL,
    SUMMARY_OCCUPATION_LABEL,
    SUMMARY_OPEN_QUESTIONS_LABEL,
    SUMMARY_OWNER_LABEL,
    SUMMARY_PREVIOUS_LABEL,
    SUMMARY_REQUEST,
    TIME_FORMAT,
    TIME_LINE,
    TOLD_ABOUT_OWNER_NOTE,
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
        (FACT_GENDER_LABEL, UNKNOWN_GENDER_LABEL if facts.gender is None else facts.gender.value),
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
    layers.append(_layer(FACTS_HEADER, FACTS_NOTE, *_facts_lines(facts)))
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


def _summary_lines(summary: ContactSummary) -> list[str]:
    pairs = (
        (SUMMARY_NAME_LABEL, summary.name),
        (SUMMARY_AGE_LABEL, None if summary.age is None else str(summary.age)),
        (SUMMARY_CITY_LABEL, summary.city),
        (SUMMARY_OCCUPATION_LABEL, summary.occupation),
        (SUMMARY_INTERESTS_LABEL, SUMMARY_ITEM_SEPARATOR.join(summary.interests)),
        (SUMMARY_MOOD_LABEL, summary.mood_and_tone),
        (SUMMARY_AGREEMENTS_LABEL, SUMMARY_ITEM_SEPARATOR.join(summary.agreements_and_plans)),
        (SUMMARY_OPEN_QUESTIONS_LABEL, SUMMARY_ITEM_SEPARATOR.join(summary.open_questions)),
        (SUMMARY_NOTES_LABEL, SUMMARY_ITEM_SEPARATOR.join(summary.notes)),
    )
    return [
        f"{SUMMARY_LIST_MARK}{label}{LABEL_SEPARATOR}{one_line(value)}"
        for label, value in pairs
        if value is not None and one_line(value)
    ]


def render_summary_layer(summary: ContactSummary) -> str | None:
    lines = _summary_lines(summary)
    told = [one_line(item) for item in summary.told_about_owner if one_line(item)]
    if told:
        lines.extend([TOLD_ABOUT_OWNER_NOTE, *(f"{SUMMARY_LIST_MARK}{item}" for item in told)])
    if not lines:
        return None
    return LINE_BREAK.join([SUMMARY_MEMORY_NOTE, *lines])


def strip_summary_boundary(text: str) -> str:
    return remove_all(text, SUMMARY_BOUNDARY)


def render_previous_memory(previous: ContactSummary | None) -> str:
    data = {} if previous is None else previous.model_dump(mode="json", exclude_defaults=True)
    text = json.dumps(
        data,
        ensure_ascii=False,
        separators=(SUMMARY_JSON_ITEM_SEPARATOR, SUMMARY_JSON_KEY_SEPARATOR),
    )
    return strip_summary_boundary(text)


def _summary_dialogue_line(message: Message) -> str:
    label = CONTACT_LABEL if message.direction is MessageDirection.INCOMING else SUMMARY_OWNER_LABEL
    return labelled(label, strip_summary_boundary(message.text))


def render_summary_system_prompt() -> str:
    return SUMMARY_INSTRUCTIONS


def render_summary_user_prompt(
    *, previous: ContactSummary | None, messages: Sequence[Message]
) -> str:
    lines = [_summary_dialogue_line(message) for message in messages]
    block = LINE_BREAK.join([SUMMARY_MESSAGES_BEGIN, *lines, SUMMARY_MESSAGES_END])
    memory = LINE_BREAK.join([SUMMARY_PREVIOUS_LABEL, render_previous_memory(previous)])
    return LAYER_SEPARATOR.join([memory, block, SUMMARY_REQUEST])
