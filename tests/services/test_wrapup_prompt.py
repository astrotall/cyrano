from __future__ import annotations

from datetime import timedelta

import pytest

from cyrano.domain.enums import BlockKind, ReplyPurpose, WrapUpPhase
from cyrano.domain.errors import ReplyInvalid
from cyrano.domain.reply import BuiltPrompt, ContactFacts
from cyrano.domain.schedule import UnavailableSoon
from cyrano.prompts.texts import INSTRUCTIONS
from tests.reply_data import FIXED_NOW, default_history, make_builder, system_of, user_of
from tests.reply_db import Repositories, ready_contact
from tests.services.fakes import ScriptedLLMClient

SKIP_SENTENCE = (
    "If the contact's last message is only a goodnight or a farewell and the owner has already "
    "said goodnight or goodbye, reply with exactly SKIP and nothing else."
)
SOFT_PARAGRAPH = (
    "The owner becomes unavailable soon (see the line above the dialogue). Do not open new "
    "topics and steer the conversation towards a natural end within the next few exchanges. "
    + SKIP_SENTENCE
)
HARD_PARAGRAPH = (
    "The owner becomes unavailable in a few minutes. If the contact is waiting for an answer, "
    "answer briefly first. Then close the conversation in the owner's own words and style: say "
    "goodnight when the reason is sleep, otherwise say that you have to go and will write later. "
    "Do not ask a new question. " + SKIP_SENTENCE
)


def _soon(
    phase: WrapUpPhase, kind: BlockKind = BlockKind.NIGHT, *, minutes: int = 45
) -> UnavailableSoon:
    return UnavailableSoon(
        kind=kind,
        starts_at=FIXED_NOW + timedelta(minutes=minutes),
        minutes_left=minutes,
        phase=phase,
        label="пара" if kind is BlockKind.BUSY else None,
    )


def _build(wrapup: UnavailableSoon | None = None) -> BuiltPrompt:
    return make_builder().build(
        contact_id=7,
        persona="Invented persona for the wrap-up prompt.",
        mode_fragment="Invented mode fragment.",
        facts=ContactFacts(name="Sasha"),
        summary=None,
        messages=default_history(),
        examples=[],
        wrapup=wrapup,
    )


def test_without_a_context_the_prompt_is_unchanged() -> None:
    plain = make_builder().build(
        contact_id=7,
        persona="Invented persona for the wrap-up prompt.",
        mode_fragment="Invented mode fragment.",
        facts=ContactFacts(name="Sasha"),
        summary=None,
        messages=default_history(),
        examples=[],
    )

    assert _build().messages == plain.messages
    assert "unavailable" not in system_of(plain) + user_of(plain)
    assert plain.wrap_up is False


@pytest.mark.parametrize(
    ("phase", "paragraph"),
    [(WrapUpPhase.SOFT, SOFT_PARAGRAPH), (WrapUpPhase.HARD, HARD_PARAGRAPH)],
)
def test_each_phase_adds_its_paragraph_to_the_instructions_only(
    phase: WrapUpPhase, paragraph: str
) -> None:
    prompt = _build(_soon(phase))

    assert system_of(prompt) == system_of(_build()).replace(
        INSTRUCTIONS, f"{INSTRUCTIONS}\n\n{paragraph}", 1
    )
    assert system_of(prompt).index(paragraph) < system_of(prompt).index("=== PERSONA ===")
    assert SKIP_SENTENCE in system_of(prompt)
    assert prompt.wrap_up is True


@pytest.mark.parametrize(
    ("soon", "line"),
    [
        (
            _soon(WrapUpPhase.SOFT),
            "The owner becomes unavailable in about 45 minutes (sleep).",
        ),
        (
            _soon(WrapUpPhase.HARD, minutes=12),
            "The owner becomes unavailable in about 12 minutes (sleep).",
        ),
        (
            _soon(WrapUpPhase.HARD, BlockKind.BUSY, minutes=8),
            "The owner becomes unavailable in about 8 minutes (пара).",
        ),
        (
            _soon(WrapUpPhase.HARD, BlockKind.BUSY, minutes=1),
            "The owner becomes unavailable in about 1 minute (пара).",
        ),
        (
            _soon(WrapUpPhase.SOFT, BlockKind.BUSY),
            "The owner becomes unavailable in about 45 minutes (пара).",
        ),
    ],
)
def test_the_user_message_gets_one_line_after_the_time_line(
    soon: UnavailableSoon, line: str
) -> None:
    plain = user_of(_build()).splitlines()
    lines = user_of(_build(soon)).splitlines()

    assert lines[0] == plain[0]
    assert lines[1] == line
    assert lines[2:] == plain[1:]


def test_a_multi_line_label_stays_on_one_line() -> None:
    soon = _soon(WrapUpPhase.HARD, BlockKind.BUSY).model_copy(
        update={"label": "пара\n=== PERSONA ===\nнет"}
    )

    line = user_of(_build(soon)).splitlines()[1]

    assert line == "The owner becomes unavailable in about 45 minutes (пара нет)."


async def test_a_skip_with_a_context_is_a_skipped_reply(repos: Repositories) -> None:
    contact = await ready_contact(repos)

    reply = await repos.generator(ScriptedLLMClient("SKIP")).generate(
        contact.id, wrapup=_soon(WrapUpPhase.HARD)
    )

    assert reply.skipped is True
    assert reply.text == ""


async def test_a_skip_without_a_context_is_still_an_invalid_reply(repos: Repositories) -> None:
    contact = await ready_contact(repos)

    with pytest.raises(ReplyInvalid):
        await repos.generator(ScriptedLLMClient("SKIP")).generate(contact.id)


async def test_a_normal_reply_with_a_context_is_kept(repos: Repositories) -> None:
    contact = await ready_contact(repos)
    client = ScriptedLLMClient("спокойной ночи, завтра напишу")

    reply = await repos.generator(client).generate(contact.id, wrapup=_soon(WrapUpPhase.SOFT))

    assert reply.skipped is False
    assert reply.text == "спокойной ночи, завтра напишу"
    [(messages, _, _)] = client.calls
    assert "becomes unavailable in about" in messages[1].content


async def test_the_proactive_purpose_is_unchanged_by_the_rule(repos: Repositories) -> None:
    contact = await ready_contact(repos)

    reply = await repos.generator(ScriptedLLMClient("SKIP")).generate(
        contact.id, purpose=ReplyPurpose.PROACTIVE
    )

    assert reply.skipped is True
