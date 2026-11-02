from __future__ import annotations

import pytest
from pydantic import ValidationError

from cyrano.domain.enums import LLMStep, Role
from cyrano.domain.errors import (
    DomainError,
    LLMAuthError,
    LLMError,
    LLMInvalidResponseError,
    LLMRateLimitError,
    LLMRequestError,
    LLMUnavailableError,
)
from cyrano.domain.llm import LLMMessage, LLMResult, LLMUsage


def test_roles_and_steps_have_the_wire_values() -> None:
    assert [role.value for role in Role] == ["system", "user", "assistant"]
    assert [step.value for step in LLMStep] == ["reply", "summary"]


def test_a_message_is_frozen_and_rejects_unknown_fields() -> None:
    message = LLMMessage(role=Role.USER, content="fake text")

    with pytest.raises(ValidationError):
        message.content = "other"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        LLMMessage.model_validate({"role": "user", "content": "x", "extra": 1})
    with pytest.raises(ValidationError):
        LLMMessage.model_validate({"role": "tool", "content": "x"})


def test_usage_rejects_negative_counts_and_allows_a_missing_cache_figure() -> None:
    assert LLMUsage(input_tokens=1, output_tokens=2).cache_hit_tokens is None
    with pytest.raises(ValidationError):
        LLMUsage(input_tokens=-1, output_tokens=0)
    with pytest.raises(ValidationError):
        LLMUsage(input_tokens=0, output_tokens=0, cache_hit_tokens=-1)


def test_a_result_carries_text_usage_and_the_truncated_flag() -> None:
    result = LLMResult(text="t", usage=LLMUsage(input_tokens=1, output_tokens=2), truncated=True)

    assert result.truncated is True
    assert result.usage.output_tokens == 2


@pytest.mark.parametrize(
    "error_type",
    [
        LLMAuthError,
        LLMRequestError,
        LLMRateLimitError,
        LLMUnavailableError,
        LLMInvalidResponseError,
    ],
)
def test_every_llm_error_is_an_llm_error_with_an_optional_status(
    error_type: type[LLMError],
) -> None:
    error = error_type("readable", status_code=418)

    assert isinstance(error, LLMError)
    assert isinstance(error, DomainError)
    assert error.status_code == 418
    assert error_type("readable").status_code is None
