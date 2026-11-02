from __future__ import annotations

import io
import json
import logging

import httpx
import pytest

from cyrano.config import Settings
from cyrano.domain.enums import LLMStep
from cyrano.domain.errors import LLMAuthError, LLMError
from cyrano.llm.deepseek import DeepSeekClient, StepConfig
from cyrano.llm.transport import RetryingTransport
from cyrano.logging import JsonFormatter
from tests.llm.fakes import (
    FAKE_API_KEY,
    FAKE_PROMPT_TEXT,
    FAKE_REPLY_TEXT,
    Answer,
    FakeSleep,
    Script,
    answer_json,
    completion_response,
    conversation,
    error_response,
    factory_for,
)


def _call_lines(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        r for r in caplog.records if r.name == "cyrano.llm.deepseek" and r.levelno == logging.INFO
    ]


def _capture_everything() -> io.StringIO:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    return stream


async def test_one_info_line_per_call_carries_the_usage(
    settings: Settings, sleep: FakeSleep, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    script = Script(error_response(503), completion_response(cache_hit_tokens=5))
    factory = factory_for(settings, script, sleep)

    await factory.get(LLMStep.REPLY).complete(conversation())
    await factory.aclose()

    lines = _call_lines(caplog)
    assert len(lines) == 1
    fields = lines[0].__dict__
    assert fields["step"] == "reply"
    assert fields["model"] == "deepseek-chat"
    assert fields["input_tokens"] == 12
    assert fields["output_tokens"] == 7
    assert fields["cache_hit_tokens"] == 5
    assert fields["retries"] == 1
    assert isinstance(fields["duration_ms"], int)
    assert fields["duration_ms"] >= 0


async def test_the_cache_hit_field_is_absent_when_the_provider_sends_none(
    settings: Settings, sleep: FakeSleep, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    factory = factory_for(settings, Script(completion_response()), sleep)

    await factory.get(LLMStep.SUMMARY).complete(conversation())
    await factory.aclose()

    line = _call_lines(caplog)[0]
    assert "cache_hit_tokens" not in line.__dict__
    assert line.__dict__["step"] == "summary"


async def test_duration_comes_from_the_injected_clock(
    settings: Settings, sleep: FakeSleep, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    ticks = iter([10.0, 10.25])
    client = httpx.AsyncClient(
        base_url="https://llm.test", transport=httpx.MockTransport(Script(completion_response()))
    )
    deepseek = DeepSeekClient(
        RetryingTransport(client, sleep=sleep),
        step=LLMStep.REPLY,
        config=StepConfig(model="m", temperature=1.0, max_tokens=5),
        clock=lambda: next(ticks),
    )

    await deepseek.complete(conversation())
    await client.aclose()

    line = _call_lines(caplog)[0]
    assert line.__dict__["duration_ms"] == 250


async def test_no_prompt_reply_body_or_key_appears_in_any_log_output(
    settings: Settings, sleep: FakeSleep
) -> None:
    stream = _capture_everything()
    script = Script(
        error_response(429),
        httpx.ReadTimeout("slow with FAKE_PROMPT_TEXT"),
        completion_response("not json " + FAKE_REPLY_TEXT),
        completion_response(answer_json(FAKE_REPLY_TEXT)),
    )
    factory = factory_for(settings, script, sleep)

    await factory.get(LLMStep.REPLY).complete_json(conversation(), Answer)
    await factory.aclose()

    output = stream.getvalue()
    assert output
    for forbidden in (FAKE_PROMPT_TEXT, FAKE_REPLY_TEXT, FAKE_API_KEY, "secret body", "not json"):
        assert forbidden not in output


async def test_failures_log_and_raise_without_bodies_or_keys(
    settings: Settings, sleep: FakeSleep
) -> None:
    stream = _capture_everything()
    script = Script(error_response(401, body=f"bad key {FAKE_API_KEY}"), repeat_last=True)
    factory = factory_for(settings, script, sleep)

    with pytest.raises(LLMAuthError) as raised:
        await factory.get(LLMStep.REPLY).complete(conversation())
    await factory.aclose()

    assert isinstance(raised.value, LLMError)
    for text in (stream.getvalue(), str(raised.value), repr(raised.value)):
        assert FAKE_API_KEY not in text
        assert FAKE_PROMPT_TEXT not in text


def test_every_log_line_is_valid_json(settings: Settings) -> None:
    stream = _capture_everything()
    logging.getLogger("cyrano.llm.deepseek").info("probe", extra={"step": "reply"})

    for line in stream.getvalue().splitlines():
        json.loads(line)
