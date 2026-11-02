from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest

from cyrano.domain.errors import (
    LLMAuthError,
    LLMError,
    LLMInvalidResponseError,
    LLMRateLimitError,
    LLMRequestError,
    LLMUnavailableError,
)
from cyrano.llm.transport import RetryingTransport, parse_retry_after
from tests.llm.fakes import (
    BASE_URL,
    FakeSleep,
    Hanging,
    Script,
    completion_response,
    error_response,
    transport_for,
)

PATH = "/chat/completions"
PAYLOAD = {"model": "fake"}
NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


@pytest.mark.parametrize("status", [429, 500, 502, 503])
async def test_a_retryable_status_is_retried_and_then_succeeds(
    sleep: FakeSleep, status: int
) -> None:
    script = Script(error_response(status), completion_response())
    transport, client = transport_for(script, sleep)

    response = await transport.post(PATH, PAYLOAD)
    await client.aclose()

    assert response.retries == 1
    assert len(script.requests) == 2
    assert sleep.delays == [1.0]


async def test_a_network_error_is_retried_and_then_succeeds(sleep: FakeSleep) -> None:
    script = Script(httpx.ConnectError("refused"), completion_response())
    transport, client = transport_for(script, sleep)

    response = await transport.post(PATH, PAYLOAD)
    await client.aclose()

    assert response.retries == 1
    assert sleep.delays == [1.0]


async def test_backoff_grows_exponentially_and_is_capped(sleep: FakeSleep) -> None:
    script = Script(
        error_response(503), error_response(503), error_response(503), completion_response()
    )
    transport, client = transport_for(script, sleep)

    await transport.post(PATH, PAYLOAD)
    await client.aclose()

    assert sleep.delays == [1.0, 2.0, 4.0]


async def test_jitter_lowers_the_delay_within_half_of_the_ceiling(sleep: FakeSleep) -> None:
    script = Script(error_response(503), error_response(503), completion_response())
    transport, client = transport_for(script, sleep, jitter=1.0)

    await transport.post(PATH, PAYLOAD)
    await client.aclose()

    assert sleep.delays == [0.5, 1.0]


async def test_the_backoff_never_exceeds_the_cap(sleep: FakeSleep) -> None:
    script = Script(*[error_response(503)] * 7, completion_response())
    transport, client = transport_for(script, sleep, max_retries=7)

    await transport.post(PATH, PAYLOAD)
    await client.aclose()

    assert sleep.delays == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]


@pytest.mark.parametrize(
    ("step", "error_type", "status"),
    [
        (error_response(429), LLMRateLimitError, 429),
        (error_response(500), LLMUnavailableError, 500),
        (error_response(503), LLMUnavailableError, 503),
        (httpx.ReadTimeout("slow"), LLMUnavailableError, None),
        (httpx.ConnectError("refused"), LLMUnavailableError, None),
    ],
)
async def test_retries_exhausted_raise_the_error_of_that_kind(
    sleep: FakeSleep,
    step: httpx.Response | Exception,
    error_type: type[LLMError],
    status: int | None,
) -> None:
    script = Script(step, repeat_last=True)
    transport, client = transport_for(script, sleep)

    with pytest.raises(error_type) as raised:
        await transport.post(PATH, PAYLOAD)
    await client.aclose()

    assert type(raised.value) is error_type
    assert raised.value.status_code == status
    assert len(script.requests) == 4
    assert sleep.delays == [1.0, 2.0, 4.0]
    assert "secret body" not in str(raised.value)


async def test_retry_after_in_seconds_replaces_the_backoff(sleep: FakeSleep) -> None:
    script = Script(error_response(429, headers={"Retry-After": "7"}), completion_response())
    transport, client = transport_for(script, sleep)

    await transport.post(PATH, PAYLOAD)
    await client.aclose()

    assert sleep.delays == [7.0]


async def test_retry_after_as_an_http_date_replaces_the_backoff(sleep: FakeSleep) -> None:
    moment = format_datetime(NOW + timedelta(seconds=12), usegmt=True)
    script = Script(error_response(503, headers={"Retry-After": moment}), completion_response())
    client = httpx.AsyncClient(base_url=BASE_URL, transport=httpx.MockTransport(script))
    transport = RetryingTransport(client, sleep=sleep, now=lambda: NOW)

    await transport.post(PATH, PAYLOAD)
    await client.aclose()

    assert sleep.delays == [12.0]


@pytest.mark.parametrize("value", ["31", "3600"])
async def test_retry_after_beyond_the_cap_fails_at_once_for_429(
    sleep: FakeSleep, value: str
) -> None:
    script = Script(error_response(429, headers={"Retry-After": value}), repeat_last=True)
    transport, client = transport_for(script, sleep)

    with pytest.raises(LLMRateLimitError) as raised:
        await transport.post(PATH, PAYLOAD)
    await client.aclose()

    assert raised.value.status_code == 429
    assert len(script.requests) == 1
    assert sleep.delays == []


async def test_retry_after_beyond_the_cap_fails_at_once_for_a_server_error(
    sleep: FakeSleep,
) -> None:
    script = Script(error_response(503, headers={"Retry-After": "120"}), repeat_last=True)
    transport, client = transport_for(script, sleep)

    with pytest.raises(LLMUnavailableError):
        await transport.post(PATH, PAYLOAD)
    await client.aclose()

    assert len(script.requests) == 1


