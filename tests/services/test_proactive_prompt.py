from __future__ import annotations

import logging
from datetime import timedelta

import pytest

from cyrano.domain.enums import ReplyPurpose
from cyrano.domain.errors import ReplyInvalid
from cyrano.domain.models import Message
from cyrano.domain.reply import REPLY_SKIP_REASON, BuiltPrompt, ContactFacts
from cyrano.logging import JsonFormatter
from cyrano.prompts.texts import INSTRUCTIONS, NEEDS_OWNER_SIGNAL, NEXT_MESSAGE_REQUEST, SKIP_SIGNAL
from cyrano.services.prompt_builder import silence_hours
from cyrano.services.reply_postprocess import contains_skip, is_bare_skip
from tests.privacy import assert_absent
from tests.reply_data import (
    FIXED_NOW,
    INCOMING,
    OUTGOING,
    default_history,
    make_builder,
    make_message,
    system_of,
    user_of,
)
from tests.reply_db import MESSAGE_MARKER, PERSONA_MARKER, Repositories, ready_contact
from tests.services.fakes import ScriptedLLMClient

EXPECTED_PARAGRAPH = (
    "The contact has not written for 7 hours since the last message of the dialogue. "
    "Write ONE short natural message that continues the conversation or asks about something "
    "the contact mentioned earlier (use the memory), without sounding needy, without "
    "apologising for the silence, and without repeating a question that was already asked. "
    "If the conversation ended naturally (a goodbye, good night, the contact said they are busy) "
    "or you cannot continue without inventing facts about the owner, reply with exactly SKIP "
    "and nothing else."
)
EXPECTED_REQUEST = (
    "Write the owner's next message in this chat now, continuing it after the silence, "
    "or reply with exactly SKIP. Output only the message text."
)
LEAKED_TEXT = "skip-leak-marker-zq"
BARE_SKIPS = [
    "SKIP",
    "skip",
    "Skip",
    "  SKIP \n",
    "SKIP.",
    '"SKIP"',
    "«SKIP»",
    "`SKIP`",
    "**SKIP**",
]
EMBEDDED_SKIPS = [
    f"SKIP {LEAKED_TEXT}",
    f"{LEAKED_TEXT}: skip",
    f"ну ладно {LEAKED_TEXT}\nSKIP",
    "I would skip this one",
]
ORDINARY_TEXTS = ["skipping rope today", "Skippy says hi", "пропустим", "ski trip", "kips"]


def _silent_history(hours: float) -> list[Message]:
    first = make_message(1, INCOMING, "invented question about the weekend")
    last = make_message(2, OUTGOING, "invented answer about the bike")
    return [
        first.model_copy(update={"created_at": FIXED_NOW - timedelta(hours=hours + 1)}),
        last.model_copy(update={"created_at": FIXED_NOW - timedelta(hours=hours)}),
    ]


def _build(purpose: ReplyPurpose, messages: list[Message]) -> BuiltPrompt:
    return make_builder().build(
        contact_id=7,
        persona="Invented persona for the proactive prompt.",
        mode_fragment="Invented mode fragment.",
        facts=ContactFacts(name="Sasha"),
        summary="- Интересы: велосипед",
        messages=messages,
        examples=[],
        purpose=purpose,
    )


def test_the_skip_signal_is_a_named_constant() -> None:
    assert SKIP_SIGNAL == "SKIP"


def test_the_proactive_purpose_adds_one_paragraph_and_changes_only_the_request() -> None:
    history = _silent_history(7.5)
    reply = _build(ReplyPurpose.REPLY, history)
    proactive = _build(ReplyPurpose.PROACTIVE, history)

    assert system_of(proactive) == system_of(reply).replace(
        INSTRUCTIONS, f"{INSTRUCTIONS}\n\n{EXPECTED_PARAGRAPH}", 1
    )
    reply_lines = user_of(reply).splitlines()
    proactive_lines = user_of(proactive).splitlines()
    assert proactive_lines[:-1] == reply_lines[:-1]
    assert reply_lines[-1] == NEXT_MESSAGE_REQUEST
    assert proactive_lines[-1] == EXPECTED_REQUEST
    assert proactive.purpose is ReplyPurpose.PROACTIVE
    assert reply.purpose is ReplyPurpose.REPLY


def test_the_paragraph_sits_inside_the_instructions_layer_before_the_persona() -> None:
    system = system_of(_build(ReplyPurpose.PROACTIVE, _silent_history(7)))

    instructions_end = system.index("=== PERSONA ===")
    assert system.index(EXPECTED_PARAGRAPH) < instructions_end
    assert system.count(EXPECTED_PARAGRAPH) == 1
    assert NEEDS_OWNER_SIGNAL in system[:instructions_end]


