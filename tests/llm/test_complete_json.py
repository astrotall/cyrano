from __future__ import annotations

import logging

import pytest

from cyrano.config import Settings
from cyrano.domain.enums import LLMStep
from cyrano.domain.errors import LLMInvalidResponseError, LLMRequestError
from tests.llm.fakes import (
    Answer,
    FakeSleep,
    Script,
    answer_json,
    completion_response,
    conversation,
    factory_for,
    system,
    user,
)

SECRET_BAD_REPLY = "super-secret-invalid-reply 12345"


async def _run(
    settings: Settings,
    sleep: FakeSleep,
    script: Script,
    *,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> Answer:
    factory = factory_for(settings, script, sleep)
    try:
        return await factory.get(LLMStep.REPLY).complete_json(
            conversation(), Answer, temperature=temperature, max_tokens=max_tokens
        )
    finally:
        await factory.aclose()


async def test_a_bare_json_reply_is_validated_and_returned(
    settings: Settings, sleep: FakeSleep
) -> None:
    script = Script(completion_response(answer_json("hi", 4)))

    answer = await _run(settings, sleep, script)

    assert answer == Answer(reply="hi", mood=4)
    assert len(script.requests) == 1


async def test_a_fenced_json_block_anywhere_in_the_text_is_used(
    settings: Settings, sleep: FakeSleep
) -> None:
    text = f"Sure, here it is:\n```json\n{answer_json('fenced', 1)}\n```\nHope it helps."
    script = Script(completion_response(text))

    answer = await _run(settings, sleep, script)

    assert answer == Answer(reply="fenced", mood=1)


async def test_a_fence_without_a_language_label_is_used(
    settings: Settings, sleep: FakeSleep
) -> None:
    script = Script(completion_response(f"```\n{answer_json('plain fence', 2)}\n```"))

    answer = await _run(settings, sleep, script)

    assert answer.reply == "plain fence"


async def test_json_mode_and_the_schema_message_are_sent(
    settings: Settings, sleep: FakeSleep
) -> None:
    script = Script(completion_response(answer_json()))

    await _run(settings, sleep, script)

    assert script.body()["response_format"] == {"type": "json_object"}
    messages = script.sent_messages()
    assert [m["role"] for m in messages] == ["system", "system", "user"]
    assert messages[0]["content"] == "fake system text"
    assert "JSON" in messages[1]["content"]
    assert '"reply"' in messages[1]["content"]
    assert '"mood"' in messages[1]["content"]
    assert messages[2]["role"] == "user"


async def test_the_schema_message_follows_all_leading_system_messages(
    settings: Settings, sleep: FakeSleep
) -> None:
    script = Script(completion_response(answer_json()))
    factory = factory_for(settings, script, sleep)

    await factory.get(LLMStep.REPLY).complete_json(
        [system("first"), system("second"), user("question")], Answer
    )
    await factory.aclose()

    messages = script.sent_messages()
    assert [m["content"] for m in messages][:2] == ["first", "second"]
    assert "JSON" in messages[2]["content"]
    assert messages[3] == {"role": "user", "content": "question"}


async def test_an_invalid_reply_is_sent_back_with_the_problems_and_then_succeeds(
    settings: Settings, sleep: FakeSleep
) -> None:
    bad = '{"reply": "kept-value-1", "mood": "kept-value-2"}'
    script = Script(completion_response(bad), completion_response(answer_json("fixed", 5)))

    answer = await _run(settings, sleep, script)

    assert answer == Answer(reply="fixed", mood=5)
    retry = script.sent_messages(1)
    assert retry[-2] == {"role": "assistant", "content": bad}
    assert retry[-1]["role"] == "user"
    correction = retry[-1]["content"]
    assert "mood" in correction
    assert "kept-value-1" not in correction
    assert "kept-value-2" not in correction
    assert "shorter" not in correction


async def test_a_reply_that_is_not_json_lists_a_root_problem(
    settings: Settings, sleep: FakeSleep
) -> None:
    script = Script(completion_response("just words"), completion_response(answer_json()))

    await _run(settings, sleep, script)

    correction = script.sent_messages(1)[-1]["content"]
    assert "(root)" in correction
    assert "just words" not in correction.split("\n", 1)[1]


async def test_a_missing_field_is_reported_by_its_path(
    settings: Settings, sleep: FakeSleep
) -> None:
    script = Script(completion_response('{"reply": "x"}'), completion_response(answer_json()))

    await _run(settings, sleep, script)

    assert "- mood:" in script.sent_messages(1)[-1]["content"]


async def test_a_truncated_invalid_reply_asks_for_a_shorter_output(
    settings: Settings, sleep: FakeSleep
) -> None:
    script = Script(
        completion_response('{"reply": "cut off', finish_reason="length"),
        completion_response(answer_json()),
    )

    await _run(settings, sleep, script)

    assert "shorter" in script.sent_messages(1)[-1]["content"]


async def test_an_empty_reply_is_retried_unchanged(settings: Settings, sleep: FakeSleep) -> None:
    script = Script(completion_response("   "), completion_response(answer_json("late", 2)))

    answer = await _run(settings, sleep, script)

    assert answer.reply == "late"
    assert script.sent_messages(1) == script.sent_messages(0)


async def test_always_invalid_raises_after_the_attempts_without_the_raw_reply(
    settings: Settings, sleep: FakeSleep, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    script = Script(completion_response(SECRET_BAD_REPLY), repeat_last=True)

    with pytest.raises(LLMInvalidResponseError) as raised:
        await _run(settings, sleep, script)

    assert len(script.requests) == 3
    assert SECRET_BAD_REPLY not in str(raised.value)
    assert SECRET_BAD_REPLY not in repr(raised.value)
    assert SECRET_BAD_REPLY not in caplog.text
    assert "secret-invalid" not in "".join(str(vars(r)) for r in caplog.records)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


async def test_empty_replies_share_the_attempt_budget_with_invalid_ones(
    settings: Settings, sleep: FakeSleep
) -> None:
    script = Script(
        completion_response(""),
        completion_response("nope"),
        completion_response(""),
        repeat_last=False,
    )

    with pytest.raises(LLMInvalidResponseError):
        await _run(settings, sleep, script)

    assert len(script.requests) == 3


async def test_a_partially_valid_object_is_never_returned(
    settings: Settings, sleep: FakeSleep
) -> None:
    script = Script(completion_response('{"reply": "only half"}'), repeat_last=True)

    with pytest.raises(LLMInvalidResponseError):
        await _run(settings, sleep, script)


async def test_per_call_arguments_override_the_defaults_for_json_too(
    settings: Settings, sleep: FakeSleep
) -> None:
    script = Script(completion_response(answer_json()))

    await _run(settings, sleep, script, temperature=0.0, max_tokens=50)

    assert script.body()["temperature"] == 0.0
    assert script.body()["max_tokens"] == 50


async def test_a_json_request_without_a_user_message_fails_before_any_http_call(
    settings: Settings, sleep: FakeSleep
) -> None:
    script = Script(completion_response(answer_json()))
    factory = factory_for(settings, script, sleep)

    with pytest.raises(LLMRequestError):
        await factory.get(LLMStep.REPLY).complete_json([system()], Answer)
    await factory.aclose()

    assert script.requests == []


async def test_rejected_replies_are_logged_as_warnings_with_counters_only(
    settings: Settings, sleep: FakeSleep, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    script = Script(completion_response(SECRET_BAD_REPLY), completion_response(answer_json()))

    await _run(settings, sleep, script)

    rejected = [r for r in caplog.records if r.getMessage() == "llm json reply rejected"]
    assert len(rejected) == 1
    assert rejected[0].levelno == logging.WARNING
    assert rejected[0].__dict__["attempt"] == 1
    assert rejected[0].__dict__["reason"] == "invalid"
    assert SECRET_BAD_REPLY not in caplog.text
