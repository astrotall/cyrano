from __future__ import annotations

import pytest
from pydantic import ValidationError

from cyrano.domain.errors import (
    DomainError,
    ModeNameNotFound,
    NoMessagesToReplyTo,
    PersonaMissing,
    PromptTooLarge,
    ReplyInvalid,
)
from cyrano.domain.llm import LLMUsage
from cyrano.domain.reply import (
    MIN_HISTORY_MESSAGES,
    REPLY_MAX_CHARS,
    ContactFacts,
    GeneratedReply,
)


def test_the_named_limits() -> None:
    assert (MIN_HISTORY_MESSAGES, REPLY_MAX_CHARS) == (4, 600)


def test_a_generated_reply_is_frozen_and_rejects_negative_counters() -> None:
    fields = {
        "text": "hi",
        "usage": LLMUsage(input_tokens=1, output_tokens=1),
        "examples_offered": 2,
        "examples_kept": 1,
        "messages_offered": 3,
        "messages_kept": 3,
        "prompt_characters": 10,
    }
    reply = GeneratedReply.model_validate(fields)

    with pytest.raises(ValidationError):
        reply.text = "other"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        GeneratedReply.model_validate({**fields, "examples_kept": -1})
    with pytest.raises(ValidationError):
        GeneratedReply.model_validate({**fields, "extra": 1})


def test_facts_reject_unknown_fields_so_nothing_else_can_slip_in() -> None:
    with pytest.raises(ValidationError):
        ContactFacts.model_validate({"name": "Anna", "username": "forbidden"})


@pytest.mark.parametrize(
    "error",
    [
        PersonaMissing(),
        ModeNameNotFound(),
        NoMessagesToReplyTo(5),
        PromptTooLarge(30000, 24000),
        ReplyInvalid("empty after cleaning"),
    ],
)
def test_the_reply_errors_are_domain_errors_with_readable_messages(error: DomainError) -> None:
    assert isinstance(error, DomainError)
    assert str(error)


def test_the_messages_carry_ids_and_counts_only() -> None:
    assert "5" in str(NoMessagesToReplyTo(5))
    assert "30000" in str(PromptTooLarge(30000, 24000))
    assert "24000" in str(PromptTooLarge(30000, 24000))
