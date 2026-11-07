from __future__ import annotations

import asyncio

import httpx

from cyrano.config import Settings
from cyrano.domain.enums import LLMStep
from cyrano.llm.constants import (
    AUTHORIZATION_HEADER,
    BEARER_PREFIX,
    CONNECT_TIMEOUT_SECONDS,
    POOL_TIMEOUT_SECONDS,
    READ_TIMEOUT_SECONDS,
    WRITE_TIMEOUT_SECONDS,
)
from cyrano.llm.deepseek import DeepSeekClient, StepConfig
from cyrano.llm.transport import Jitter, RetryingTransport, Sleep


def step_config(settings: Settings, step: LLMStep) -> StepConfig:
    if step is LLMStep.REPLY:
        return StepConfig(
            model=settings.reply_model,
            temperature=settings.llm_reply_temperature,
            max_tokens=settings.llm_reply_max_tokens,
        )
    return StepConfig(
        model=settings.summary_model,
        temperature=settings.llm_summary_temperature,
        max_tokens=settings.llm_summary_max_tokens,
    )


class LLMClientFactory:
    def __init__(
        self,
        settings: Settings,
        *,
        http_transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep = asyncio.sleep,
        jitter: Jitter | None = None,
    ) -> None:
        self._settings = settings
        self._http = httpx.AsyncClient(
            base_url=settings.deepseek_base_url,
            headers={
                AUTHORIZATION_HEADER: BEARER_PREFIX + settings.deepseek_api_key.get_secret_value()
            },
            timeout=httpx.Timeout(
                connect=CONNECT_TIMEOUT_SECONDS,
                read=READ_TIMEOUT_SECONDS,
                write=WRITE_TIMEOUT_SECONDS,
                pool=POOL_TIMEOUT_SECONDS,
            ),
            transport=http_transport,
        )
        self._transport = (
            RetryingTransport(self._http, sleep=sleep)
            if jitter is None
            else RetryingTransport(self._http, sleep=sleep, jitter=jitter)
        )

    def get(self, step: LLMStep) -> DeepSeekClient:
        return DeepSeekClient(
            self._transport,
            step=step,
            config=step_config(self._settings, step),
            thinking=self._settings.deepseek_thinking,
        )

    async def aclose(self) -> None:
        await self._http.aclose()
