from __future__ import annotations

import httpx
import pytest

from cyrano.config import Settings, load_settings
from cyrano.domain.enums import LLMStep, Role
from cyrano.domain.errors import LLMInvalidResponseError, LLMRequestError
from cyrano.domain.llm import LLMMessage
from tests.llm.fakes import (
    FAKE_API_KEY,
    FAKE_REPLY_TEXT,
    FakeSleep,
    Script,
    completion_response,
    conversation,
    factory_for,
    system,
    user,
)


async def test_a_reply_comes_back_with_usage(settings: Settings, sleep: FakeSleep) -> None:
    script = Script(completion_response(prompt_tokens=21, completion_tokens=8, cache_hit_tokens=16))
    factory = factory_for(settings, script, sleep)

    result = await factory.get(LLMStep.REPLY).complete(conversation())
    await factory.aclose()

    assert result.text == FAKE_REPLY_TEXT
    assert result.usage.input_tokens == 21
    assert result.usage.output_tokens == 8
    assert result.usage.cache_hit_tokens == 16
    assert result.truncated is False


async def test_missing_cache_hit_tokens_stay_none(settings: Settings, sleep: FakeSleep) -> None:
    factory = factory_for(settings, Script(completion_response()), sleep)

    result = await factory.get(LLMStep.REPLY).complete(conversation())
    await factory.aclose()

    assert result.usage.cache_hit_tokens is None


async def test_the_length_finish_reason_marks_the_reply_truncated(
    settings: Settings, sleep: FakeSleep
) -> None:
    factory = factory_for(settings, Script(completion_response(finish_reason="length")), sleep)

    result = await factory.get(LLMStep.REPLY).complete(conversation())
    await factory.aclose()

    assert result.truncated is True


@pytest.mark.parametrize("text", ["", "   \n ", None])
async def test_an_empty_reply_is_an_invalid_response(
    settings: Settings, sleep: FakeSleep, text: str | None
) -> None:
    factory = factory_for(settings, Script(completion_response(text)), sleep)

    with pytest.raises(LLMInvalidResponseError):
        await factory.get(LLMStep.REPLY).complete(conversation())
    await factory.aclose()


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="not json at all"),
        httpx.Response(200, json={"choices": []}),
        httpx.Response(200, json={"choices": [{"message": {"content": "x"}}]}),
        httpx.Response(200, json=["unexpected"]),
    ],
)
async def test_an_unexpected_body_shape_is_an_invalid_response(
    settings: Settings, sleep: FakeSleep, response: httpx.Response
) -> None:
    factory = factory_for(settings, Script(response), sleep)

    with pytest.raises(LLMInvalidResponseError) as raised:
        await factory.get(LLMStep.REPLY).complete(conversation())
    await factory.aclose()

    assert "not json" not in str(raised.value)


async def test_the_request_body_has_the_step_settings_and_no_response_format(
    settings: Settings, sleep: FakeSleep
) -> None:
    script = Script(completion_response())
    factory = factory_for(settings, script, sleep)

    await factory.get(LLMStep.REPLY).complete(
        [system("s1"), user("u1"), LLMMessage(role=Role.ASSISTANT, content="a1"), user("u2")]
    )
    await factory.aclose()

    body = script.body()
    assert body["model"] == "deepseek-chat"
    assert body["temperature"] == 1.0
    assert body["max_tokens"] == 400
    assert body["stream"] is False
    assert "response_format" not in body
    assert script.sent_messages() == [
        {"role": "system", "content": "s1"},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
    ]


async def test_thinking_is_sent_as_disabled_by_default(
    settings: Settings, sleep: FakeSleep
) -> None:
    script = Script(completion_response())
    factory = factory_for(settings, script, sleep)

    await factory.get(LLMStep.REPLY).complete(conversation())
    await factory.aclose()

    assert script.body()["thinking"] == {"type": "disabled"}


async def test_thinking_can_be_enabled_by_setting(
    full_env: dict[str, str], monkeypatch: pytest.MonkeyPatch, sleep: FakeSleep
) -> None:
    monkeypatch.setenv("DEEPSEEK_THINKING", "true")
    script = Script(completion_response())
    factory = factory_for(load_settings(env_file=None), script, sleep)

    await factory.get(LLMStep.REPLY).complete(conversation())
    await factory.aclose()

    assert script.body()["thinking"] == {"type": "enabled"}


async def test_the_request_goes_to_the_chat_completions_url_with_the_key_in_the_header(
    settings: Settings, sleep: FakeSleep
) -> None:
    script = Script(completion_response())
    factory = factory_for(settings, script, sleep)

    await factory.get(LLMStep.REPLY).complete(conversation())
    await factory.aclose()

    request = script.requests[0]
    assert request.method == "POST"
    assert str(request.url) == "https://api.deepseek.com/chat/completions"
    assert request.headers["Authorization"] == f"Bearer {FAKE_API_KEY}"
    assert FAKE_API_KEY.encode() not in request.content


async def test_a_custom_base_url_with_a_path_is_used(
    full_env: dict[str, str], monkeypatch: pytest.MonkeyPatch, sleep: FakeSleep
) -> None:
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://proxy.test/v1")
    script = Script(completion_response())
    factory = factory_for(load_settings(env_file=None), script, sleep)

    await factory.get(LLMStep.REPLY).complete(conversation())
    await factory.aclose()

    assert str(script.requests[0].url) == "https://proxy.test/v1/chat/completions"


@pytest.mark.parametrize(
    "messages",
    [[], [system()], [system(), LLMMessage(role=Role.ASSISTANT, content="a")]],
)
async def test_a_request_without_a_user_message_fails_before_any_http_call(
    settings: Settings, sleep: FakeSleep, messages: list[LLMMessage]
) -> None:
    script = Script(completion_response())
    factory = factory_for(settings, script, sleep)

    with pytest.raises(LLMRequestError):
        await factory.get(LLMStep.REPLY).complete(messages)
    await factory.aclose()

    assert script.requests == []


@pytest.mark.parametrize(("temperature", "max_tokens"), [(-0.1, None), (2.1, None), (None, 0)])
async def test_invalid_per_call_arguments_fail_before_any_http_call(
    settings: Settings, sleep: FakeSleep, temperature: float | None, max_tokens: int | None
) -> None:
    script = Script(completion_response())
    factory = factory_for(settings, script, sleep)

    with pytest.raises(LLMRequestError):
        await factory.get(LLMStep.REPLY).complete(
            conversation(), temperature=temperature, max_tokens=max_tokens
        )
    await factory.aclose()

    assert script.requests == []
