from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from pydantic import BaseModel, ValidationError

from cyrano.domain.enums import LLMStep, Role
from cyrano.domain.errors import LLMInvalidResponseError, LLMRequestError
from cyrano.domain.llm import LLMMessage, LLMResult, LLMUsage
from cyrano.llm.constants import (
    CHAT_COMPLETIONS_PATH,
    FINISH_REASON_LENGTH,
    JSON_RESPONSE_FORMAT,
    LLM_JSON_MAX_RETRIES,
    MAX_TEMPERATURE,
    MIN_TEMPERATURE,
    THINKING_DISABLED,
    THINKING_ENABLED,
)
from cyrano.llm.json_reply import JsonProblems, parse_json_reply
from cyrano.llm.prompts import (
    INVALID_REPLY_INSTRUCTION,
    SCHEMA_INSTRUCTION,
    TRUNCATED_REPLY_INSTRUCTION,
)
from cyrano.llm.transport import RetryingTransport
from cyrano.llm.wire import WireResponse

logger = logging.getLogger(__name__)

MILLISECONDS = 1000


@dataclass(frozen=True, slots=True)
class StepConfig:
    model: str
    temperature: float
    max_tokens: int


class DeepSeekClient:
    def __init__(
        self,
        transport: RetryingTransport,
        *,
        step: LLMStep,
        config: StepConfig,
        thinking: bool = False,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._transport = transport
        self._step = step
        self._config = config
        self._thinking = thinking
        self._clock = clock

    @property
    def step(self) -> LLMStep:
        return self._step

    @property
    def model(self) -> str:
        return self._config.model

    async def complete(
        self,
        messages: Sequence[LLMMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResult:
        resolved_temperature, resolved_max_tokens = self._resolve(messages, temperature, max_tokens)
        result = await self._request(
            messages, resolved_temperature, resolved_max_tokens, json_mode=False
        )
        if not result.text.strip():
            raise LLMInvalidResponseError("LLM provider returned an empty reply")
        return result

    async def complete_json[T: BaseModel](
        self,
        messages: Sequence[LLMMessage],
        schema: type[T],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> T:
        resolved_temperature, resolved_max_tokens = self._resolve(messages, temperature, max_tokens)
        conversation = _with_schema_message(messages, schema)
        attempts = LLM_JSON_MAX_RETRIES + 1
        for attempt in range(1, attempts + 1):
            result = await self._request(
                conversation, resolved_temperature, resolved_max_tokens, json_mode=True
            )
            if not result.text.strip():
                self._log_rejected(attempt, attempts, "empty", result.truncated, 0)
                continue
            parsed = parse_json_reply(result.text, schema)
            if not isinstance(parsed, JsonProblems):
                return parsed
            self._log_rejected(attempt, attempts, "invalid", result.truncated, parsed.count)
            conversation = [
                *conversation,
                LLMMessage(role=Role.ASSISTANT, content=result.text),
                LLMMessage(role=Role.USER, content=_correction(parsed, result.truncated)),
            ]
        msg = f"LLM reply was not valid JSON after {attempts} attempts"
        raise LLMInvalidResponseError(msg)

    def _resolve(
        self,
        messages: Sequence[LLMMessage],
        temperature: float | None,
        max_tokens: int | None,
    ) -> tuple[float, int]:
        if not messages:
            raise LLMRequestError("LLM request has no messages")
        if not any(message.role is Role.USER for message in messages):
            raise LLMRequestError("LLM request has no user message")
        resolved_temperature = self._config.temperature if temperature is None else temperature
        resolved_max_tokens = self._config.max_tokens if max_tokens is None else max_tokens
        if not MIN_TEMPERATURE <= resolved_temperature <= MAX_TEMPERATURE:
            raise LLMRequestError("LLM request temperature is out of range")
        if resolved_max_tokens < 1:
            raise LLMRequestError("LLM request max_tokens must be positive")
        return resolved_temperature, resolved_max_tokens

    def _payload(
        self,
        messages: Sequence[LLMMessage],
        temperature: float,
        max_tokens: int,
        *,
        json_mode: bool,
    ) -> Mapping[str, object]:
        payload: dict[str, object] = {
            "model": self._config.model,
            "messages": [
                {"role": message.role.value, "content": message.content} for message in messages
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "thinking": THINKING_ENABLED if self._thinking else THINKING_DISABLED,
            "stream": False,
        }
        if json_mode:
            payload["response_format"] = JSON_RESPONSE_FORMAT
        return payload

    async def _request(
        self,
        messages: Sequence[LLMMessage],
        temperature: float,
        max_tokens: int,
        *,
        json_mode: bool,
    ) -> LLMResult:
        payload = self._payload(messages, temperature, max_tokens, json_mode=json_mode)
        started = self._clock()
        response = await self._transport.post(CHAT_COMPLETIONS_PATH, payload)
        duration_ms = round((self._clock() - started) * MILLISECONDS)
        try:
            body = WireResponse.model_validate(json.loads(response.content))
        except ValueError, ValidationError:
            raise LLMInvalidResponseError("LLM provider response has an unexpected shape") from None
        choice = body.choices[0]
        usage = LLMUsage(
            input_tokens=body.usage.prompt_tokens,
            output_tokens=body.usage.completion_tokens,
            cache_hit_tokens=body.usage.prompt_cache_hit_tokens,
        )
        self._log_call(usage, duration_ms, response.retries)
        return LLMResult(
            text=choice.message.content or "",
            usage=usage,
            truncated=choice.finish_reason == FINISH_REASON_LENGTH,
        )

    def _log_call(self, usage: LLMUsage, duration_ms: int, retries: int) -> None:
        extra: dict[str, object] = {
            "step": self._step.value,
            "model": self._config.model,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "duration_ms": duration_ms,
            "retries": retries,
        }
        if usage.cache_hit_tokens is not None:
            extra["cache_hit_tokens"] = usage.cache_hit_tokens
        logger.info("llm call finished", extra=extra)

    def _log_rejected(
        self, attempt: int, attempts: int, reason: str, truncated: bool, problem_count: int
    ) -> None:
        logger.warning(
            "llm json reply rejected",
            extra={
                "step": self._step.value,
                "attempt": attempt,
                "max_attempts": attempts,
                "reason": reason,
                "truncated": truncated,
                "problem_count": problem_count,
            },
        )


def _with_schema_message[T: BaseModel](
    messages: Sequence[LLMMessage], schema: type[T]
) -> list[LLMMessage]:
    leading = 0
    while leading < len(messages) and messages[leading].role is Role.SYSTEM:
        leading += 1
    instruction = LLMMessage(
        role=Role.SYSTEM,
        content=SCHEMA_INSTRUCTION.format(
            schema=json.dumps(schema.model_json_schema(), ensure_ascii=False)
        ),
    )
    return [*messages[:leading], instruction, *messages[leading:]]


def _correction(problems: JsonProblems, truncated: bool) -> str:
    text = INVALID_REPLY_INSTRUCTION.format(problems="\n".join(problems.lines))
    return f"{text}\n{TRUNCATED_REPLY_INSTRUCTION}" if truncated else text
