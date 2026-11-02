"""A tiny request that proves the LLM client, the key and the network work together."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from cyrano.domain.enums import Role
from cyrano.domain.llm import LLMMessage
from cyrano.services.llm_client import LLMClient

CHECK_SYSTEM_PROMPT = "Reply with the single word OK."
CHECK_USER_PROMPT = "ping"
CHECK_MAX_TOKENS = 5
MILLISECONDS = 1000


@dataclass(frozen=True, slots=True)
class LLMCheckReport:
    input_tokens: int
    output_tokens: int
    cache_hit_tokens: int | None
    latency_ms: int


async def check_llm(
    client: LLMClient, clock: Callable[[], float] = time.monotonic
) -> LLMCheckReport:
    started = clock()
    result = await client.complete(
        [
            LLMMessage(role=Role.SYSTEM, content=CHECK_SYSTEM_PROMPT),
            LLMMessage(role=Role.USER, content=CHECK_USER_PROMPT),
        ],
        max_tokens=CHECK_MAX_TOKENS,
    )
    return LLMCheckReport(
        input_tokens=result.usage.input_tokens,
        output_tokens=result.usage.output_tokens,
        cache_hit_tokens=result.usage.cache_hit_tokens,
        latency_ms=round((clock() - started) * MILLISECONDS),
    )
