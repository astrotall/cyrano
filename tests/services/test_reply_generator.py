from __future__ import annotations

import io
import logging

import pytest
from sqlalchemy.engine import URL

from cyrano.domain.enums import MessageDirection, Role
from cyrano.domain.errors import (
    ContactNotFound,
    DefaultModeUnavailable,
    LLMUnavailableError,
    ModeNameNotFound,
    NoMessagesToReplyTo,
    PersonaMissing,
    PromptTooLarge,
    ReplyInvalid,
)
from cyrano.domain.llm import LLMResult, LLMUsage
from cyrano.domain.models import ModeCreate
from cyrano.domain.style import StyleExampleCreate
from cyrano.logging import JsonFormatter
from cyrano.prompts.texts import (
    DIALOGUE_BEGIN,
    DIALOGUE_BOUNDARY,
    EXAMPLES_HEADER,
    INSTRUCTIONS_HEADER,
    MODE_HEADER,
)
from tests.db.conftest import execute
from tests.privacy import FAKE_TELEGRAM_ID, assert_absent
from tests.reply_db import (
    BIO_MARKER,
    EXAMPLE_MARKER,
    MESSAGE_MARKER,
    PERSONA_MARKER,
    REPLY_MARKER,
    USERNAME_MARKER,
    Repositories,
    ready_contact,
    row_counts,
)
from tests.services.fakes import FakeLLMClient, ScriptedLLMClient


async def test_a_reply_is_generated_with_usage_and_counters(repos: Repositories) -> None:
    contact = await ready_contact(repos)
    await repos.style_examples.add_many(
        [
            StyleExampleCreate.model_validate(
                {
                    "turns": [
                        {"role": "them", "text": f"{EXAMPLE_MARKER} question"},
                        {"role": "me", "text": f"{EXAMPLE_MARKER} answer"},
                    ]
                }
            )
        ]
    )
    client = ScriptedLLMClient(f'"{REPLY_MARKER}"')

    reply = await repos.generator(client).generate(contact.id)

    assert reply.text == REPLY_MARKER
    assert (reply.usage.input_tokens, reply.usage.output_tokens) == (321, 17)
    assert (reply.examples_offered, reply.examples_kept) == (1, 1)
    assert (reply.messages_offered, reply.messages_kept) == (3, 3)
    sent = client.calls[0][0]
    assert reply.prompt_characters == sum(len(message.content) for message in sent)


async def test_the_client_gets_exactly_one_system_and_one_user_message(
    repos: Repositories,
) -> None:
    contact = await ready_contact(repos)
    client = ScriptedLLMClient("fine")

    await repos.generator(client).generate(contact.id)

    sent, temperature, max_tokens = client.calls[0]
    assert [message.role for message in sent] == [Role.SYSTEM, Role.USER]
    assert (temperature, max_tokens) == (None, None)
    system, user = sent[0].content, sent[1].content
    assert system.startswith(INSTRUCTIONS_HEADER)
    assert PERSONA_MARKER in system
    assert f"- Bio: {BIO_MARKER}" in system
    assert DIALOGUE_BEGIN in user
    assert f"Contact: {MESSAGE_MARKER} 1" in user
    assert f"Me: {MESSAGE_MARKER} 2" in user


async def test_the_username_and_the_telegram_id_never_reach_the_model(
    repos: Repositories,
) -> None:
    contact = await ready_contact(repos)
    client = ScriptedLLMClient("fine")

    await repos.generator(client).generate(contact.id)

    sent = "\n".join(message.content for message in client.calls[0][0])
    assert_absent(USERNAME_MARKER, sent)
    assert_absent(FAKE_TELEGRAM_ID, sent)


async def test_only_the_last_n_messages_are_loaded(repos: Repositories) -> None:
    await repos.personas.replace(PERSONA_MARKER)
    contact = await repos.contact()
    await repos.conversation(contact.id, count=8)
    client = ScriptedLLMClient("fine")

    reply = await repos.generator(client, history_limit=5).generate(contact.id)

    user = client.calls[0][0][1].content
    assert (reply.messages_offered, reply.messages_kept) == (5, 5)
    assert f"{MESSAGE_MARKER} 3\n" not in user
    assert f"{MESSAGE_MARKER} 4\n" in user
    assert f"{MESSAGE_MARKER} 8\n" in user


async def test_trimming_counters_reach_the_reply(repos: Repositories) -> None:
    contact = await ready_contact(repos)
    for number in range(1, 6):
        await repos.style_examples.add_many(
            [
                StyleExampleCreate.model_validate(
                    {
                        "turns": [
                            {"role": "them", "text": f"{EXAMPLE_MARKER} q{number} " * 20},
                            {"role": "me", "text": f"{EXAMPLE_MARKER} a{number} " * 20},
                        ]
                    }
                )
            ]
        )
    full = await repos.generator(ScriptedLLMClient("fine")).generate(contact.id)
    client = ScriptedLLMClient("fine")

    reply = await repos.generator(client, budget=full.prompt_characters - 1).generate(contact.id)

    assert (reply.examples_offered, reply.examples_kept) == (5, 4)
    assert reply.prompt_characters < full.prompt_characters


