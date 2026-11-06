from __future__ import annotations

import logging

import pytest

from cyrano.domain.llm import LLMUsage
from cyrano.domain.reply import GeneratedReply
from cyrano.logging import JsonFormatter
from cyrano.prompts.texts import DIALOGUE_BOUNDARY, INSTRUCTIONS_HEADER, NEEDS_OWNER_SIGNAL
from cyrano.services.reply_postprocess import contains_needs_owner, is_bare_needs_owner
from tests.privacy import assert_absent
from tests.reply_db import MESSAGE_MARKER, PERSONA_MARKER, Repositories, ready_contact
from tests.services.fakes import ScriptedLLMClient

LEAKED_TEXT = "leaked-reply-marker-zq"
BARE_SIGNALS = [
    "NEEDS_OWNER",
    "needs_owner",
    "Needs_Owner",
    "  NEEDS_OWNER  \n",
    '"NEEDS_OWNER"',
    "«NEEDS_OWNER»",
    "“NEEDS_OWNER”",
    "'NEEDS_OWNER'",
    "`NEEDS_OWNER`",
    "```\nNEEDS_OWNER\n```",
    "NEEDS_OWNER.",
    "NEEDS_OWNER!",
    "**NEEDS_OWNER**",
    "(NEEDS_OWNER)",
]
EMBEDDED_SIGNALS = [
    f"{LEAKED_TEXT} NEEDS_OWNER",
    f"NEEDS_OWNER {LEAKED_TEXT}",
    f"ну привет {LEAKED_TEXT}, needs_owner кстати",
    "NEEDS OWNER",
    "needs-owner",
    f"{LEAKED_TEXT}\n\nNEEDS_OWNER\n{DIALOGUE_BOUNDARY}",
    f"{INSTRUCTIONS_HEADER} NEEDS_OWNER",
]
ORDINARY_REPLIES = ["привет, как ты", "owner of a cat", "needs more coffee", "NEEDS", "OWNER"]


def test_the_signal_is_a_named_constant() -> None:
    assert NEEDS_OWNER_SIGNAL == "NEEDS_OWNER"


@pytest.mark.parametrize("raw", BARE_SIGNALS)
def test_the_bare_signal_is_recognised_in_any_case_and_wrapping(raw: str) -> None:
    assert is_bare_needs_owner(raw)
    assert contains_needs_owner(raw)


@pytest.mark.parametrize("raw", EMBEDDED_SIGNALS)
def test_the_signal_inside_a_longer_reply_is_recognised_but_is_not_bare(raw: str) -> None:
    assert contains_needs_owner(raw)
    assert not is_bare_needs_owner(raw)


@pytest.mark.parametrize("raw", ORDINARY_REPLIES)
def test_an_ordinary_reply_is_not_a_signal(raw: str) -> None:
    assert not contains_needs_owner(raw)
    assert not is_bare_needs_owner(raw)


def test_a_generated_reply_has_no_owner_signal_by_default() -> None:
    reply = GeneratedReply(
        text="fine",
        usage=LLMUsage(input_tokens=1, output_tokens=1, cache_hit_tokens=None),
        examples_offered=0,
        examples_kept=0,
        messages_offered=0,
        messages_kept=0,
        prompt_characters=0,
    )

    assert reply.needs_owner is False


@pytest.mark.parametrize("raw", BARE_SIGNALS + EMBEDDED_SIGNALS)
async def test_the_generator_returns_the_signal_with_an_empty_text_and_the_usage(
    repos: Repositories, raw: str
) -> None:
    contact = await ready_contact(repos)

    reply = await repos.generator(ScriptedLLMClient(raw)).generate(contact.id)

    assert reply.needs_owner is True
    assert reply.text == ""
    assert (reply.usage.input_tokens, reply.usage.output_tokens) == (321, 17)
    assert (reply.messages_offered, reply.messages_kept) == (3, 3)
    assert reply.prompt_characters > 0


async def test_a_normal_reply_is_untouched_by_the_signal_check(repos: Repositories) -> None:
    contact = await ready_contact(repos)

    reply = await repos.generator(ScriptedLLMClient('"see you, bring tea"')).generate(contact.id)

    assert reply.needs_owner is False
    assert reply.text == "see you, bring tea"


async def test_the_generator_logs_the_signal_and_the_leak_without_the_reply(
    repos: Repositories, caplog: pytest.LogCaptureFixture
) -> None:
    contact = await ready_contact(repos)
    caplog.set_level(logging.DEBUG)

    await repos.generator(ScriptedLLMClient("NEEDS_OWNER")).generate(contact.id)
    await repos.generator(ScriptedLLMClient(f"{LEAKED_TEXT} NEEDS_OWNER")).generate(contact.id)
    await repos.generator(ScriptedLLMClient("fine")).generate(contact.id)

    lines = [r for r in caplog.records if r.name == "cyrano.services.reply_generator"]
    assert [(r.__dict__["needs_owner"], r.__dict__["needs_owner_leaked"]) for r in lines] == [
        (True, False),
        (True, True),
        (False, False),
    ]
    output = "\n".join(JsonFormatter().format(record) for record in caplog.records)
    for marker in (LEAKED_TEXT, PERSONA_MARKER, MESSAGE_MARKER):
        assert_absent(marker, output)