async def test_retry_after_exactly_at_the_cap_is_still_waited_for(sleep: FakeSleep) -> None:
    script = Script(error_response(429, headers={"Retry-After": "30"}), completion_response())
    transport, client = transport_for(script, sleep)

    await transport.post(PATH, PAYLOAD)
    await client.aclose()

    assert sleep.delays == [30.0]


@pytest.mark.parametrize("value", ["soon", "", "-5", "nan", "1e3"])
async def test_an_unreadable_retry_after_falls_back_to_the_backoff(
    sleep: FakeSleep, value: str
) -> None:
    script = Script(error_response(429, headers={"Retry-After": value}), completion_response())
    transport, client = transport_for(script, sleep)

    await transport.post(PATH, PAYLOAD)
    await client.aclose()

    assert sleep.delays == [1.0]


@pytest.mark.parametrize("status", [401, 403])
async def test_auth_failures_are_not_retried(sleep: FakeSleep, status: int) -> None:
    script = Script(error_response(status), repeat_last=True)
    transport, client = transport_for(script, sleep)

    with pytest.raises(LLMAuthError) as raised:
        await transport.post(PATH, PAYLOAD)
    await client.aclose()

    assert raised.value.status_code == status
    assert len(script.requests) == 1
    assert sleep.delays == []
    assert "secret body" not in str(raised.value)


@pytest.mark.parametrize("status", [400, 402, 404, 422])
async def test_other_client_errors_are_not_retried(sleep: FakeSleep, status: int) -> None:
    script = Script(error_response(status), repeat_last=True)
    transport, client = transport_for(script, sleep)

    with pytest.raises(LLMRequestError) as raised:
        await transport.post(PATH, PAYLOAD)
    await client.aclose()

    assert raised.value.status_code == status
    assert len(script.requests) == 1
    assert "secret body" not in str(raised.value)


async def test_an_unexpected_status_is_an_invalid_response(
    sleep: FakeSleep,
) -> None:
    script = Script(error_response(302), repeat_last=True)
    transport, client = transport_for(script, sleep)

    with pytest.raises(LLMInvalidResponseError):
        await transport.post(PATH, PAYLOAD)
    await client.aclose()

    assert len(script.requests) == 1


async def test_a_hanging_attempt_hits_the_deadline_and_counts_as_a_network_error(
    sleep: FakeSleep,
) -> None:
    hanging = Hanging()
    transport, client = transport_for(hanging, sleep, deadline_seconds=0.01, max_retries=2)

    with pytest.raises(LLMUnavailableError) as raised:
        await transport.post(PATH, PAYLOAD)
    await client.aclose()

    assert len(hanging.requests) == 3
    assert sleep.delays == [1.0, 2.0]
    assert "TimeoutError" in str(raised.value)


async def test_a_hanging_attempt_followed_by_an_answer_succeeds(sleep: FakeSleep) -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            await asyncio.sleep(3600)
        return completion_response()

    client = httpx.AsyncClient(base_url=BASE_URL, transport=httpx.MockTransport(handler))
    transport = RetryingTransport(client, sleep=sleep, deadline_seconds=0.01)

    response = await transport.post(PATH, PAYLOAD)
    await client.aclose()

    assert response.retries == 1


@pytest.mark.parametrize(
    "failure",
    [
        httpx.ConnectError("refused"),
        httpx.ConnectTimeout("slow"),
        httpx.ReadTimeout("slow"),
        httpx.RemoteProtocolError("broken"),
        httpx.ReadError("reset"),
    ],
)
async def test_no_httpx_exception_leaks(sleep: FakeSleep, failure: Exception) -> None:
    script = Script(failure, repeat_last=True)
    transport, client = transport_for(script, sleep)

    with pytest.raises(LLMError) as raised:
        await transport.post(PATH, PAYLOAD)
    await client.aclose()

    assert not isinstance(raised.value, httpx.HTTPError)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert type(failure).__name__ in str(raised.value)


async def test_retries_are_logged_as_warnings_with_the_status_or_the_class_only(
    sleep: FakeSleep, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    script = Script(
        error_response(429), httpx.ConnectError("refused to host"), completion_response()
    )
    transport, client = transport_for(script, sleep)

    await transport.post(PATH, PAYLOAD)
    await client.aclose()

    retries = [r for r in caplog.records if r.getMessage() == "llm request will be retried"]
    assert [r.levelno for r in retries] == [logging.WARNING, logging.WARNING]
    assert retries[0].__dict__["status"] == 429
    assert retries[0].__dict__["error"] is None
    assert retries[1].__dict__["status"] is None
    assert retries[1].__dict__["error"] == "ConnectError"
    assert "refused to host" not in caplog.text
    assert "secret body" not in caplog.text


def test_parse_retry_after_reads_seconds_dates_and_garbage() -> None:
    assert parse_retry_after("5", lambda: NOW) == 5.0
    assert parse_retry_after(" 2.5 ", lambda: NOW) == 2.5
    assert parse_retry_after(None, lambda: NOW) is None
    assert parse_retry_after("garbage", lambda: NOW) is None
    past = format_datetime(NOW - timedelta(seconds=60), usegmt=True)
    assert parse_retry_after(past, lambda: NOW) == 0.0
    future = format_datetime(NOW + timedelta(seconds=90), usegmt=True)
    assert parse_retry_after(future, lambda: NOW) == 90.0