async def test_the_mode_of_the_contact_is_used(repos: Repositories) -> None:
    await repos.modes.create(ModeCreate(name="playful", prompt_fragment="playful-fragment-zq"))
    contact = await ready_contact(repos, mode="playful")
    client = ScriptedLLMClient("fine")

    await repos.generator(client).generate(contact.id)

    system = client.calls[0][0][0].content
    assert f"{MODE_HEADER}\nplayful-fragment-zq" in system


async def test_a_contact_without_a_mode_gets_the_default_mode(repos: Repositories) -> None:
    contact = await ready_contact(repos)
    default = await repos.modes.get_by_name("default")
    assert default is not None
    client = ScriptedLLMClient("fine")

    await repos.generator(client).generate(contact.id)

    assert f"{MODE_HEADER}\n{default.prompt_fragment}" in client.calls[0][0][0].content


async def test_an_explicit_mode_name_overrides_the_contact_mode(repos: Repositories) -> None:
    await repos.modes.create(ModeCreate(name="playful", prompt_fragment="playful-fragment-zq"))
    await repos.modes.create(ModeCreate(name="serious", prompt_fragment="serious-fragment-zq"))
    contact = await ready_contact(repos, mode="playful")
    client = ScriptedLLMClient("fine")

    await repos.generator(client).generate(contact.id, "serious")

    system = client.calls[0][0][0].content
    assert "serious-fragment-zq" in system
    assert "playful-fragment-zq" not in system


async def test_examples_follow_the_mode_name(repos: Repositories) -> None:
    await repos.modes.create(ModeCreate(name="playful", prompt_fragment="playful-fragment-zq"))
    contact = await ready_contact(repos, mode="playful")
    await repos.style_examples.add_many(
        [
            StyleExampleCreate.model_validate(
                {
                    "tags": ["playful"],
                    "turns": [{"role": "them", "text": "q"}, {"role": "me", "text": "tagged-zq"}],
                }
            ),
            StyleExampleCreate.model_validate(
                {
                    "tags": ["serious"],
                    "turns": [{"role": "them", "text": "q"}, {"role": "me", "text": "other-zq"}],
                }
            ),
        ]
    )
    client = ScriptedLLMClient("fine")

    reply = await repos.generator(client).generate(contact.id)

    system = client.calls[0][0][0].content
    assert EXAMPLES_HEADER in system
    assert "tagged-zq" in system
    assert "other-zq" not in system
    assert reply.examples_offered == 1


async def test_a_disabled_mode_falls_back_to_the_default(
    repos: Repositories, migrated_url: URL
) -> None:
    await repos.modes.create(ModeCreate(name="playful", prompt_fragment="playful-fragment-zq"))
    contact = await ready_contact(repos, mode="playful")
    await execute(migrated_url, "UPDATE modes SET enabled = false WHERE name = 'playful'")
    default = await repos.modes.get_by_name("default")
    assert default is not None
    client = ScriptedLLMClient("fine")

    await repos.generator(client).generate(contact.id)

    system = client.calls[0][0][0].content
    assert "playful-fragment-zq" not in system
    assert default.prompt_fragment in system


async def test_an_unknown_mode_name_is_an_error_not_a_silent_default(
    repos: Repositories,
) -> None:
    contact = await ready_contact(repos)
    client = ScriptedLLMClient("fine")

    with pytest.raises(ModeNameNotFound):
        await repos.generator(client).generate(contact.id, "no-such-mode")

    assert client.calls == []


async def test_a_missing_default_mode_propagates(repos: Repositories, migrated_url: URL) -> None:
    contact = await ready_contact(repos)
    await execute(migrated_url, "UPDATE modes SET enabled = false WHERE name = 'default'")
    client = ScriptedLLMClient("fine")

    with pytest.raises(DefaultModeUnavailable):
        await repos.generator(client).generate(contact.id)

    assert client.calls == []


async def test_no_persona_is_an_error_and_nothing_is_sent(repos: Repositories) -> None:
    contact = await repos.contact()
    await repos.conversation(contact.id)
    client = ScriptedLLMClient("fine")

    with pytest.raises(PersonaMissing):
        await repos.generator(client).generate(contact.id)

    assert client.calls == []


async def test_an_unknown_contact_is_an_error(repos: Repositories) -> None:
    await repos.personas.replace(PERSONA_MARKER)
    client = ScriptedLLMClient("fine")

    with pytest.raises(ContactNotFound):
        await repos.generator(client).generate(424242)

    assert client.calls == []


