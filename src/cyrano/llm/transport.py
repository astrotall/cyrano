from __future__ import annotations

import asyncio
import logging
import random
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

from cyrano.domain.errors import (
    LLMAuthError,
    LLMError,
    LLMInvalidResponseError,
    LLMRateLimitError,
    LLMRequestError,
    LLMUnavailableError,
)
from cyrano.llm.constants import (
    ATTEMPT_DEADLINE_SECONDS,
    BACKOFF_BASE_SECONDS,
    BACKOFF_CAP_SECONDS,
    BACKOFF_JITTER_RATIO,
    HTTP_CLIENT_ERROR_MAX,
    HTTP_CLIENT_ERROR_MIN,
    HTTP_FORBIDDEN,
    HTTP_OK_MAX,
    HTTP_OK_MIN,
    HTTP_SERVER_ERROR_MIN,
    HTTP_TOO_MANY_REQUESTS,
    HTTP_UNAUTHORIZED,
    MAX_RETRIES,
    RETRY_AFTER_HEADER,
)

logger = logging.getLogger(__name__)

_SYSTEM_RANDOM = random.SystemRandom()
RETRY_AFTER_SECONDS_PATTERN = re.compile(r"\d+(?:\.\d+)?")

Sleep = Callable[[float], Awaitable[None]]
Jitter = Callable[[], float]
Clock = Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class TransportResponse:
    content: bytes
    retries: int


class _NetworkFailure(Exception):
    def __init__(self, error_class: str) -> None:
        super().__init__(error_class)
        self.error_class = error_class


def _utc_now() -> datetime:
    return datetime.now(UTC)


def parse_retry_after(value: str | None, now: Clock = _utc_now) -> float | None:
    if value is None:
        return None
    text = value.strip()
    if RETRY_AFTER_SECONDS_PATTERN.fullmatch(text):
        return float(text)
    try:
        moment = parsedate_to_datetime(text)
    except TypeError, ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return max(0.0, (moment - now()).total_seconds())


def _exhausted(status: int) -> LLMError:
    if status == HTTP_TOO_MANY_REQUESTS:
        return LLMRateLimitError(
            f"LLM provider rate limit reached (HTTP {status})", status_code=status
        )
    return LLMUnavailableError(f"LLM provider failed (HTTP {status})", status_code=status)


def _final_error(status: int) -> LLMError:
    if status in {HTTP_UNAUTHORIZED, HTTP_FORBIDDEN}:
        return LLMAuthError(
            f"LLM provider refused the credentials (HTTP {status})", status_code=status
        )
    if HTTP_CLIENT_ERROR_MIN <= status <= HTTP_CLIENT_ERROR_MAX:
        return LLMRequestError(
            f"LLM provider rejected the request (HTTP {status})", status_code=status
        )
    return LLMInvalidResponseError(
        f"LLM provider answered with an unexpected status (HTTP {status})", status_code=status
    )


def _is_retryable(status: int) -> bool:
    return status == HTTP_TOO_MANY_REQUESTS or status >= HTTP_SERVER_ERROR_MIN


class RetryingTransport:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        sleep: Sleep = asyncio.sleep,
        jitter: Jitter = _SYSTEM_RANDOM.random,
        now: Clock = _utc_now,
        deadline_seconds: float = ATTEMPT_DEADLINE_SECONDS,
        max_retries: int = MAX_RETRIES,
    ) -> None:
        self._client = client
        self._sleep = sleep
        self._jitter = jitter
        self._now = now
        self._deadline_seconds = deadline_seconds
        self._max_retries = max_retries

    async def post(self, path: str, payload: Mapping[str, object]) -> TransportResponse:
        retries = 0
        while True:
            status: int | None = None
            error_class: str | None = None
            retry_after: float | None = None
            try:
                response = await self._send(path, payload)
            except _NetworkFailure as failure:
                error_class = failure.error_class
            else:
                status = response.status_code
                if HTTP_OK_MIN <= status <= HTTP_OK_MAX:
                    return TransportResponse(content=response.content, retries=retries)
                if not _is_retryable(status):
                    raise _final_error(status)
                retry_after = parse_retry_after(response.headers.get(RETRY_AFTER_HEADER), self._now)
                if retry_after is not None and retry_after > BACKOFF_CAP_SECONDS:
                    raise _exhausted(status)
            if retries >= self._max_retries:
                if status is not None:
                    raise _exhausted(status)
                raise LLMUnavailableError(
                    f"LLM provider unreachable after {retries} retries ({error_class})"
                )
            delay = retry_after if retry_after is not None else self._backoff(retries)
            logger.warning(
                "llm request will be retried",
                extra={
                    "retry": retries + 1,
                    "max_retries": self._max_retries,
                    "status": status,
                    "error": error_class,
                    "delay_seconds": delay,
                },
            )
            await self._sleep(delay)
            retries += 1

    def _backoff(self, retries: int) -> float:
        ceiling = min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * 2.0**retries)
        return ceiling * (1 - BACKOFF_JITTER_RATIO * self._jitter())

    async def _send(self, path: str, payload: Mapping[str, object]) -> httpx.Response:
        try:
            async with asyncio.timeout(self._deadline_seconds):
                return await self._client.post(path, json=payload)
        except (httpx.HTTPError, TimeoutError) as exc:
            raise _NetworkFailure(type(exc).__name__) from None