def test_the_reply_purpose_never_mentions_the_skip_signal() -> None:
    prompt = _build(ReplyPurpose.REPLY, default_history())

    assert SKIP_SIGNAL not in system_of(prompt)
    assert SKIP_SIGNAL not in user_of(prompt)


@pytest.mark.parametrize(
    ("hours", "line"),
    [
        (1, "for 1 hour since"),
        (1.9, "for 1 hour since"),
        (6, "for 6 hours since"),
        (49, "for 49 hours"),
    ],
)
def test_the_silence_in_the_paragraph_is_computed_in_whole_hours(hours: float, line: str) -> None:
    assert line in system_of(_build(ReplyPurpose.PROACTIVE, _silent_history(hours)))


def test_the_silence_is_never_negative_and_empty_history_is_zero() -> None:
    later = make_message(1, INCOMING, "x").model_copy(
        update={"created_at": FIXED_NOW + timedelta(hours=2)}
    )

    assert silence_hours(FIXED_NOW, [later]) == 0
    assert silence_hours(FIXED_NOW, []) == 0


def test_the_proactive_prompt_keeps_the_summary_and_the_dialogue() -> None:
    prompt = _build(ReplyPurpose.PROACTIVE, _silent_history(7))

    assert "=== CONTACT SUMMARY ===" in system_of(prompt)
    assert "Me: invented answer about the bike" in user_of(prompt)


@pytest.mark.parametrize("raw", BARE_SKIPS)
def test_the_bare_skip_is_recognised_in_any_case_and_wrapping(raw: str) -> None:
    assert is_bare_skip(raw)
    assert contains_skip(raw)


@pytest.mark.parametrize("raw", EMBEDDED_SKIPS)
def test_a_skip_inside_a_longer_reply_is_a_signal_but_not_bare(raw: str) -> None:
    assert contains_skip(raw)
    assert not is_bare_skip(raw)


@pytest.mark.parametrize("raw", ORDINARY_TEXTS)
def test_an_ordinary_text_is_not_a_skip(raw: str) -> None:
    assert not contains_skip(raw)
    assert not is_bare_skip(raw)


@pytest.mark.parametrize("raw", BARE_SKIPS + EMBEDDED_SKIPS)
async def test_a_proactive_skip_gives_an_empty_skipped_reply(repos: Repositories, raw: str) -> None:
    contact = await ready_contact(repos)

    reply = await repos.generator(ScriptedLLMClient(raw)).generate(
        contact.id, purpose=ReplyPurpose.PROACTIVE
    )

    assert reply.skipped is True
    assert reply.needs_owner is False
    assert reply.text == ""
    assert (reply.usage.input_tokens, reply.usage.output_tokens) == (321, 17)


@pytest.mark.parametrize("raw", BARE_SKIPS + EMBEDDED_SKIPS)
async def test_a_skip_in_a_reply_is_invalid_output(repos: Repositories, raw: str) -> None:
    contact = await ready_contact(repos)

    with pytest.raises(ReplyInvalid) as raised:
        await repos.generator(ScriptedLLMClient(raw)).generate(contact.id)

    assert raised.value.reason == REPLY_SKIP_REASON


async def test_the_owner_signal_wins_over_a_skip(repos: Repositories) -> None:
    contact = await ready_contact(repos)

    reply = await repos.generator(ScriptedLLMClient("NEEDS_OWNER or SKIP")).generate(
        contact.id, purpose=ReplyPurpose.PROACTIVE
    )

    assert reply.needs_owner is True
    assert reply.skipped is False


async def test_a_normal_proactive_reply_is_cleaned_and_kept(repos: Repositories) -> None:
    contact = await ready_contact(repos)
    client = ScriptedLLMClient('"how did the invented ride go?"')

    reply = await repos.generator(client).generate(contact.id, purpose=ReplyPurpose.PROACTIVE)

    assert reply.skipped is False
    assert reply.text == "how did the invented ride go?"
    [(messages, _, _)] = client.calls
    assert messages[1].content.endswith(EXPECTED_REQUEST)


async def test_the_generator_logs_the_skip_flag_without_any_text(
    repos: Repositories, caplog: pytest.LogCaptureFixture
) -> None:
    contact = await ready_contact(repos)
    caplog.set_level(logging.DEBUG)

    await repos.generator(ScriptedLLMClient(f"SKIP {LEAKED_TEXT}")).generate(
        contact.id, purpose=ReplyPurpose.PROACTIVE
    )
    await repos.generator(ScriptedLLMClient("fine")).generate(
        contact.id, purpose=ReplyPurpose.PROACTIVE
    )

    lines = [r for r in caplog.records if r.name == "cyrano.services.reply_generator"]
    assert [r.__dict__["skipped"] for r in lines] == [True, False]
    output = "\n".join(JsonFormatter().format(record) for record in caplog.records)
    for marker in (LEAKED_TEXT, PERSONA_MARKER, MESSAGE_MARKER):
        assert_absent(marker, output)
