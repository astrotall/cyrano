from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence

import httpx
from pydantic import BaseModel

from cyrano.config import Settings
from cyrano.domain.enums import Role
from cyrano.domain.llm import LLMMessage
from cyrano.llm.factory import LLMClientFactory
from cyrano.llm.transport import RetryingTransport

BASE_URL = "https://llm.test"
FAKE_API_KEY = "sk-deepseek-secret"
FAKE_REPLY_TEXT = "fake reply text, nothing personal"
FAKE_PROMPT_TEXT = "fake prompt text about a made-up person"

Step = httpx.Response | Exception


def completion_response(
    text: str | None = FAKE_REPLY_TEXT,
    *,
    finish_reason: str = "stop",
    prompt_tokens: int = 12,
    completion_tokens: int = 7,
    cache_hit_tokens: int | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    usage: dict[str, int] = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }
    if cache_hit_tokens is not None:
        usage["prompt_cache_hit_tokens"] = cache_hit_tokens
    body = {
        "id": "fake-id",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": finish_reason,
            }
        ],
        "usage": usage,
    }
    return httpx.Response(200, json=body, headers=headers)


def error_response(
    status: int, *, headers: dict[str, str] | None = None, body: str = "provider says: secret body"
) -> httpx.Response:
    return httpx.Response(status, text=body, headers=headers)


class Script:
    def __init__(self, *steps: Step, repeat_last: bool = False) -> None:
        self._steps = list(steps)
        self._repeat_last = repeat_last
        self.requests: list[httpx.Request] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self._steps:
            msg = "the script has no step left"
            raise AssertionError(msg)
        keep = self._repeat_last and len(self._steps) == 1
        step = self._steps[0] if keep else self._steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step

    def body(self, index: int = 0) -> dict[str, object]:
        content = self.requests[index].content
        loaded: dict[str, object] = json.loads(content)
        return loaded

    def sent_messages(self, index: int = 0) -> list[dict[str, str]]:
        messages = self.body(index)["messages"]
        assert isinstance(messages, list)
        return [dict(message) for message in messages]


class Hanging:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        await asyncio.sleep(3600)
        raise AssertionError


class FakeSleep:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def no_jitter() -> float:
    return 0.0


def transport_for(
    handler: Script | Hanging,
    sleep: FakeSleep,
    *,
    deadline_seconds: float = 5.0,
    max_retries: int = 3,
    jitter: float = 0.0,
) -> tuple[RetryingTransport, httpx.AsyncClient]:
    client = httpx.AsyncClient(base_url=BASE_URL, transport=httpx.MockTransport(handler))
    transport = RetryingTransport(
        client,
        sleep=sleep,
        jitter=lambda: jitter,
        deadline_seconds=deadline_seconds,
        max_retries=max_retries,
    )
    return transport, client


def factory_for(
    settings: Settings, handler: Script | Hanging, sleep: FakeSleep
) -> LLMClientFactory:
    return LLMClientFactory(
        settings, http_transport=httpx.MockTransport(handler), sleep=sleep, jitter=no_jitter
    )


def user(text: str = FAKE_PROMPT_TEXT) -> LLMMessage:
    return LLMMessage(role=Role.USER, content=text)


def system(text: str = "fake system text") -> LLMMessage:
    return LLMMessage(role=Role.SYSTEM, content=text)


def conversation() -> Sequence[LLMMessage]:
    return [system(), user()]


class Answer(BaseModel):
    reply: str
    mood: int


def answer_json(reply: str = "hello", mood: int = 3) -> str:
    return json.dumps({"reply": reply, "mood": mood})