async def test_no_incoming_message_is_an_error_and_nothing_is_sent(repos: Repositories) -> None:
    await repos.personas.replace(PERSONA_MARKER)
    contact = await repos.contact()
    await repos.say(contact.id, MessageDirection.OUTGOING, "only mine", 1)
    client = ScriptedLLMClient("fine")

    with pytest.raises(NoMessagesToReplyTo):
        await repos.generator(client).generate(contact.id)

    assert client.calls == []


async def test_a_contact_without_any_message_is_an_error(repos: Repositories) -> None:
    await repos.personas.replace(PERSONA_MARKER)
    contact = await repos.contact()

    with pytest.raises(NoMessagesToReplyTo):
        await repos.generator(ScriptedLLMClient("fine")).generate(contact.id)


async def test_a_prompt_over_the_budget_is_not_sent(repos: Repositories) -> None:
    contact = await ready_contact(repos)
    client = ScriptedLLMClient("fine")

    with pytest.raises(PromptTooLarge):
        await repos.generator(client, budget=50).generate(contact.id)

    assert client.calls == []


async def test_an_empty_reply_is_invalid(repos: Repositories) -> None:
    contact = await ready_contact(repos)

    with pytest.raises(ReplyInvalid):
        await repos.generator(ScriptedLLMClient('""')).generate(contact.id)


async def test_a_reply_that_leaks_the_boundary_is_invalid(repos: Repositories) -> None:
    contact = await ready_contact(repos)

    with pytest.raises(ReplyInvalid):
        await repos.generator(ScriptedLLMClient(f"ok {DIALOGUE_BOUNDARY}")).generate(contact.id)


async def test_a_client_error_propagates_unchanged(repos: Repositories) -> None:
    contact = await ready_contact(repos)
    failure = LLMUnavailableError("provider down")

    with pytest.raises(LLMUnavailableError) as caught:
        await repos.generator(ScriptedLLMClient(failure)).generate(contact.id)

    assert caught.value is failure


async def test_the_summary_slot_reaches_the_prompt(repos: Repositories) -> None:
    contact = await ready_contact(repos)
    client = ScriptedLLMClient("fine")

    await repos.generator(client).generate(contact.id, summary="summary-marker-zq")

    assert "summary-marker-zq" in client.calls[0][0][0].content


async def test_generating_writes_nothing_to_the_database(
    repos: Repositories, migrated_url: URL
) -> None:
    contact = await ready_contact(repos)
    await repos.style_examples.add_many(
        [
            StyleExampleCreate.model_validate(
                {"turns": [{"role": "them", "text": "q"}, {"role": "me", "text": "a"}]}
            )
        ]
    )
    before = await row_counts(migrated_url)
    contact_before = await repos.contacts.get_by_id(contact.id)

    await repos.generator(ScriptedLLMClient("fine")).generate(contact.id)
    with pytest.raises(ReplyInvalid):
        await repos.generator(ScriptedLLMClient("")).generate(contact.id)

    assert await row_counts(migrated_url) == before
    assert await repos.contacts.get_by_id(contact.id) == contact_before


async def test_logs_carry_counters_and_none_of_the_texts(
    repos: Repositories, caplog: pytest.LogCaptureFixture
) -> None:
    contact = await ready_contact(repos)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    caplog.set_level(logging.DEBUG)

    await repos.generator(ScriptedLLMClient(REPLY_MARKER)).generate(contact.id, summary="s-zq")
    with pytest.raises(ReplyInvalid):
        await repos.generator(ScriptedLLMClient("")).generate(contact.id)

    captured = stream.getvalue() + caplog.text
    for marker in (
        PERSONA_MARKER,
        MESSAGE_MARKER,
        REPLY_MARKER,
        BIO_MARKER,
        USERNAME_MARKER,
        "s-zq",
        FAKE_TELEGRAM_ID,
    ):
        assert_absent(marker, captured)
    lines = [r for r in caplog.records if r.name == "cyrano.services.reply_generator"]
    assert len(lines) == 1
    fields = lines[0].__dict__
    assert (fields["examples_kept"], fields["messages_kept"]) == (0, 3)
    assert (fields["input_tokens"], fields["output_tokens"]) == (321, 17)
    assert fields["prompt_characters"] > 0


async def test_a_token_limit_override_reaches_the_client(repos: Repositories) -> None:
    contact = await ready_contact(repos)
    client = ScriptedLLMClient("fine")

    reply = await repos.generator(client).generate(contact.id, max_tokens=800)

    assert client.calls[0][2] == 800
    assert reply.truncated is False


async def test_a_truncated_model_reply_is_flagged_and_still_cleaned(repos: Repositories) -> None:
    contact = await ready_contact(repos)
    client = FakeLLMClient(
        result=LLMResult(
            text="Me: cut short",
            usage=LLMUsage(input_tokens=5, output_tokens=400, cache_hit_tokens=None),
            truncated=True,
        )
    )

    reply = await repos.generator(client).generate(contact.id)

    assert (reply.truncated, reply.text) == (True, "cut short")
